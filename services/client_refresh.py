"""
services/client_refresh.py
--------------------------
Starting, running and reading a client's Meta / GoHighLevel data refresh. The refresh route
(routers/client_groups.py) and the MCP tools (ai/mcp/refresh_mcp.py) both go through here.

Meta is queued: a meta_refresh_jobs document the every-minute meta-tick cron claims and advances, so it
survives the request that asked for it. GoHighLevel has no queue (the ghl-tick only refreshes groups that are
due), so it runs in-process, as the route's background task does.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.ghl_service import fetch_and_cache_ghl_data_optimized

logger = logging.getLogger(__name__)

INTEGRATIONS = ("meta", "ghl")

# A GHL claim younger than this means a load is in progress; an older one is stuck (routers/cron.py imports this
# as GHL_STALE_CLAIM_MINUTES).
GHL_CLAIM_HELD_MINUTES = 10

_OPEN_META_JOB = ["pending", "in_progress", "partial"]
_tasks: set = set()  # strong references: the loop only keeps weak ones to running tasks


async def claim_refresh(db, group_id: str, integration: str) -> None:
    """Mark the group's refresh running. The claim time goes with the status: the ghl-tick reads it to decide
    whether a running group is held or merely stuck, so a status alone left the group claimable and the tick
    would run a second FULL LOAD of the same location alongside this one."""
    claim = {f"{integration}_refresh_status": "running"}
    if integration == "ghl":
        claim["_ghl_claimed_at"] = datetime.utcnow()
    await db.client_groups.update_one({"id": group_id}, {"$set": claim})


async def run_refresh(group_id: str, integration: str, user_id: str, group: dict):
    """Full invalidate + refresh for one integration, recording the outcome on the group."""
    status_field = f"{integration}_refresh_status"
    refresh_field = f"last_{integration}_refresh"

    try:
        async with get_mongo_client() as mongo_client:
            db = mongo_client[DB_NAME]

            if integration == "meta":
                from services.meta_refresh_manager import start_refresh

                ad_account_id = group.get("meta_ad_account_id")
                if not ad_account_id:
                    logger.warning(f"No Meta ad account for group {group_id}")
                    await db.client_groups.update_one(
                        {"id": group_id},
                        {"$set": {status_field: "error"},
                         "$unset": {"_ghl_claimed_at": ""}},
                    )
                    return

                # Delegate to the resilient refresh manager
                await start_refresh(group_id, user_id, group, mongo_client)
                return  # manager handles status updates

            elif integration == "ghl":
                location_id = group.get("ghl_location_id")
                if not location_id:
                    logger.warning(f"No GHL location for group {group_id}")
                    await db.client_groups.update_one(
                        {"id": group_id},
                        {"$set": {status_field: "error"},
                         "$unset": {"_ghl_claimed_at": ""}},
                    )
                    return

                await fetch_and_cache_ghl_data_optimized(
                    group_id=group_id,
                    ghl_location_id=location_id,
                    user_id=user_id,
                    mongo_client=mongo_client,
                    is_initial_load=True,
                )

            # Mark complete
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {
                    status_field: "complete",
                    refresh_field: datetime.utcnow(),
                },
                 "$unset": {"_ghl_claimed_at": ""}},
            )
            logger.info(f"Refresh complete: {integration} for group {group_id}")

    except Exception as e:
        logger.error(f"Refresh failed: {integration} for group {group_id}: {e}", exc_info=True)
        error_msg = str(e)[:300]
        try:
            async with get_mongo_client() as mc:
                await mc[DB_NAME].client_groups.update_one(
                    {"id": group_id},
                    {"$set": {
                        status_field: "error",
                        f"{integration}_refresh_error": error_msg,
                    },
                     "$unset": {"_ghl_claimed_at": ""}},
                )
        except Exception:
            pass


async def get_refresh_status(mongo_client, group_id: str, user_id: str) -> dict | None:
    """The refresh status of a group's integrations, or None when the user has no such group."""
    db = mongo_client[DB_NAME]
    group = await db.client_groups.find_one(
        {"id": group_id, "user_id": user_id},
        {
            "meta_refresh_status": 1, "ghl_refresh_status": 1,
            "meta_refresh_error": 1, "ghl_refresh_error": 1,
            "meta_token_error": 1, "_id": 0,
        },
    )
    # `is None`, not falsy: the projection is {} for a group that has never refreshed.
    if group is None:
        return None

    # Granular Meta refresh progress from the job tracker
    from services.meta_refresh_manager import get_refresh_progress
    meta_progress = await get_refresh_progress(group_id, mongo_client)

    ghl_status = group.get("ghl_refresh_status", "idle")
    return {
        "meta_refresh_status": group.get("meta_refresh_status", "idle"),
        "ghl_refresh_status": ghl_status,
        "meta_refresh_error": group.get("meta_refresh_error"),
        # An error left over from a failure that a later refresh got past is not the
        # current state: only report it while the refresh is not complete.
        "ghl_refresh_error": None if ghl_status == "complete" else group.get("ghl_refresh_error"),
        "meta_token_error": group.get("meta_token_error", False),
        "meta_refresh_progress": meta_progress,
    }


def is_connected(group: dict, integration: str) -> bool:
    return bool(group.get("meta_ad_account_id" if integration == "meta" else "ghl_location_id"))


async def is_running(db, group: dict, integration: str) -> bool:
    """Whether a refresh of this integration is already in flight for the group."""
    if integration == "meta":
        return await db["meta_refresh_jobs"].count_documents(
            {"group_id": group["id"], "status": {"$in": _OPEN_META_JOB}}, limit=1
        ) > 0
    return group.get("ghl_refresh_status") == "running" and not ghl_claim_is_stale(group)


def ghl_claim_is_stale(group: dict) -> bool:
    """A GHL refresh marked running whose claim is older than the held window: its task most likely died. The
    ghl-tick only reclaims such a group when it is also due, so this can last until the nightly slot."""
    claimed = group.get("_ghl_claimed_at")
    return (group.get("ghl_refresh_status") == "running" and bool(claimed)
            and claimed < datetime.utcnow() - timedelta(minutes=GHL_CLAIM_HELD_MINUTES))


async def claim_ghl_if_free(db, group_id: str) -> bool:
    """Atomically claim a GHL refresh: only when the group is not running, or its claim is stale. False means
    another caller holds it."""
    held_since = datetime.utcnow() - timedelta(minutes=GHL_CLAIM_HELD_MINUTES)
    res = await db.client_groups.update_one(
        {"id": group_id, "$or": [{"ghl_refresh_status": {"$ne": "running"}},
                                  {"_ghl_claimed_at": {"$lt": held_since}}]},
        {"$set": {"ghl_refresh_status": "running", "_ghl_claimed_at": datetime.utcnow()}},
    )
    return res.modified_count == 1


async def queue_refresh(mongo_client, user_id: str, group: dict, integration: str) -> bool:
    """Start a refresh that outlives the caller. Meta becomes a pending job for the meta-tick cron; GHL runs as
    an in-process task. The caller has checked the integration is connected. False: it was already running (GHL
    claim lost to a concurrent caller)."""
    if integration == "meta":
        from services.meta_refresh_manager import create_refresh_job

        await create_refresh_job(group["id"], user_id, group, mongo_client, status="pending")
        return True
    if not await claim_ghl_if_free(mongo_client[DB_NAME], group["id"]):
        return False
    task = asyncio.create_task(run_refresh(group["id"], "ghl", user_id, group))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True
