"""
One-time backfill: reruns fetch_and_cache_hp_call_center for every HP-linked
client_group so today's improvements land immediately instead of waiting up
to 24h for the next hp-tick cron cycle.

What the cron now does (that this script exercises):

    1. Persists every normalized HP call to the shared `call_logs` collection
       (source="hotprospector") — future-facing, /api/call_logs serves them.
    2. Appends a synthetic "Unmatched calls" pseudo-lead to
       `hotprospector_leads` for each location that has calls without a
       matching lead — Sales-Hub then displays those calls, and its per-lead
       durations reconcile with Overview's Talk Time card.

Selection rule: we pick every client_group that has a `ghl_location_id`,
regardless of `call_log_provider`. HotProspector is scoped by GHL location,
not by the frontend's provider flag — a group that stores GHL-side calls
via the webhook can still have HP call data on the same location, and vice
versa. Skipping by `call_log_provider` would miss those.

Idempotent: safe to run multiple times. `call_logs` dedupes on
(source, source_event_id) via the existing unique index; `hotprospector_leads`
does a delete-then-insert per (user, location).

Run with:  python -m scripts.backfill_hp_call_logs
"""

import asyncio
import logging
import os
import sys
from collections import Counter

# Absolute imports need the project root on sys.path when invoked as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from integrations.hotprospector import (
    HotProspectorIntegration,
    get_hotprospector_credentials,
)
from integrations.gohighlevel import get_subaccount_tokens
from services.hp_service import fetch_and_cache_hp_call_center

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get(
    "MONGODB_URI",
    os.environ.get("MONGO_URI", "mongodb://localhost:27017"),
)


async def _iter_hp_targets(db):
    """
    Yield (user_id, ghl_location_id, client_group_name) for every group we
    should refresh. Dedup by (user, location) so a user with the same
    GHL location on two groups only pays for the fetch once — the cron's
    downstream `update_many` still stamps all their groups.
    """
    seen = set()
    cursor = db["client_groups"].find(
        {"ghl_location_id": {"$exists": True, "$ne": None, "$ne": ""}},
        projection={"user_id": 1, "ghl_location_id": 1, "name": 1, "_id": 0},
    )
    async for g in cursor:
        key = (g["user_id"], g["ghl_location_id"])
        if key in seen:
            continue
        seen.add(key)
        yield g["user_id"], g["ghl_location_id"], g.get("name")


async def _run_one(client, user_id, ghl_location_id, group_name, hp_creds_cache):
    """
    Run fetch_and_cache_hp_call_center for a single (user, location).
    Skips gracefully if the user has no HP credentials — they're not on
    HotProspector, so there's nothing to backfill.
    """
    # Credentials are per-user; cache to avoid re-hitting Mongo per group.
    if user_id not in hp_creds_cache:
        creds = await get_hotprospector_credentials(user_id, client)
        hp_creds_cache[user_id] = creds
    creds = hp_creds_cache[user_id]

    if not creds:
        return {"status": "skipped_no_creds", "user_id": user_id, "location_id": ghl_location_id}

    integration = HotProspectorIntegration(creds.get("api_uid"), creds.get("api_key"))

    # Location display name (mirrors what the cron does on line ~296).
    subaccounts = await get_subaccount_tokens(user_id, client)
    location_name = subaccounts.get(ghl_location_id, {}).get("name", "Unknown Location")

    result = await fetch_and_cache_hp_call_center(
        user_id=user_id,
        ghl_location_id=ghl_location_id,
        mongo_client=client,
        integration=integration,
        location_name=location_name,
        client_group_name=group_name,
        mode="backfill",  # full history — we want the write to call_logs to be complete
    )
    return {
        "status": "ok" if result.get("success") else "failed",
        "user_id": user_id,
        "location_id": ghl_location_id,
        "total_leads": result.get("total_leads", 0),
        "total_calls": result.get("total_calls", 0),
        "error": result.get("error"),
    }


async def main() -> None:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    logger.info(f"Connected to MongoDB: {DB_NAME}")

    counters = Counter()
    hp_creds_cache: dict = {}

    async for user_id, ghl_location_id, group_name in _iter_hp_targets(db):
        counters["seen"] += 1
        logger.info(
            "Backfilling — user=%s location=%s group=%s (seen=%d)",
            user_id, ghl_location_id, group_name, counters["seen"],
        )
        try:
            outcome = await _run_one(client, user_id, ghl_location_id, group_name, hp_creds_cache)
        except Exception as e:
            logger.exception(
                "  failed — user=%s location=%s err=%s", user_id, ghl_location_id, e,
            )
            counters["errored"] += 1
            continue

        counters[outcome["status"]] += 1
        if outcome["status"] == "ok":
            counters["calls_total"] += outcome.get("total_calls", 0)
            counters["leads_total"] += outcome.get("total_leads", 0)
        elif outcome["status"] == "failed":
            logger.warning(
                "  fetch_and_cache_hp_call_center returned failure — %s: %s",
                (user_id, ghl_location_id), outcome.get("error"),
            )

    logger.info(
        "Backfill complete — seen: %s | ok: %s | skipped_no_creds: %s | failed: %s | "
        "errored: %s | total calls: %s | total leads: %s",
        counters["seen"],
        counters["ok"],
        counters["skipped_no_creds"],
        counters["failed"],
        counters["errored"],
        counters["calls_total"],
        counters["leads_total"],
    )
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
