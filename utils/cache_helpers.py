# utils/cache_helpers.py

from datetime import datetime
from typing import Any, Optional
from motor.motor_asyncio import AsyncIOMotorClient
import logging
import os

logger = logging.getLogger(__name__)


async def get_cached_data(
        user_id: str,
        data_path: str,  # e.g., "facebook.accounts.act_123.account_data"
        mongo_client: AsyncIOMotorClient,
        ttl_seconds: int = 300  # 5 minutes default
) -> Optional[Any]:
    """
    Generic cache getter with TTL check

    Args:
        user_id: User identifier
        data_path: Dot-notation path (e.g., "facebook.accounts.act_123.leads")
        mongo_client: MongoDB client
        ttl_seconds: Cache validity in seconds

    Returns:
        Cached data if valid, None otherwise
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdy")]
    user_doc = await db["users"].find_one({"user_id": user_id})

    if not user_doc:
        return None

    # Navigate nested path
    data_doc = user_doc.get("integrations", {})
    for key in data_path.split('.'):
        data_doc = data_doc.get(key, {})

    if not data_doc or not isinstance(data_doc, dict):
        return None

    updated_at = data_doc.get("updated_at")
    if not updated_at:
        return None

    # Check if cache is still valid
    cache_age = (datetime.now() - updated_at).total_seconds()

    if cache_age < ttl_seconds:
        logger.info(
            f"✅ Cache hit for {data_path} "
            f"(age: {cache_age:.0f}s / {ttl_seconds}s)"
        )
        return data_doc.get("data")
    else:
        logger.info(
            f"⏰ Cache expired for {data_path} "
            f"(age: {cache_age:.0f}s / {ttl_seconds}s)"
        )
        return None


async def save_cached_data(
        user_id: str,
        data_path: str,
        data: Any,
        mongo_client: AsyncIOMotorClient
):
    """
    Generic cache setter

    Args:
        user_id: User identifier
        data_path: Dot-notation path
        data: Data to cache
        mongo_client: MongoDB client
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdy")]

    # Build nested update path
    update_path = f"integrations.{data_path}"

    await db["users"].update_one(
        {"user_id": user_id},
        {
            "$set": {
                f"{update_path}.data": data,
                f"{update_path}.updated_at": datetime.now(),
                "updated_at": datetime.now()
            }
        },
        upsert=True
    )

    logger.info(f"💾 Cached data at {update_path}")


async def create_performance_indexes(mongo_client: AsyncIOMotorClient):
    """Create indexes for better query performance"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdy")]

        # Index for user lookups
        await db["users"].create_index("user_id", unique=True)

        # Compound index for integration data lookups
        await db["users"].create_index([
            ("user_id", 1),
            ("integrations.facebook.accounts", 1)
        ])

        # Index for client groups
        await db["client_groups"].create_index([
            ("user_id", 1),
            ("id", 1)
        ])

        # Index for webhook data
        await db["webhooks"].create_index([
            ("user_id", 1),
            ("event_type", 1),
            ("received_at", -1)
        ])

        # Index for HotProspector leads
        await db["hotprospector_leads"].create_index([
            ("user_id", 1),
            ("ghl_location_id", 1)
        ])

        logger.info("✅ Performance indexes created successfully")

    except Exception as e:
        logger.error(f"Error creating indexes: {str(e)}")