"""
Backfill script — populates hp_daily_calls for every HotProspector client group.

Makes **no HotProspector API calls**. The daily series is derived entirely
from call logs already stored in hotprospector_leads (see
services.hp_service.cache_hp_daily_calls_from_stored), so it's safe to re-run
and costs nothing but a read per group.

Run with:
    python -m scripts.backfill_daily_calls
    python -m scripts.backfill_daily_calls --user someone@example.com
"""

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from services.hp_service import cache_hp_daily_calls_from_stored

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill(user_filter=None):
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    query = {"call_log_provider": "hotprospector", "ghl_location_id": {"$exists": True, "$nin": [None, ""]}}
    if user_filter:
        query["user_id"] = user_filter

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "user_id": 1, "ghl_location_id": 1,
                "client_status": 1, "_id": 0},
    ).to_list(None)
    groups = [g for g in groups if (g.get("client_status") or "Active") == "Active"]

    # De-dupe locations shared across multiple groups — same reasoning as
    # hp-tick: refreshing one writes hp_daily_calls to every group at that
    # location via update_many, so re-deriving it per sibling is wasted work.
    seen_locations = set()
    ok = failed = skipped = 0
    for group in groups:
        loc = group["ghl_location_id"]
        if loc in seen_locations:
            skipped += 1
            continue
        seen_locations.add(loc)

        try:
            written = await cache_hp_daily_calls_from_stored(
                group_id=group["id"],
                user_id=group["user_id"],
                ghl_location_id=loc,
                mongo_client=client,
            )
            ok += 1
            logger.info("  %s: %d days", group.get("name"), written)
        except Exception as e:
            failed += 1
            logger.warning("  %s: failed — %s", group.get("name"), e)

    logger.info("Done — %d locations cached, %d failed, %d sibling groups skipped", ok, failed, skipped)
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", help="only this user_id")
    a = p.parse_args()
    asyncio.run(backfill(a.user))
