"""
Seed the landing-funnel rollup from the visitors we already have.

The rollup (`landing_page_daily`) is written on the way past, so a client who
installed the snippet months ago starts with an empty funnel until their next
visitor arrives. This walks `attribution_visitors` and reconstructs what it
honestly can:

    first_touch.ts   → one visitor, one view, that day
    ...carrying ads  → one ad view, that day
    opted_in_at      → one opt-in, that day
    identified_at    → the opt-in's fallback, for visitors captured before
                       `opted_in_at` existed

**It is an approximation, and says so on the row.** Three things are not
recoverable and are left at zero rather than guessed:

    form starts   nothing recorded them before this feature existed
    return visits a visitor document keeps one `last_seen_at`, so a person who
                  came back on four days reads as one view
    bounces that expired  anonymous visitors carry a 180-day TTL, so any
                  window older than that has already lost the people who did
                  not convert — which is the whole reason the rollup exists

So a backfilled day under-reports views and over-reports the opt-in rate, most
of all in older windows. Rows it writes carry `backfilled: true` so nobody
later mistakes them for counted ones, and days that already have counted
traffic are left alone.

Run with:  python -m scripts.backfill_landing_funnel [--group <id>] [--dry-run]
"""

import argparse
import asyncio
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from services.attribution_service import VISITORS, is_paid_touch
from services.landing_funnel import COUNTERS, LANDING_DAILY, day_of

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


def _empty() -> dict:
    return {key: 0 for key in COUNTERS}


async def backfill(client, group_id: str | None = None, dry_run: bool = False) -> dict:
    db = client[DB_NAME]

    query = {"client_group_id": {"$exists": True}}
    if group_id:
        query["client_group_id"] = group_id

    # (client_group_id, day) → counters
    tally: dict[tuple[str, str], dict] = defaultdict(_empty)
    owners: dict[str, str | None] = {}
    visitors = 0

    cursor = db[VISITORS].find(
        query,
        projection={
            "client_group_id": 1, "user_id": 1, "created_at": 1,
            "first_touch": 1, "last_paid_touch": 1, "touch_count": 1,
            "identified_at": 1, "opted_in_at": 1,
        },
    )
    async for visitor in cursor:
        group = visitor.get("client_group_id")
        if not group:
            continue
        owners.setdefault(group, visitor.get("user_id"))
        visitors += 1

        touch = visitor.get("first_touch") or {}
        landed_at = touch.get("ts") or visitor.get("created_at")
        if landed_at:
            day = tally[(group, day_of(landed_at))]
            day["visitors"] += 1
            day["views"] += 1
            # The only pageview figure available: how many landings this
            # browser has ever reported, all of them booked to the first day.
            day["pageviews"] += int(visitor.get("touch_count") or 1)
            if is_paid_touch(touch) or visitor.get("last_paid_touch"):
                day["ad_views"] += 1

        converted_at = visitor.get("opted_in_at") or visitor.get("identified_at")
        if converted_at:
            tally[(group, day_of(converted_at))]["opt_ins"] += 1

    written = 0
    skipped = 0
    now = datetime.utcnow()
    for (group, day), counters in sorted(tally.items()):
        existing = await db[LANDING_DAILY].find_one(
            {"client_group_id": group, "day": day}, {"backfilled": 1}
        )
        # A day with counted traffic is the better record of itself. Adding an
        # approximation on top of it would double every visitor who happened to
        # land after this feature shipped.
        if existing and not existing.get("backfilled"):
            skipped += 1
            continue

        if dry_run:
            written += 1
            continue

        await db[LANDING_DAILY].update_one(
            {"client_group_id": group, "day": day},
            {
                "$set": {
                    **counters,
                    "user_id": owners.get(group),
                    "backfilled": True,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        written += 1

    return {"visitors": visitors, "days": written, "skipped": skipped}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", help="Only this client group id")
    parser.add_argument("--dry-run", action="store_true", help="Count without writing")
    args = parser.parse_args()

    client = AsyncIOMotorClient(MONGO_URI)
    try:
        result = await backfill(client, group_id=args.group, dry_run=args.dry_run)
    finally:
        client.close()

    logger.info(
        "%s %d day rows from %d visitors (%d days left alone — already counted)",
        "Would write" if args.dry_run else "Wrote",
        result["days"], result["visitors"], result["skipped"],
    )


if __name__ == "__main__":
    asyncio.run(main())
