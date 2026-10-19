"""
Backfill script — populates ghl_daily_leads for every client group.

The refresh keeps this current from now on, but only forward. This fills in
the history so the Leads page chart has a real curve without waiting for the
next GHL cron pass to touch every location.

Cheap and safe to re-run: no GHL API calls at all — ghl_contacts is already
fully synced, this just re-buckets what's already on disk. Each run rewrites
the whole series, and an empty read leaves a non-empty existing cache alone.

Run with:
    python -m scripts.backfill_daily_leads
    python -m scripts.backfill_daily_leads --user someone@example.com
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
from services.ghl_daily_leads import cache_ghl_daily_leads

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill(user_filter=None):
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    query = {"ghl_location_id": {"$exists": True, "$nin": [None, ""]}}
    if user_filter:
        query["user_id"] = user_filter

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "user_id": 1, "ghl_location_id": 1,
                "client_status": 1, "_id": 0},
    ).to_list(None)
    groups = [g for g in groups if (g.get("client_status") or "Active") == "Active"]

    logger.info("Backfilling daily leads for %d active groups", len(groups))

    ok = failed = 0
    for group in groups:
        try:
            written = await cache_ghl_daily_leads(
                group_id=group["id"],
                user_id=group["user_id"],
                ghl_location_id=group["ghl_location_id"],
                mongo_client=client,
            )
            ok += 1
            logger.info("  %s: %d days", group.get("name"), written)
        except Exception as e:
            failed += 1
            logger.warning("  %s: failed — %s", group.get("name"), e)

    logger.info("Done — %d groups cached, %d failed", ok, failed)
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--user", help="only this user_id")
    a = p.parse_args()
    asyncio.run(backfill(a.user))
