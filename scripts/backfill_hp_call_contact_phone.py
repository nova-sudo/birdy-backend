"""
Backfill script — fixes contact_phone on outbound HotProspector call_logs rows.

_persist_hp_calls_to_call_logs used to store `from_number or to_number`, which
on an outbound call is the agency's own line, so the lead page (which matches
calls to a lead by phone) never found outbound calls. New writes store the
lead's end (services.hp_service._hp_lead_number); this rewrites existing rows
from the stored raw_payload.to_number in one server-side update.

Dry run by default; pass --apply to write.

Run with:  python -m scripts.backfill_hp_call_contact_phone [--apply]
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.database import DB_NAME

# Outbound HP rows whose stored phone isn't the number they dialled.
FILTER = {
    "source": "hotprospector",
    "direction": "outbound",
    "raw_payload.to_number": {"$type": "string", "$ne": ""},
    "$expr": {"$ne": ["$contact_phone", "$raw_payload.to_number"]},
}
UPDATE = [{"$set": {"contact_phone": "$raw_payload.to_number"}}]


async def backfill(mongo_client, apply: bool) -> dict:
    coll = mongo_client[DB_NAME]["call_logs"]
    matching = await coll.count_documents(FILTER)
    updated = (await coll.update_many(FILTER, UPDATE)).modified_count if apply else 0
    return {"matching": matching, "updated": updated}


async def main():
    apply = "--apply" in sys.argv[1:]
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    counts = await backfill(client, apply)
    print(("Applied: " if apply else "Dry run (pass --apply to write): ") + str(counts))


if __name__ == "__main__":
    asyncio.run(main())
