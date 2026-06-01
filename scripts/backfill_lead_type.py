"""
One-time backfill: stamp `lead_type` on every existing document in the
ghl_contacts collection using the same rule the refresh / read paths now
apply (see services/contact_classifier.py).

Without this, contacts that landed before the rule shipped will continue
to report as "lead" on read (because the API now calls the classifier on
read too, which fixes the response shape) but their stored documents
won't have a `lead_type` field — so the Mongo aggregations used for the
new Leads-page count cards return zeros for those rows.

Idempotent: every run reclassifies every document. Run again whenever you
want to re-sync (e.g. after tweaking the classifier rule).

Also creates an index on `lead_type` so the count aggregations stay fast
as the collection grows.

Run with:  python -m scripts.backfill_lead_type
"""

import asyncio
import logging
import os
import sys
from collections import Counter

# Add project root to path so absolute imports work when invoked as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne

from core.database import DB_NAME
from services.contact_classifier import classify_contact_type

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
BATCH_SIZE = 500


async def backfill_lead_type(db) -> dict:
    """
    Reclassify every doc in ghl_contacts and write `lead_type` to it.

    Returns counters: {processed, lead, contact, unchanged}.
    """
    col = db["ghl_contacts"]
    total = await col.count_documents({})
    logger.info(f"ghl_contacts: {total} documents to process")

    # Project only what we need to compute classification, plus the existing
    # lead_type so we can skip writes that wouldn't change anything.
    cursor = col.find(
        {},
        projection={
            "_id": 1,
            "contact_data.attributionSource": 1,
            "lead_type": 1,
        },
    )

    ops: list[UpdateOne] = []
    counters = Counter()
    processed = 0

    async for doc in cursor:
        contact_data = doc.get("contact_data") or {}
        new_type = classify_contact_type(contact_data)
        old_type = doc.get("lead_type")

        counters[new_type] += 1
        counters["processed"] += 1

        if old_type == new_type:
            counters["unchanged"] += 1
            continue

        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"lead_type": new_type}},
        ))

        if len(ops) >= BATCH_SIZE:
            await col.bulk_write(ops, ordered=False)
            processed += len(ops)
            ops = []
            logger.info(f"  written: {processed} / {total - counters['unchanged']}")

    if ops:
        await col.bulk_write(ops, ordered=False)
        processed += len(ops)

    counters["written"] = processed
    return dict(counters)


async def create_lead_type_index(db) -> None:
    """Index on `lead_type` for fast count aggregations on the Leads page."""
    col = db["ghl_contacts"]
    await col.create_index("lead_type", name="idx_lead_type", background=True)
    logger.info("Created lead_type index on ghl_contacts")


async def main() -> None:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    logger.info(f"Connected to MongoDB: {DB_NAME}")

    counters = await backfill_lead_type(db)
    await create_lead_type_index(db)

    logger.info(
        "Backfill complete — processed: %s | leads: %s | contacts: %s | "
        "unchanged (already correct): %s | written: %s",
        counters.get("processed", 0),
        counters.get("lead", 0),
        counters.get("contact", 0),
        counters.get("unchanged", 0),
        counters.get("written", 0),
    )

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
