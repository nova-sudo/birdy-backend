"""
Staggered refresh cycle — processes all client groups one at a time,
using the SAME refresh logic as the individual group refresh endpoint.

Triggered on-demand via POST /api/client-groups/refresh-all.
"""

import asyncio
import logging
from datetime import datetime

from core.database import DB_NAME
from dependencies import get_mongo_client

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 15 * 60  # 15 minutes between each group

# ── In-memory state for the running cycle ──────────────────────────────────
_cycle_task: asyncio.Task | None = None
_cycle_status: dict = {
    "running": False,
    "current_group": None,
    "groups_done": 0,
    "groups_total": 0,
    "started_at": None,
    "last_refresh_at": None,
}


# ── Public API ─────────────────────────────────────────────────────────────

def start_cycle():
    global _cycle_task
    if _cycle_task and not _cycle_task.done():
        return {"status": "already_running", **_cycle_status}
    _cycle_task = asyncio.create_task(_run_cycle())
    return {"status": "started"}


def stop_cycle():
    global _cycle_task
    if _cycle_task and not _cycle_task.done():
        _cycle_task.cancel()
        _cycle_status["running"] = False
        _cycle_status["current_group"] = None
        return {"status": "stopped"}
    return {"status": "not_running"}


def get_cycle_status() -> dict:
    running = _cycle_task is not None and not _cycle_task.done()
    return {**_cycle_status, "running": running}


# ── Core cycle — reuses the individual refresh logic ───────────────────────

async def _refresh_single_group(group: dict, user_id: str, mongo_client):
    """
    Refresh a single group using the exact same code path as
    POST /api/client-groups/{group_id}/refresh/{integration}.
    """
    from services.meta_refresh_manager import start_refresh
    from services.ghl_service import fetch_and_cache_ghl_data_optimized

    group_id = group["id"]
    group_name = group.get("name", "Unknown")
    db = mongo_client[DB_NAME]

    # ── Meta ───────────────────────────────────────────────────────────
    if group.get("meta_ad_account_id"):
        try:
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {"meta_refresh_status": "running"}},
            )
            await start_refresh(group_id, user_id, group, mongo_client)
            logger.info(f"[refresh-all] Meta OK for '{group_name}'")
        except Exception as e:
            logger.error(f"[refresh-all] Meta FAIL for '{group_name}': {e}")
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {"meta_refresh_status": "error"}},
            )

    # ── GHL ────────────────────────────────────────────────────────────
    if group.get("ghl_location_id"):
        try:
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {"ghl_refresh_status": "running"}},
            )
            await fetch_and_cache_ghl_data_optimized(
                group_id=group_id,
                ghl_location_id=group["ghl_location_id"],
                user_id=user_id,
                mongo_client=mongo_client,
                is_initial_load=True,
            )
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {
                    "ghl_refresh_status": "complete",
                    "last_ghl_refresh": datetime.utcnow(),
                }},
            )
            logger.info(f"[refresh-all] GHL OK for '{group_name}'")
        except Exception as e:
            logger.error(f"[refresh-all] GHL FAIL for '{group_name}': {e}")
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {"ghl_refresh_status": "error"}},
            )


async def _run_cycle():
    """Fetch all groups, then refresh them one-by-one with a 15-min gap."""
    _cycle_status.update(
        running=True, groups_done=0, groups_total=0,
        current_group=None, started_at=datetime.utcnow().isoformat(),
        last_refresh_at=None,
    )

    try:
        async with get_mongo_client() as mongo_client:
            db = mongo_client[DB_NAME]

            # Build flat list of (user_id, group)
            pipeline = [
                {"$match": {
                    "$or": [
                        {"meta_ad_account_id": {"$exists": True, "$ne": None}},
                        {"ghl_location_id": {"$exists": True, "$ne": None}},
                    ]
                }},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}},
            ]
            user_rows = await db["client_groups"].aggregate(pipeline).to_list(None)

            queue: list[tuple[str, dict]] = []
            for row in user_rows:
                for g in row["groups"]:
                    queue.append((row["_id"], g))

            if not queue:
                logger.info("[refresh-all] No groups with integrations found")
                _cycle_status["running"] = False
                return

            _cycle_status["groups_total"] = len(queue)
            logger.info(f"[refresh-all] Starting — {len(queue)} groups, one every 15 min")

            for idx, (user_id, group) in enumerate(queue):
                group_name = group.get("name", "Unknown")
                _cycle_status["current_group"] = group_name

                logger.info(f"[refresh-all] [{idx + 1}/{len(queue)}] Refreshing '{group_name}'")

                await _refresh_single_group(group, user_id, mongo_client)

                _cycle_status["groups_done"] = idx + 1
                _cycle_status["last_refresh_at"] = datetime.utcnow().isoformat()

                # Wait 15 min before next group (skip after last)
                if idx < len(queue) - 1:
                    logger.info(f"[refresh-all] Waiting 15 min before next group ({idx + 2}/{len(queue)})")
                    await asyncio.sleep(INTERVAL_SECONDS)

            logger.info(
                f"[refresh-all] Cycle complete — "
                f"{_cycle_status['groups_done']}/{_cycle_status['groups_total']} groups"
            )

    except asyncio.CancelledError:
        logger.info("[refresh-all] Cycle cancelled")
    except Exception as e:
        logger.error(f"[refresh-all] Critical error: {e}", exc_info=True)
    finally:
        _cycle_status["running"] = False
        _cycle_status["current_group"] = None
