import asyncio
import logging
from datetime import datetime

import httpx

from core.database import DB_NAME
from core.constants import META_PRESETS_FREQUENT
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_service import (
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)
from services.ghl_service import fetch_and_cache_ghl_data_optimized

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
    """Start the staggered refresh cycle. Returns immediately."""
    global _cycle_task
    if _cycle_task and not _cycle_task.done():
        return {"status": "already_running", **_cycle_status}

    _cycle_task = asyncio.create_task(_run_cycle())
    return {"status": "started"}


def stop_cycle():
    """Cancel a running cycle."""
    global _cycle_task
    if _cycle_task and not _cycle_task.done():
        _cycle_task.cancel()
        _cycle_status["running"] = False
        _cycle_status["current_group"] = None
        return {"status": "stopped"}
    return {"status": "not_running"}


def get_cycle_status() -> dict:
    """Return the current cycle state."""
    running = _cycle_task is not None and not _cycle_task.done()
    return {**_cycle_status, "running": running}


# ── Core cycle logic ───────────────────────────────────────────────────────

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
            groups_col = db["client_groups"]

            # Build a flat list of (user_id, group) across all users
            pipeline = [
                {"$match": {
                    "$or": [
                        {"meta_ad_account_id": {"$exists": True, "$ne": None}},
                        {"ghl_location_id": {"$exists": True, "$ne": None}},
                    ]
                }},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}},
            ]
            user_rows = await groups_col.aggregate(pipeline).to_list(None)

            # Flatten and cache validated tokens per user
            queue: list[tuple[str, dict]] = []
            meta_tokens: dict[str, str | None] = {}

            for row in user_rows:
                user_id = row["_id"]
                for g in row["groups"]:
                    queue.append((user_id, g))

                # Validate Meta token once per user
                has_meta = any(g.get("meta_ad_account_id") for g in row["groups"])
                if has_meta:
                    token_doc = await get_facebook_token(user_id, mongo_client)
                    access_token = token_doc.get("access_token") if token_doc else None
                    if access_token and await _validate_meta_token(access_token):
                        meta_tokens[user_id] = access_token
                        await groups_col.update_many(
                            {"user_id": user_id, "meta_token_error": True},
                            {"$unset": {"meta_token_error": "", "meta_token_error_at": ""}},
                        )
                    else:
                        meta_tokens[user_id] = None
                        if access_token:
                            await groups_col.update_many(
                                {"user_id": user_id, "meta_ad_account_id": {"$exists": True, "$ne": None}},
                                {"$set": {"meta_token_error": True, "meta_token_error_at": datetime.utcnow()}},
                            )

            if not queue:
                logger.info("[refresh-cycle] No groups with integrations found")
                _cycle_status["running"] = False
                return

            _cycle_status["groups_total"] = len(queue)
            logger.info(f"[refresh-cycle] Starting — {len(queue)} groups, one every 15 min")

            # ── Process one group at a time ────────────────────────────
            for idx, (user_id, group) in enumerate(queue):
                group_name = group.get("name", "Unknown")
                group_id = group["id"]
                _cycle_status["current_group"] = group_name

                logger.info(
                    f"[refresh-cycle] [{idx + 1}/{len(queue)}] "
                    f"Refreshing '{group_name}'"
                )

                # Meta
                access_token = meta_tokens.get(user_id)
                if group.get("meta_ad_account_id") and access_token:
                    try:
                        await _refresh_group_meta(
                            group, user_id, access_token, mongo_client, db,
                        )
                        logger.info(f"[refresh-cycle]   Meta OK for '{group_name}'")
                    except Exception as e:
                        logger.error(f"[refresh-cycle]   Meta FAIL for '{group_name}': {e}")

                # GHL
                if group.get("ghl_location_id"):
                    try:
                        await _refresh_group_ghl(group, user_id, mongo_client)
                        logger.info(f"[refresh-cycle]   GHL OK for '{group_name}'")
                    except Exception as e:
                        logger.error(f"[refresh-cycle]   GHL FAIL for '{group_name}': {e}")

                _cycle_status["groups_done"] = idx + 1
                _cycle_status["last_refresh_at"] = datetime.utcnow().isoformat()

                # Wait 15 min before the next group (skip wait after the last one)
                if idx < len(queue) - 1:
                    logger.info(
                        f"[refresh-cycle] Waiting 15 min before next group "
                        f"({idx + 2}/{len(queue)})"
                    )
                    await asyncio.sleep(INTERVAL_SECONDS)

            logger.info(
                f"[refresh-cycle] Cycle complete — "
                f"{_cycle_status['groups_done']}/{_cycle_status['groups_total']} groups"
            )

    except asyncio.CancelledError:
        logger.info("[refresh-cycle] Cycle cancelled")
    except Exception as e:
        logger.error(f"[refresh-cycle] Critical error: {e}", exc_info=True)
    finally:
        _cycle_status["running"] = False
        _cycle_status["current_group"] = None


# ── Per-group helpers ──────────────────────────────────────────────────────

async def _validate_meta_token(access_token: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://graph.facebook.com/v25.0/me",
                params={"access_token": access_token},
            )
            if resp.status_code == 200:
                return True
            body = resp.text
            if any(s in body for s in ('"code":190', '"code":200', "not allowed",
                                       "not a confirmed user", "ads_management")):
                return False
            return True
    except Exception:
        return True


async def _refresh_group_meta(group, user_id, access_token, mongo_client, db):
    group_id = group["id"]
    group_name = group.get("name", "Unknown")
    meta_ad_account_id = group["meta_ad_account_id"]
    ad_account_currency = group.get("ad_account_currency")

    if not ad_account_currency:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"https://graph.facebook.com/v25.0/{meta_ad_account_id}",
                    params={"fields": "currency", "access_token": access_token},
                )
                ad_account_currency = resp.json().get("currency")
            if ad_account_currency:
                await db["client_groups"].update_one(
                    {"id": group_id},
                    {"$set": {"ad_account_currency": ad_account_currency}},
                )
        except Exception as e:
            logger.error(f"  Could not fetch currency for '{group_name}': {e}")
            return

    if not ad_account_currency:
        logger.warning(f"  Skipping '{group_name}' — no currency")
        return

    await fetch_meta_all_presets_for_group(
        group_id, meta_ad_account_id, user_id, mongo_client,
        ad_account_currency, presets=META_PRESETS_FREQUENT,
    )

    # Incremental leads + today's granular insights
    from integrations.facebook_utils.meta_incremental_refresh import (
        update_todays_campaign_insights,
        update_todays_adset_insights,
        update_todays_ad_insights,
        fetch_todays_facebook_leads_incremental,
    )

    await asyncio.gather(
        update_todays_campaign_insights(
            meta_ad_account_id, access_token, user_id,
            group_id, group_name, mongo_client, ad_account_currency,
        ),
        update_todays_adset_insights(
            meta_ad_account_id, access_token, user_id,
            group_id, group_name, mongo_client, ad_account_currency,
        ),
        update_todays_ad_insights(
            meta_ad_account_id, access_token, user_id,
            group_id, group_name, mongo_client, ad_account_currency,
        ),
        return_exceptions=True,
    )

    new_leads_count, new_leads = await fetch_todays_facebook_leads_incremental(
        meta_ad_account_id, access_token, user_id,
        group_id, group_name, mongo_client, max_concurrent_ads=5,
    )

    if new_leads:
        lead_docs = [
            {
                "user_id": user_id,
                "ad_account_id": meta_ad_account_id,
                "client_group_id": group_id,
                "client_group_name": group_name,
                "lead_id": lead.get("lead_id"),
                "lead_data": lead,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            for lead in new_leads
        ]
        try:
            await db["facebook_leads"].insert_many(lead_docs, ordered=False)
        except Exception:
            pass

    await update_preset_lead_counts(group_id, user_id, mongo_client)


async def _refresh_group_ghl(group, user_id, mongo_client):
    await fetch_and_cache_ghl_data_optimized(
        group["id"], group["ghl_location_id"], user_id,
        mongo_client, is_initial_load=False,
    )
