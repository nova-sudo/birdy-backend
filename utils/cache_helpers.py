# utils/cache_helpers.py

import asyncio
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
            f"Cache hit for {data_path} "
            f"(age: {cache_age:.0f}s / {ttl_seconds}s)"
        )
        return data_doc.get("data")
    else:
        logger.info(
            f"Cache expired for {data_path} "
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

    logger.info(f"Cached data at {update_path}")


async def create_performance_indexes(mongo_client: AsyncIOMotorClient):
    """Create indexes for better query performance.
    All fired concurrently with background=True so cold starts are fast."""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdy")]

        await asyncio.gather(
            db["users"].create_index("user_id", unique=True, name="idx_users_uid", background=True),
            db["users"].create_index([("user_id", 1), ("integrations.facebook.accounts", 1)], name="idx_users_fb", background=True),
            db["client_groups"].create_index([("user_id", 1), ("id", 1)], name="idx_cg_uid", background=True),
            db["webhooks"].create_index([("user_id", 1), ("event_type", 1), ("received_at", -1)], name="idx_wh_evt", background=True),
            db["hotprospector_leads"].create_index([("user_id", 1), ("ghl_location_id", 1)], name="idx_hp_loc", background=True),
            db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1), ("agentId", 1)], unique=True, name="idx_hpmd_uniq", background=True),
            db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1)], name="idx_hpmd_date", background=True),
            db["alerts"].create_index([("user_id", 1), ("created_at", -1)], name="idx_al_created", background=True),
            db["alerts"].create_index([("user_id", 1), ("id", 1)], name="idx_al_id", background=True),
            db["alerts"].create_index([("user_id", 1), ("status", 1)], name="idx_al_status", background=True),
            db["alert_notifications"].create_index([("user_id", 1), ("triggered_at", -1)], name="idx_notif_trig", background=True),
            db["alert_notifications"].create_index([("user_id", 1), ("read", 1)], name="idx_notif_read", background=True),
            db["ghl_contacts"].create_index([("user_id", 1), ("client_group_id", 1), ("contact_data.dateAdded", -1)], name="idx_ghl_date", background=True),
        )

        logger.info("Performance indexes ensured")

    except Exception as e:
        logger.error(f"Error creating indexes: {str(e)}")
