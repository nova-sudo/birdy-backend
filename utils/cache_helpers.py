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
        # The call-center endpoint (routers/hotprospector.py get_hp_call_center)
        # sorts every query by created_at desc, but neither index above includes
        # it — every page fetch forced an in-memory SORT stage over the *entire*
        # matching set (all of a location's leads, or all of a user's leads
        # across every location on the "All Clients" view) before it could
        # skip/limit. These two cover both query shapes so the sort comes
        # straight from the index instead.
        # save_hotprospector_leads_to_collection upserts one UpdateOne per lead,
        # filtering on (user_id, ghl_location_id, lead_data.id). The index above
        # stops one field short, so each upsert IXSCANned every lead in the
        # location and fetched them all to test lead_data.id.
        # Measured live: 1,707 documents examined to return 1, on a location
        # holding 1,707 leads — so a full sync of that location is roughly
        # 1,707 x 1,707 document examinations. It is why
        # user_id_1_ghl_location_id_1 is the hottest index on the cluster at
        # ~957,000 ops, ahead of every read path.
        # Unique because (user_id, ghl_location_id, lead_data.id) IS the
        # logical key this collection is keyed by — verified zero duplicates
        # across all 54,792 rows before adding the constraint.
        ("hotprospector_leads.idx_hpl_user_loc_leadid", db["hotprospector_leads"].create_index([("user_id", 1), ("ghl_location_id", 1), ("lead_data.id", 1)], unique=True, name="idx_hpl_user_loc_leadid", background=True)),
        ("hotprospector_leads.idx_hpl_user_created", db["hotprospector_leads"].create_index([("user_id", 1), ("created_at", -1)], name="idx_hpl_user_created", background=True)),
        ("hotprospector_leads.idx_hpl_user_loc_created", db["hotprospector_leads"].create_index([("user_id", 1), ("ghl_location_id", 1), ("created_at", -1)], name="idx_hpl_user_loc_created", background=True)),
        ("hotprospector_member_daily.idx_hpmd_uniq", db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1), ("agentId", 1)], unique=True, name="idx_hpmd_uniq", background=True)),
        ("hotprospector_member_daily.idx_hpmd_date", db["hotprospector_member_daily"].create_index([("user_id", 1), ("date", 1)], name="idx_hpmd_date", background=True)),
        ("alerts.idx_al_created", db["alerts"].create_index([("user_id", 1), ("created_at", -1)], name="idx_al_created", background=True)),
        ("alerts.idx_al_id", db["alerts"].create_index([("user_id", 1), ("id", 1)], name="idx_al_id", background=True)),
        ("alerts.idx_al_status", db["alerts"].create_index([("user_id", 1), ("status", 1)], name="idx_al_status", background=True)),
        ("alert_notifications.idx_notif_trig", db["alert_notifications"].create_index([("user_id", 1), ("triggered_at", -1)], name="idx_notif_trig", background=True)),
        ("alert_notifications.idx_notif_read", db["alert_notifications"].create_index([("user_id", 1), ("read", 1)], name="idx_notif_read", background=True)),
        ("ghl_contacts.idx_ghl_date", db["ghl_contacts"].create_index([("user_id", 1), ("client_group_id", 1), ("contact_data.dateAdded", -1)], name="idx_ghl_date", background=True)),
        # The Clients-page facet (routers/client_groups.py, the counts branch)
        # matches on user_id + a contact_data.dateAdded RANGE and groups by
        # client_group_id — it never filters client_group_id. idx_ghl_date above
        # puts that field in the MIDDLE, and a compound index cannot skip an
        # interior key to reach the range beyond it, so the planner degrades to
        # a generic multi-interval scan (ixscan_generic).
        # Measured live on birdyaidev, one user, a 7-week window:
        #   idx_ghl_date            16,791 keys examined -> 62 groups returned
        # Ordering the same three fields by ESR (equality, then range, then the
        # group key) turns that into one contiguous range scan. Both indexes
        # earn their keep: idx_ghl_date still serves the per-group queries that
        # DO pin client_group_id (18,983 ops measured), which this one cannot
        # since its second key is the range.
        ("ghl_contacts.idx_ghl_user_date_group", db["ghl_contacts"].create_index([("user_id", 1), ("contact_data.dateAdded", 1), ("client_group_id", 1)], name="idx_ghl_user_date_group", background=True)),
        # The single worst query on the cluster before this index existed.
        # services/ghl_service.py::fetch_and_cache_ghl_data_optimized ends every
        # run with count_documents({user_id, location_id}), and /api/cron/ghl-tick
        # invokes it once a minute per claimed group. The only usable index was
        # location_contact_unique (location_id, contact_id): Mongo could IXSCAN on
        # location_id but had to FETCH every contact in the location just to test
        # user_id, then discard them all and return one number.
        # Measured live on birdyaidev via $queryStats: 19,063,597 documents
        # examined across 7,238 executions — ~2,634 scanned per single-number
        # result, and ~80% of every document scanned cluster-wide. That is the
        # "Query Targeting: Scanned Objects / Returned > 1000" Atlas alert.
        # With (user_id, location_id) the plan collapses to a pure COUNT_SCAN and
        # examines ZERO documents.
        ("ghl_contacts.idx_ghl_user_location", db["ghl_contacts"].create_index([("user_id", 1), ("location_id", 1)], name="idx_ghl_user_location", background=True)),
        # client_groups is looked up by bare {"id": ...} in a dozen call paths
        # (meta_refresh_manager, ghl_service, refresh jobs). user_id_1_id_1 above
        # cannot serve those — a compound index is unusable without its leading
        # field — so each one was a COLLSCAN of the whole collection (~17,600
        # executions measured, 34 docs scanned per 1 returned).
        ("client_groups.idx_cg_id", db["client_groups"].create_index("id", name="idx_cg_id", background=True)),
        # The every-minute meta-refresh claim scan in
        # services/meta_refresh_manager.py::schedule_stale_groups: it filters on
        # meta_ad_account_id/meta_token_error/last_meta_refresh and then SORTS by
        # last_meta_refresh, so with no index it COLLSCANned the collection AND
        # sorted the result in memory on every tick.
        #
        # PARTIAL, keyed on last_meta_refresh alone — this beat the alternatives
        # when measured (docs examined for the real query, 76-doc collection):
        #   no index                                76  + in-memory SORT
        #   (meta_ad_account_id, last_meta_refresh) 67  + in-memory SORT
        #   (last_meta_refresh) plain               38  sort from index
        #   (last_meta_refresh) partial             20  sort from index  <-- this
        # The compound index loses because meta_ad_account_id is matched by
        # $exists/$ne rather than equality, so it cannot pin a prefix and the
        # sort field stays unusable. Pushing that same predicate into a
        # partialFilterExpression instead keeps the index to the rows the query
        # can ever want, and leaves last_meta_refresh free to serve the sort.
        # Query eligibility: the filter says meta_ad_account_id $exists True,
        # which is a provable subset of the partial expression below.
        ("client_groups.idx_cg_meta_stale", db["client_groups"].create_index(
            [("last_meta_refresh", 1)],
            name="idx_cg_meta_stale",
            partialFilterExpression={"meta_ad_account_id": {"$exists": True}},
            background=True,
        )),
        # Counts filtered by all three fields fell back to the (user_id,
        # client_group_id, ...) index and re-fetched each doc to test
        # ad_account_id: 469 documents scanned per single-number result.
        ("facebook_leads.idx_fbl_user_acct_group", db["facebook_leads"].create_index([("user_id", 1), ("ad_account_id", 1), ("client_group_id", 1)], name="idx_fbl_user_acct_group", background=True)),
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
        # Windowed tag counts, one row per (group, preset) — see
        # services/ghl_tag_cache.py. Read once per Clients page load by
        # (user_id, preset); written by (group_id, preset).
        ("client_group_tag_cache.idx_cgtc_uniq", db["client_group_tag_cache"].create_index([("group_id", 1), ("preset", 1)], unique=True, name="idx_cgtc_uniq", background=True)),
        ("client_group_tag_cache.idx_cgtc_user_preset", db["client_group_tag_cache"].create_index([("user_id", 1), ("preset", 1)], name="idx_cgtc_user_preset", background=True)),
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
