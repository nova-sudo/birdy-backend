"""
Backfill script — populates hotprospector_call_cache (per-preset call stats) and
hotprospector_cache.metrics for every existing HotProspector-provider client group.

For each user with a HotProspector integration, for each DISTINCT ghl_location_id
used by a call_log_provider == "hotprospector" client group, it runs the single
canonical sync path services.hp_service.fetch_and_cache_hp_call_center(mode="backfill"),
which fetches leads + the full call-log history, derives stats for all 13 date
presets, and writes them onto every client group at that location via update_many.

This mirrors scripts/backfill_opportunity_stats.py (GHL). The per-minute hp-tick
cron already keeps these fresh going forward; this script is for filling existing
groups immediately (e.g. after deploying the new answered_calls / total_talk_min
fields) without waiting for the cron to walk every group.

Run with:  python -m scripts.backfill_call_stats
"""

import asyncio
import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# This is a long-running one-shot job (not the timeout-bounded cron), so opt into
# GENTLE proactive spacing — wide enough to stay under HotProspector's SearchByUserInput
# limit so we rarely hit a 429 — plus a PATIENT, generous backoff budget for the rare
# one. setdefault so an explicit .env value still wins. Must be set BEFORE importing the
# integration, which reads these at import time.
os.environ.setdefault("HP_SEARCH_MIN_INTERVAL", "13")   # ~4-5 searches/min, under the limit
os.environ.setdefault("HP_MAX_429_WAIT", "65")          # honor the full "try again in 60s"
os.environ.setdefault("HP_MAX_429_RETRIES", "3")
os.environ.setdefault("HP_MAX_TOTAL_BACKOFF", "600")    # allow long cumulative waits

from motor.motor_asyncio import AsyncIOMotorClient
from core.database import DB_NAME
from services.hp_service import fetch_and_cache_hp_call_center

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill():
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    users_col = db["users"]
    groups_col = db["client_groups"]

    users = await users_col.find(
        {"integrations.hotprospector": {"$exists": True}},
        {"user_id": 1},
    ).to_list(None)

    logger.info("Found %d users with a HotProspector integration", len(users))

    total_locations = 0
    total_ok = 0
    total_failed = 0

    for user_doc in users:
        user_id = user_doc["user_id"]

        groups = await groups_col.find(
            {
                "user_id": user_id,
                "call_log_provider": "hotprospector",
                "ghl_location_id": {"$exists": True, "$ne": None},
            },
            {"ghl_location_id": 1},
        ).to_list(None)

        # One sync per DISTINCT location (update_many stamps all groups there).
        location_ids = sorted({g["ghl_location_id"] for g in groups if g.get("ghl_location_id")})
        if not location_ids:
            continue

        logger.info("\n" + "=" * 60)
        logger.info("User %s — %d HotProspector location(s)", user_id, len(location_ids))

        for location_id in location_ids:
            total_locations += 1
            try:
                result = await fetch_and_cache_hp_call_center(
                    user_id, location_id, client, mode="backfill",
                )
            except Exception as e:
                logger.error("  [%s] backfill crashed: %s", location_id, e)
                total_failed += 1
                await asyncio.sleep(1)
                continue

            if result.get("success"):
                total_ok += 1
                logger.info(
                    "  [%s] %d leads, %d calls (in=%d, out=%d)",
                    location_id, result.get("total_leads", 0), result.get("total_calls", 0),
                    result.get("inbound", 0), result.get("outbound", 0),
                )
            else:
                total_failed += 1
                logger.warning("  [%s] backfill failed: %s", location_id, result.get("error"))
            await asyncio.sleep(1)

    logger.info("\n" + "=" * 60)
    logger.info(
        "Call-stats backfill complete: %d locations, %d ok, %d failed",
        total_locations, total_ok, total_failed,
    )

    client.close()


if __name__ == "__main__":
    asyncio.run(backfill())
