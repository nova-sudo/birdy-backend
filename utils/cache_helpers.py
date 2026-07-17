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
    All fired concurrently with background=True so cold starts are fast.

    Each create_index call is awaited individually (return_exceptions=True)
    rather than one bare asyncio.gather(): a single conflict — e.g. an index
    with the same keys but a different (often auto-generated) name already
    existing — must not abort/mask the rest of the batch, and every outcome
    is logged so a real failure is visible instead of silent.
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdy")]

    index_calls = [
        # Names below match indexes that already exist in production under Mongo's
        # auto-generated default name (created before an explicit name= was added
        # here) — using that same name makes this call a true no-op instead of an
        # IndexOptionsConflict (same keys, different requested name).
        ("users.user_id_1", db["users"].create_index("user_id", unique=True, name="user_id_1", background=True)),
        ("users.user_id_1_integrations.facebook.accounts_1", db["users"].create_index([("user_id", 1), ("integrations.facebook.accounts", 1)], name="user_id_1_integrations.facebook.accounts_1", background=True)),
        ("client_groups.user_id_1_id_1", db["client_groups"].create_index([("user_id", 1), ("id", 1)], name="user_id_1_id_1", background=True)),
        ("webhooks.user_id_1_event_type_1_received_at_-1", db["webhooks"].create_index([("user_id", 1), ("event_type", 1), ("received_at", -1)], name="user_id_1_event_type_1_received_at_-1", background=True)),
        ("hotprospector_leads.user_id_1_ghl_location_id_1", db["hotprospector_leads"].create_index([("user_id", 1), ("ghl_location_id", 1)], name="user_id_1_ghl_location_id_1", background=True)),
        ("hotprospector_member_daily.idx_hpmd_uniq", db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1), ("agentId", 1)], unique=True, name="idx_hpmd_uniq", background=True)),
        ("hotprospector_member_daily.idx_hpmd_date", db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1)], name="idx_hpmd_date", background=True)),
        ("alerts.idx_al_created", db["alerts"].create_index([("user_id", 1), ("created_at", -1)], name="idx_al_created", background=True)),
        ("alerts.idx_al_id", db["alerts"].create_index([("user_id", 1), ("id", 1)], name="idx_al_id", background=True)),
        ("alerts.idx_al_status", db["alerts"].create_index([("user_id", 1), ("status", 1)], name="idx_al_status", background=True)),
        ("alert_notifications.idx_notif_trig", db["alert_notifications"].create_index([("user_id", 1), ("triggered_at", -1)], name="idx_notif_trig", background=True)),
        ("alert_notifications.idx_notif_read", db["alert_notifications"].create_index([("user_id", 1), ("read", 1)], name="idx_notif_read", background=True)),
        ("ghl_contacts.idx_ghl_date", db["ghl_contacts"].create_index([("user_id", 1), ("client_group_id", 1), ("contact_data.dateAdded", -1)], name="idx_ghl_date", background=True)),
        # meta_refresh_jobs previously had NO indexes beyond the default _id_, so
        # every read in services/meta_refresh_manager.py (job_id lookups, the
        # per-group "latest job" query, the stale-claim atomic $or, and the
        # stuck-retry scan) was a full collection scan — the direct cause of the
        # "Query Targeting: Scanned Objects / Returned > 1000" Atlas alert
        # (measured live: ~18,000 docs scanned per 1-doc job_id lookup, on a
        # cron that runs every minute). These cover every query shape in that file.
        ("meta_refresh_jobs.idx_mrj_job_id", db["meta_refresh_jobs"].create_index("job_id", name="idx_mrj_job_id", background=True)),
        ("meta_refresh_jobs.idx_mrj_group_created", db["meta_refresh_jobs"].create_index([("group_id", 1), ("created_at", -1)], name="idx_mrj_group_created", background=True)),
        ("meta_refresh_jobs.idx_mrj_group_status", db["meta_refresh_jobs"].create_index([("group_id", 1), ("status", 1)], name="idx_mrj_group_status", background=True)),
        ("meta_refresh_jobs.idx_mrj_status_retry", db["meta_refresh_jobs"].create_index([("status", 1), ("next_retry_at", 1), ("created_at", 1)], name="idx_mrj_status_retry", background=True)),
        ("waitlist.idx_waitlist_email", db["waitlist"].create_index("email", unique=True, name="idx_waitlist_email", background=True)),
    ]

    results = await asyncio.gather(*(coro for _, coro in index_calls), return_exceptions=True)

    failed = []
    for (label, _), result in zip(index_calls, results):
        if isinstance(result, Exception):
            failed.append(label)
            logger.warning(f"Index ensure failed for {label}: {result}")

    if failed:
        logger.warning(f"Performance indexes ensured with {len(failed)} failure(s): {failed}")
    else:
        logger.info("Performance indexes ensured")
