"""
Backfill script — re-parses GHL call_logs rows that came in before the
normalizer read GHL's standard webhook shape (`location.id`, `customData`).

Those rows were stored with no user_id, direction or recording_url, so no
lead page could show them. For each source="ghl" row with a raw_payload and
no user_id, this re-runs services.call_logs_service.normalize_call_log on the
stored payload, resolves the owner from the location, and fills started_at
from received_at (flagged started_at_estimated) when the payload has no time.

Dry run by default; pass --apply to write.

Run with:  python -m scripts.backfill_ghl_call_logs [--apply]
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME
from services.call_logs_service import normalize_call_log, resolve_user_id_from_location


async def backfill(mongo_client, apply: bool) -> dict:
    coll = mongo_client[DB_NAME]["call_logs"]
    rows = await coll.find(
        {"source": "ghl", "user_id": None, "raw_payload": {"$type": "object"}}).to_list(None)
    counts = {"rows": len(rows), "owner_found": 0, "with_recording": 0, "updated": 0}
    for row in rows:
        normalized = normalize_call_log("ghl", row["raw_payload"])
        user_id = await resolve_user_id_from_location(normalized.get("location_id"), mongo_client)
        if not user_id:
            continue
        counts["owner_found"] += 1
        counts["with_recording"] += bool(normalized.get("recording_url"))
        update = {**normalized, "user_id": user_id}
        if not update.get("started_at"):
            update["started_at"] = row.get("received_at")
            update["started_at_estimated"] = True
        if apply:
            await coll.update_one({"_id": row["_id"], "user_id": None}, {"$set": update})
            counts["updated"] += 1
    return counts


async def main():
    apply = "--apply" in sys.argv[1:]
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    counts = await backfill(client, apply)
    print(("Applied: " if apply else "Dry run (pass --apply to write): ") + str(counts))


if __name__ == "__main__":
    asyncio.run(main())
