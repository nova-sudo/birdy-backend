"""
Backfill script — populates measured per-day ad spend for every client group.

The refresh keeps this current from now on, but only forward. This fills in the
history so the chart has a real curve for ranges that predate the change.

Cheap: one paginated Meta call per group (not per ad, not per campaign), and
one small array written per group. Safe to re-run — each run rewrites the
window rather than appending, and a failed fetch leaves the existing cache
untouched.

Run with:
    python -m scripts.backfill_daily_spend
    python -m scripts.backfill_daily_spend --days 400 --user someone@example.com
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
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_daily_spend import cache_account_daily_spend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill(user_filter=None, days=400):
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    query = {"meta_ad_account_id": {"$exists": True, "$nin": [None, ""]}}
    if user_filter:
        query["user_id"] = user_filter

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "user_id": 1, "meta_ad_account_id": 1,
                "client_status": 1, "_id": 0},
    ).to_list(None)
    groups = [g for g in groups if (g.get("client_status") or "Active") == "Active"]

    logger.info("Backfilling daily spend for %d active groups (%d days)", len(groups), days)

    tokens = {}
    ok = failed = 0
    for group in groups:
        uid = group["user_id"]
        if uid not in tokens:
            tok = await get_facebook_token(uid, client)
            tokens[uid] = (tok or {}).get("access_token")
        token = tokens[uid]
        if not token:
            logger.warning("  %s: no Meta token — skipped", group.get("name"))
            failed += 1
            continue

        try:
            written = await cache_account_daily_spend(
                group_id=group["id"],
                ad_account_id=group["meta_ad_account_id"],
                access_token=token,
                mongo_client=client,
                days=days,
            )
            if written:
                ok += 1
            else:
                failed += 1
                logger.warning("  %s: nothing written", group.get("name"))
        except Exception as e:
            failed += 1
            logger.warning("  %s: failed — %s", group.get("name"), e)

    logger.info("Done — %d groups cached, %d skipped/failed", ok, failed)
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=400, help="how far back to fetch")
    p.add_argument("--user", help="only this user_id")
    a = p.parse_args()
    asyncio.run(backfill(a.user, a.days))
