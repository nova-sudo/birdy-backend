"""
Backfill script — stores HotProspector call times in call_logs as UTC.

HotProspector shows a call's time in the account's local time with no zone
("Oct 27, 2026 1:30 pm"). _persist_hp_calls_to_call_logs used to store that
wall-clock time as if it were UTC, so in British Summer Time every HP call sat
an hour late and never lined up with the same call in GoHighLevel. New writes
read it in the owner's timezone (services.hp_service._hp_started_at); this
recomputes started_at for existing rows from raw_payload.call_time_iso, one
server-side update per owner. Re-running it changes nothing.

Dry run by default; pass --apply to write.

Run with:  python -m scripts.backfill_hp_call_started_at [--apply]
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from services.hp_service import _hp_zone


def _utc_start(zone_name: str) -> dict:
    return {"$dateFromString": {
        "dateString": "$raw_payload.call_time_iso", "timezone": zone_name, "onError": None, "onNull": None}}


async def backfill(mongo_client, apply: bool) -> dict:
    db = mongo_client[DB_NAME]
    coll = db["call_logs"]
    counts = {"owners": 0, "matching": 0, "updated": 0}
    for user_id in await coll.distinct("user_id", {"source": "hotprospector"}):
        owner = await db["users"].find_one({"user_id": user_id}, {"timezone": 1})
        zone = _hp_zone((owner or {}).get("timezone")).key
        start = _utc_start(zone)
        flt = {
            "source": "hotprospector",
            "user_id": user_id,
            "raw_payload.call_time_iso": {"$type": "string", "$ne": ""},
            "$expr": {"$and": [{"$ne": [start, None]}, {"$ne": ["$started_at", start]}]},
        }
        matching = await coll.count_documents(flt)
        if not matching:
            continue
        counts["owners"] += 1
        counts["matching"] += matching
        if apply:
            res = await coll.update_many(flt, [{"$set": {"started_at": start}}])
            counts["updated"] += res.modified_count
    return counts


async def main():
    apply = "--apply" in sys.argv[1:]
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    counts = await backfill(client, apply)
    print(("Applied: " if apply else "Dry run (pass --apply to write): ") + str(counts))


if __name__ == "__main__":
    asyncio.run(main())
