"""
One-time backfill script: adds match_keys to all existing documents
in ghl_contacts, facebook_leads, and hotprospector_leads.

Also creates MongoDB indexes on match_keys for fast join queries.

Run with:  python -m scripts.backfill_match_keys
"""

import asyncio
import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from utils.phone_normalize import compute_match_keys
from core.database import DB_NAME

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))
BATCH_SIZE = 500


async def backfill_collection(db, collection_name: str, email_path: str, phone_path: str):
    """Backfill match_keys for all documents in a collection."""
    col = db[collection_name]
    total = await col.count_documents({})
    logger.info(f"Backfilling {collection_name}: {total} documents")

    cursor = col.find(
        {"match_keys": {"$exists": False}},
        {email_path: 1, phone_path: 1, "_id": 1}
    )

    ops = []
    processed = 0

    async for doc in cursor:
        # Navigate nested paths
        email = doc
        for key in email_path.split("."):
            email = email.get(key, {}) if isinstance(email, dict) else None
        phone = doc
        for key in phone_path.split("."):
            phone = phone.get(key, {}) if isinstance(phone, dict) else None

        keys = compute_match_keys(
            email if isinstance(email, str) else None,
            phone if isinstance(phone, str) else None,
        )

        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"match_keys": keys}}
        ))

        if len(ops) >= BATCH_SIZE:
            await col.bulk_write(ops, ordered=False)
            processed += len(ops)
            logger.info(f"  {collection_name}: {processed}/{total}")
            ops = []

    if ops:
        await col.bulk_write(ops, ordered=False)
        processed += len(ops)

    logger.info(f"  {collection_name}: done — {processed} documents updated")


async def create_indexes(db):
    """Create match_keys indexes on all three collections."""
    for col_name in ["ghl_contacts", "facebook_leads", "hotprospector_leads"]:
        col = db[col_name]
        await col.create_index("match_keys", name="idx_match_keys", background=True)
        logger.info(f"Created match_keys index on {col_name}")


async def main():
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    logger.info(f"Connected to MongoDB: {DB_NAME}")

    # Backfill each collection
    await backfill_collection(db, "ghl_contacts", "contact_data.email", "contact_data.phone")
    await backfill_collection(db, "facebook_leads", "lead_data.email", "lead_data.phone_number")
    await backfill_collection(db, "hotprospector_leads", "lead_data.email", "lead_data.phone")

    # Create indexes
    await create_indexes(db)

    logger.info("Backfill complete!")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
