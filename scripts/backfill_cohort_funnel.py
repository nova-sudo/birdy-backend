"""
Backfill script — populates ghl_funnel_cache for every client group.

The dashboard's Performance funnel reads `ghl_funnel_cache.<preset>`, which the
GHL refresh writes going forward. This fills it in for groups that have not been
refreshed since the cache was introduced, so the card has something to show
without waiting for a refresh cycle to come round.

Unlike scripts/backfill_opportunity_stats.py this makes **no external API
calls**. The cohort funnel is derived entirely from data already in Mongo —
`ghl_contacts` for the cohort and its opportunities, `hotprospector_leads` for
which of them were called — so it is safe to re-run and costs nothing but a
read per group.

Run with:  python -m scripts.backfill_cohort_funnel
           python -m scripts.backfill_cohort_funnel --user someone@example.com
"""

import argparse
import asyncio
import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from core.database import DB_NAME
from services.ghl_service import cache_ghl_cohort_funnel_all_presets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill(user_filter: str | None = None):
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    query = {}
    if user_filter:
        query["user_id"] = user_filter

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "user_id": 1, "ghl_location_id": 1, "_id": 0}
    ).to_list(None)

    logger.info("Found %d client groups%s", len(groups), f" for {user_filter}" if user_filter else "")

    updated = failed = 0
    for group in groups:
        group_id = group.get("id")
        if not group_id:
            continue
        try:
            await cache_ghl_cohort_funnel_all_presets(
                group_id,
                group.get("user_id"),
                group.get("ghl_location_id") or "",
                client,
            )
            updated += 1
        except Exception as e:
            failed += 1
            logger.warning("Failed for %s (%s): %s", group.get("name"), group_id, e)

    logger.info("Done — %d groups updated, %d failed", updated, failed)
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", help="only backfill groups belonging to this user_id")
    args = parser.parse_args()
    asyncio.run(backfill(args.user))
