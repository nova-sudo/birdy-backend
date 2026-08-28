"""
services/ghl_service.py
-----------------------
GoHighLevel service / helper functions extracted from main.py.
"""

import asyncio
import logging
import uuid
from collections import Counter
from datetime import datetime

from utils.phone_normalize import compute_match_keys
from typing import Dict, List, Tuple

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.gohighlevel import (
    ghl_integration,
    get_subaccount_tokens,
)
from services.contact_classifier import classify_contact_type

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# cache_ghl_opp_stats_all_presets  — fetch & cache opp stats per preset
# ------------------------------------------------------------------
async def cache_ghl_opp_stats_all_presets(
    group_id: str,
    location_id: str,
    access_token: str,
    mongo_client,
):
    """
    Fetch every opportunity for the location ONCE, then derive stats for all 13
    date presets in-memory from lastStatusChangeAt / createdAt. This is much
    cheaper than 13 API calls and correctly reflects "activity in window"
    semantics (old per-preset calls filtered by creation date only, which for
    long-cycle pipelines returned near-zero for every windowed preset).

    Failure handling:
      - If the full opp fetch fails, we write NOTHING (preserves existing cache).
      - If the opp list comes back empty and the existing "maximum" cache is
        non-empty, we also skip writes (safety against intermittent API issues).
    """
    from core.constants import META_CACHE_PRESETS, ghl_date_bounds
    from integrations.gohighlevel import compute_opp_stats

    db = mongo_client[DB_NAME]
    groups_col = db["client_groups"]

    ok, opps = await ghl_integration.fetch_all_opportunities(location_id, access_token)
    if not ok:
        logger.warning("Opp fetch failed for %s — preserving existing cache", location_id)
        return

    # Safety: if fetch returned zero opps but we already had data, assume a
    # transient/partial failure and skip rather than wipe good stats.
    if not opps:
        existing = await groups_col.find_one(
            {"id": group_id}, {"ghl_opp_cache.maximum": 1}
        )
        existing_max = (existing or {}).get("ghl_opp_cache", {}).get("maximum", {}) or {}
        if (existing_max.get("total_opportunities") or 0) > 0:
            logger.warning(
                "Opp fetch returned 0 opps for %s but cache has %s — skipping to preserve",
                location_id, existing_max.get("total_opportunities"),
            )
            return

    # Derive stats for every preset from the one opp list
    opp_cache: dict = {}
    for preset in META_CACHE_PRESETS:
        start_iso, end_iso = ghl_date_bounds(preset)
        opp_cache[preset] = compute_opp_stats(opps, start_iso, end_iso)

    # Write all presets in one update via dot notation so failed presets (if any)
    # wouldn't clobber the whole subdocument — belt-and-braces.
    update_fields = {f"ghl_opp_cache.{preset}": stats for preset, stats in opp_cache.items()}
    update_fields["ghl_opp_cache.updated_at"] = datetime.utcnow().isoformat()

    await groups_col.update_one(
        {"id": group_id},
        {"$set": update_fields},
    )

    # Also mirror "maximum" into the legacy location for backward compat
    max_stats = opp_cache.get("maximum") or {}
    if (max_stats.get("total_opportunities") or 0) > 0:
        await groups_col.update_one(
            {"id": group_id},
            {"$set": {"gohighlevel_cache.metrics.opportunity_stats": max_stats}},
        )

    logger.info(
        "Cached GHL opp stats for group %s: %d opps, %d presets, maximum won=%d",
        group_id, len(opps), len(META_CACHE_PRESETS), max_stats.get("won", 0),
    )


# ------------------------------------------------------------------
# cache_ghl_cohort_funnel_all_presets — the dashboard's close-rate funnel
# ------------------------------------------------------------------
async def cache_ghl_cohort_funnel_all_presets(
    group_id: str,
    user_id: str,
    location_id: str,
    mongo_client,
):
    """
    Derive the cohort funnel for every preset and cache it per preset.

    Reads the group's contacts once and buckets them into all 13 windows in
    memory, mirroring cache_ghl_opp_stats_all_presets. That single read is the
    reason this belongs on the refresh path rather than in the
    /api/client-groups handler: counting ghl_contacts per request was the
    largest single source of collection scans on this cluster.

    Failure handling matches the opp-stats cache — if the contact read comes
    back empty while the stored cohort is non-empty, assume a transient problem
    and write nothing rather than zeroing a good cache.
    """
    from core.constants import META_CACHE_PRESETS, ghl_date_bounds
    from integrations.gohighlevel import compute_cohort_funnel

    db = mongo_client[DB_NAME]
    groups_col = db["client_groups"]

    contacts = await db["ghl_contacts"].find(
        {"user_id": user_id, "client_group_id": group_id},
        {
            "contact_data.dateAdded": 1,
            "contact_data.opportunities": 1,
            "match_keys": 1,
            "_id": 0,
        },
    ).to_list(length=None)

    if not contacts:
        existing = await groups_col.find_one(
            {"id": group_id}, {"ghl_funnel_cache.maximum": 1}
        )
        existing_max = (existing or {}).get("ghl_funnel_cache", {}).get("maximum", {}) or {}
        if (existing_max.get("leads") or 0) > 0:
            logger.warning(
                "Cohort funnel read 0 contacts for %s but cache holds %s leads — "
                "skipping to preserve",
                group_id, existing_max.get("leads"),
            )
            return

    # Every match key HotProspector has a call logged against, gathered once so
    # the per-contact "was this lead called?" test is a set lookup rather than
    # a query per contact.
    called_keys: set = set()
    if location_id:
        cursor = db["hotprospector_leads"].find(
            {
                "user_id": user_id,
                "ghl_location_id": location_id,
                "lead_data.call_logs_count": {"$gt": 0},
            },
            {"match_keys": 1, "_id": 0},
        )
        async for doc in cursor:
            called_keys.update(doc.get("match_keys") or [])

    funnel_cache = {
        preset: compute_cohort_funnel(contacts, called_keys, *ghl_date_bounds(preset))
        for preset in META_CACHE_PRESETS
    }

    update_fields = {f"ghl_funnel_cache.{p}": s for p, s in funnel_cache.items()}
    update_fields["ghl_funnel_cache.updated_at"] = datetime.utcnow().isoformat()
    await groups_col.update_one({"id": group_id}, {"$set": update_fields})

    maximum = funnel_cache.get("maximum") or {}
    logger.info(
        "Cached GHL cohort funnel for group %s: %d contacts, %d called-keys, "
        "lifetime leads=%d closes=%d",
        group_id, len(contacts), len(called_keys),
        maximum.get("leads", 0), maximum.get("closes", 0),
    )


# ------------------------------------------------------------------
# fetch_ghl_contacts_for_group  (line ~2790)
# ------------------------------------------------------------------
async def fetch_ghl_contacts_for_group(
        ghl_location_id: str,
        current_user: str,
        mongo_client,
        subaccount_tokens: dict
):
    """
    Fetch GHL contact COUNT for a single group (optimized for cache).
    Returns: (location_id, contact_count, was_refreshed)
    """
    try:
        location_data = subaccount_tokens.get(ghl_location_id, {})

        # Get cached count from token data
        cached_count = location_data.get("contact_count", 0)
        contacts_updated_at = location_data.get("contacts_updated_at")

        # Check cache validity (5 minutes)
        cache_valid = contacts_updated_at and (
                datetime.now() - contacts_updated_at
        ).total_seconds() < 300

        # If cache valid, return cached count
        if cache_valid:
            return ghl_location_id, cached_count, False

        # Fetch fresh count if stale
        if location_data.get("access_token"):
            success, result = await ghl_integration.fetch_location_contacts_client_groups(
                ghl_location_id,
                location_data.get("access_token"),
                limit=100
            )

            if success and isinstance(result, int):
                contact_count = result

                # Update user doc with new count
                db = mongo_client[DB_NAME]
                await db["users"].update_one(
                    {"user_id": current_user},
                    {
                        "$set": {
                            f"integrations.gohighlevel.subaccounts.{ghl_location_id}.contact_count": contact_count,
                            f"integrations.gohighlevel.subaccounts.{ghl_location_id}.contacts_updated_at": datetime.now()
                        }
                    }
                )

                logger.info(f"Updated contact count to {contact_count} for {ghl_location_id}")
                return ghl_location_id, contact_count, True
            else:
                logger.warning(f"Failed to fetch count for {ghl_location_id}, using cached")
                return ghl_location_id, cached_count, False

        return ghl_location_id, cached_count, False

    except Exception as e:
        logger.error(f"Error fetching GHL contact count for {ghl_location_id}: {str(e)}")
        return ghl_location_id, 0, False


# ------------------------------------------------------------------
# refresh_ghl_data_for_user  (line 3110 — runtime version)
# ------------------------------------------------------------------
async def refresh_ghl_data_for_user(user_id: str):
    """Refresh GHL data for a specific user"""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        client_groups_collection = db["client_groups"]
        contacts_collection = db["ghl_contacts"]

        groups = await client_groups_collection.find(
            {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}}
        ).to_list(None)

        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)

        tasks = []
        for group in groups:
            if group.get("ghl_location_id"):
                tasks.append(
                    fetch_ghl_contacts_for_group(
                        group["ghl_location_id"],
                        user_id,
                        mongo_client,
                        subaccount_tokens
                    )
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, group in enumerate(groups):
            if not isinstance(results[i], Exception):
                location_id, contact_count, was_refreshed = results[i]

                # Safety check: ensure contact_count is an integer
                if not isinstance(contact_count, int):
                    logger.warning(f"Contact count for {location_id} ")

                location_data = subaccount_tokens.get(location_id, {})

                # Fetch & cache opp stats for ALL date presets
                access_token = location_data.get("access_token")
                # Start with existing opp stats so we never overwrite good data with zeros
                existing_doc = await client_groups_collection.find_one(
                    {"id": group["id"]},
                    {"gohighlevel_cache.metrics.opportunity_stats": 1, "ghl_opp_cache.maximum": 1}
                )
                opp_stats = (existing_doc or {}).get("gohighlevel_cache", {}).get("metrics", {}).get("opportunity_stats", {
                    "won": 0, "lost": 0, "open": 0, "abandoned": 0,
                    "total_opportunities": 0, "won_revenue": 0.0, "total_revenue": 0.0,
                })
                if access_token:
                    try:
                        await cache_ghl_opp_stats_all_presets(
                            group["id"], location_id, access_token, mongo_client
                        )
                        # Read back "maximum" for the legacy cache
                        g_doc = await client_groups_collection.find_one(
                            {"id": group["id"]}, {"ghl_opp_cache.maximum": 1}
                        )
                        if g_doc and g_doc.get("ghl_opp_cache", {}).get("maximum", {}).get("total_opportunities", 0) > 0:
                            opp_stats = g_doc["ghl_opp_cache"]["maximum"]
                    except Exception:
                        pass  # Keep existing opp_stats

                # The dashboard funnel reads contacts, not the opportunity
                # feed, so it is cached separately and must not be skipped
                # when the group has no GHL access token.
                try:
                    await cache_ghl_cohort_funnel_all_presets(
                        group["id"], user_id, location_id, mongo_client
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to cache cohort funnel for %s: %s", group["id"], e
                    )

                cache_data = {
                    "location_id": location_id,
                    "name": location_data.get("name", "Unknown Location"),
                    "address": location_data.get("address"),
                    "metrics": {
                        "total_contacts": contact_count,
                        "opportunity_stats": opp_stats,
                    }
                }

                update_fields = {
                    "gohighlevel_cache": cache_data
                }

                # Only update refresh time if we actually fetched new data
                if was_refreshed:
                    update_fields["last_ghl_refresh"] = datetime.utcnow()

                await client_groups_collection.update_one(
                    {"id": group["id"]},
                    {"$set": update_fields}
                )


# ------------------------------------------------------------------
# fetch_and_cache_multiple_ghl_locations  (line ~3966)
# ------------------------------------------------------------------
async def fetch_and_cache_multiple_ghl_locations(
        user_id: str,
        mongo_client,
        is_initial_load: bool = False
):
    """
    PARALLEL: Fetch GHL contacts for all locations of a user in parallel.

    Each location's contacts are stored as separate documents with compound indexes.

    Returns:
        dict: {"total_locations": int, "successful": int, "failed": int}
    """
    try:
        db = mongo_client[DB_NAME]
        client_groups_collection = db["client_groups"]

        # Get all client groups for this user with GHL locations
        groups = await client_groups_collection.find({
            "user_id": user_id,
            "ghl_location_id": {"$exists": True, "$ne": None}
        }).to_list(None)

        if not groups:
            logger.info(f"No GHL locations found for user {user_id}")
            return {
                "total_locations": 0,
                "successful": 0,
                "failed": 0
            }

        logger.info(f"Starting parallel fetch for {len(groups)} locations")

        # Create parallel fetch tasks
        fetch_tasks = [
            fetch_and_cache_ghl_data_optimized(
                group["id"],
                group["ghl_location_id"],
                user_id,
                mongo_client,
                is_initial_load=is_initial_load
            )
            for group in groups
        ]

        # Execute all fetches in parallel
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # Log results
        success_count = sum(1 for r in results if not isinstance(r, Exception))
        failure_count = len(results) - success_count

        logger.info(
            f"Parallel fetch complete: "
            f"{success_count} succeeded, {failure_count} failed"
        )

        return {
            "total_locations": len(groups),
            "successful": success_count,
            "failed": failure_count
        }

    except Exception as e:
        logger.error(f"Error in parallel fetch: {e}", exc_info=True)
        raise


# ------------------------------------------------------------------
# fetch_and_cache_ghl_data_optimized  (line ~5967)
# ------------------------------------------------------------------
async def fetch_and_cache_ghl_data_optimized(
        group_id: str,
        ghl_location_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    CORRECTED: Fetch GHL contacts using NEW /contacts/search endpoint

    ACTUAL API RESPONSE:
    {
        "contacts": [...],
        "total": 2152,
        "traceId": "..."
    }

    We calculate pagination metadata ourselves since API only returns total.

    Args:
        group_id: Client group ID
        ghl_location_id: GHL location ID
        user_id: User ID
        mongo_client: MongoDB client
        is_initial_load: If True, fetch all. If False, incremental update
    """
    try:
        db = mongo_client[DB_NAME]
        contacts_collection = db["ghl_contacts"]
        client_groups_collection = db["client_groups"]

        # Get location info
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_data = subaccount_tokens.get(ghl_location_id, {})

        access_token = location_data.get("access_token")
        if not access_token:
            logger.warning(f"No access token for GHL location {ghl_location_id}")
            return

        location_name = location_data.get("name", "Unknown Location")
        location_address = location_data.get("address", "")

        # Get client group info
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        # ============================================
        # STEP 1: Determine last known contact for incremental refresh
        # ============================================
        last_known_contact_date = None
        last_known_contact_id = None

        if not is_initial_load:
            last_contact = await contacts_collection.find_one(
                {
                    "user_id": user_id,
                    "location_id": ghl_location_id
                },
                sort=[("contact_data.dateAdded", -1)]
            )

            if last_contact:
                last_known_contact_date = last_contact.get("contact_data", {}).get("dateAdded")
                last_known_contact_id = last_contact.get("contact_id")

                if last_known_contact_date:
                    logger.info(
                        f"INCREMENTAL: Last known contact from {last_known_contact_date} "
                        f"(ID: {last_known_contact_id})"
                    )

        # FULL LOAD no longer deletes up front. Every contact it sees gets
        # upserted in place and tagged with this run's sync_batch_id (STEP 4);
        # stale contacts (still tagged with an older/no batch id) are only
        # pruned once the fetch is confirmed complete (STEP 4b) — so a fetch
        # that dies partway through (timeout, GHL error, ...) never leaves the
        # location with fewer contacts than it had before this run.
        sync_batch_id = str(uuid.uuid4()) if is_initial_load else None
        pagination_failed = False

        if is_initial_load:
            logger.info(f"FULL LOAD: Fetching all contacts for {location_name}")

        # ============================================
        # STEP 2: Fetch contacts using NEW page-based API
        # ============================================
        page = 1
        total_contacts_processed = 0
        total_new_contacts = 0
        total_updated_contacts = 0
        should_continue = True

        # Tag counter for metrics
        tag_counter = Counter()

        # Track total (we'll know after first request)
        total_contacts_api = None
        total_pages = None

        while should_continue:
            try:
                # Fetch page using NEW search endpoint
                success, result = await ghl_integration.fetch_contacts_search(
                    ghl_location_id,
                    access_token,
                    page=page,
                    limit=500
                )

                if not success:
                    logger.error(f"Failed to fetch page {page}: {result.get('error')}")
                    pagination_failed = True
                    break

                # Extract from ACTUAL response format
                contacts_batch = result.get("contacts", [])
                total_contacts_api = result.get("total", 0)
                meta = result.get("meta", {})  # Our calculated metadata

                # Get pagination info from our calculated meta
                if total_pages is None:
                    total_pages = meta.get("totalPages", 1)
                    logger.info(
                        f"Total pages: {total_pages}, Total contacts: {total_contacts_api}"
                    )

                if not contacts_batch:
                    logger.info(f"No more contacts at page {page}")
                    break

                # ============================================
                # STEP 3: Check for last known contact (incremental mode)
                # ============================================
                contacts_to_save = []

                for contact in contacts_batch:
                    contact_id = contact.get("id")
                    date_added = contact.get("dateAdded")

                    # Stop condition: hit last known contact
                    if not is_initial_load and last_known_contact_id:
                        if contact_id == last_known_contact_id:
                            logger.info(
                                f"OPTIMIZATION: Found last known contact at page {page}"
                            )
                            should_continue = False
                            break

                        # Also check by date
                        if date_added and last_known_contact_date:
                            try:
                                contact_date = datetime.fromisoformat(
                                    date_added.replace('Z', '+00:00')
                                )
                                last_date = datetime.fromisoformat(
                                    last_known_contact_date.replace('Z', '+00:00')
                                )

                                if contact_date <= last_date:
                                    logger.info(
                                        f"OPTIMIZATION: Hit older data at page {page}"
                                    )
                                    should_continue = False
                                    break
                            except Exception as e:
                                logger.debug(f"Date comparison error: {e}")

                    # Contact is new, add to save list
                    contacts_to_save.append(contact)

                    # Count tags for metrics
                    tags = contact.get("tags", [])
                    if tags:
                        for tag in tags:
                            tag_counter[tag] += 1

                # ============================================
                # STEP 4: Save contacts to database
                # ============================================
                # Always an upsert keyed on (location_id, contact_id) — the pair
                # `location_contact_unique` enforces as unique — never a raw
                # insert. FULL LOAD used to insert_many() fresh documents after
                # wiping the location's contacts up front; now that the wipe
                # only happens *after* a confirmed-complete fetch (STEP 4b
                # below), a FULL LOAD page can land on contacts that still have
                # their old documents in place, and insert_many() would throw
                # E11000 duplicate key errors on every one of them. Upserting
                # in place is correct either way: FULL LOAD refreshes each
                # contact's data and tags it with this run's sync_batch_id;
                # INCREMENTAL does the same minus the batch tag.
                if contacts_to_save:
                    from pymongo import UpdateOne
                    bulk_operations = []

                    for contact in contacts_to_save:
                        contact_id = contact.get("id")

                        contact_doc = {
                            "user_id": user_id,
                            "location_id": ghl_location_id,
                            "location_name": location_name,
                            "location_address": location_address,
                            "client_group_id": group_id,
                            "client_group_name": client_group_name,
                            "contact_id": contact_id,
                            "contact_data": contact,
                            # Re-classified on every upsert so a GHL workflow that
                            # later stamps `attributionSource` correctly updates an
                            # existing contact from "contact" to "lead".
                            "lead_type": classify_contact_type(contact),
                            "match_keys": compute_match_keys(contact.get("email"), contact.get("phone")),
                            "updated_at": datetime.now()
                        }
                        if is_initial_load:
                            # Marks which contacts this FULL LOAD actually touched,
                            # so STEP 4b can tell "still in GHL" apart from "GHL
                            # stopped returning this one" once the fetch completes.
                            contact_doc["sync_batch_id"] = sync_batch_id

                        bulk_operations.append(
                            UpdateOne(
                                {
                                    "user_id": user_id,
                                    "location_id": ghl_location_id,
                                    "contact_id": contact_id
                                },
                                {
                                    "$set": contact_doc,
                                    "$setOnInsert": {"created_at": datetime.now()}
                                },
                                upsert=True
                            )
                        )

                    if bulk_operations:
                        result = await contacts_collection.bulk_write(
                            bulk_operations, ordered=False
                        )
                        total_new_contacts += result.upserted_count
                        total_updated_contacts += result.modified_count

                        logger.info(
                            f"Page {page}/{total_pages}: {result.upserted_count} new, "
                            f"{result.modified_count} updated "
                            f"(total: {total_new_contacts + total_updated_contacts}/{total_contacts_api})"
                        )

                total_contacts_processed += len(contacts_batch)

                # Check if we should stop
                if not should_continue:
                    logger.info(f"Stopping early - found last known contact")
                    break

                # Check if there's a next page
                has_next = meta.get("hasNext", False)
                if not has_next or len(contacts_batch) == 0:
                    logger.info(f"Reached last page (page {page}/{total_pages})")
                    break

                # Move to next page
                page += 1

                # Small delay to avoid rate limiting
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"Error on page {page}: {str(e)}", exc_info=True)
                pagination_failed = True
                break

        # ============================================
        # STEP 4b: Prune stale contacts, or bail out without deleting anything
        # ============================================
        # Every contact this run actually saw was just upserted in place
        # (STEP 4), so a failed/partial fetch never loses data on its own —
        # whatever pages did complete are simply fresher than before, and
        # pages that never ran leave their contacts untouched. The only
        # destructive step is pruning contacts GHL no longer returns, and that
        # can only be trusted once every page has been seen: only then does a
        # missing sync_batch_id genuinely mean "gone from GHL" rather than
        # "we didn't get that far".
        if is_initial_load:
            if pagination_failed:
                logger.error(
                    f"FULL LOAD for {location_name} did not complete "
                    f"(stopped at page {page}/{total_pages or '?'}, "
                    f"{total_new_contacts + total_updated_contacts}/{total_contacts_api or '?'} "
                    f"contacts synced this run) — leaving existing contacts as-is, no pruning"
                )
                raise RuntimeError(
                    f"GHL FULL LOAD incomplete for {location_name}: "
                    f"stopped at page {page}/{total_pages or '?'}"
                )

            stale = await contacts_collection.delete_many({
                "user_id": user_id,
                "location_id": ghl_location_id,
                "sync_batch_id": {"$ne": sync_batch_id},
            })
            logger.info(
                f"FULL LOAD for {location_name} complete: "
                f"{total_new_contacts} new, {total_updated_contacts} updated, "
                f"pruned {stale.deleted_count} no-longer-in-GHL contacts"
            )

        # ============================================
        # STEP 5: Update cached tag metrics
        # ============================================
        existing_tag_metrics = {}
        if not is_initial_load:
            existing_group = await client_groups_collection.find_one(
                {"id": group_id},
                {"gohighlevel_cache.metrics.tag_breakdown": 1}
            )

            if existing_group:
                existing_tag_metrics = (existing_group.get("gohighlevel_cache", {})
                                        .get("metrics", {})
                                        .get("tag_breakdown", {}))

        # Merge existing tag counts
        if existing_tag_metrics:
            for tag, count in existing_tag_metrics.items():
                tag_counter[tag] += count

        # Get final contact count
        total_contacts_in_db = await contacts_collection.count_documents({
            "user_id": user_id,
            "location_id": ghl_location_id
        })

        # Convert tag counter to sorted dict
        tag_metrics = dict(tag_counter.most_common())

        # Fetch & cache opp stats for ALL date presets via GHL Opportunities API
        try:
            await cache_ghl_opp_stats_all_presets(
                group_id, ghl_location_id, access_token, mongo_client
            )
        except Exception as e:
            logger.warning(f"Failed to cache opp stats presets for {ghl_location_id}: {e}")

        # The dashboard funnel is derived from contacts rather than the
        # opportunity feed, so it gets its own pass over the same refresh.
        try:
            await cache_ghl_cohort_funnel_all_presets(
                group_id, user_id, ghl_location_id, mongo_client
            )
        except Exception as e:
            logger.warning(f"Failed to cache cohort funnel for {ghl_location_id}: {e}")

        # Daily lead counts for the Leads page chart — see ghl_daily_leads.py.
        # A local count over the contacts just synced above, not a fetch, so
        # it's cheap enough to run on every refresh alongside the others.
        try:
            from services.ghl_daily_leads import cache_ghl_daily_leads
            await cache_ghl_daily_leads(
                group_id, user_id, ghl_location_id, mongo_client
            )
        except Exception as e:
            logger.warning(f"Failed to cache daily leads for {ghl_location_id}: {e}")

        # Windowed tag counts for the Clients-page tag columns. Same
        # fire-and-forget shape as the daily-leads cache above: a failure
        # leaves the previous rows in place and costs a stale tag column,
        # never a failed refresh.
        try:
            from services.ghl_tag_cache import cache_group_tag_breakdown
            await cache_group_tag_breakdown(user_id, group_id, mongo_client)
        except Exception as e:
            logger.warning(f"Failed to cache tag breakdown for {group_id}: {e}")

        # Read back opp stats — prefer fresh "maximum" from ghl_opp_cache, fall back to existing
        opp_stats = {"won": 0, "lost": 0, "open": 0, "abandoned": 0,
                     "total_opportunities": 0, "won_revenue": 0.0, "total_revenue": 0.0}
        try:
            group_doc = await client_groups_collection.find_one(
                {"id": group_id},
                {"ghl_opp_cache.maximum": 1, "gohighlevel_cache.metrics.opportunity_stats": 1}
            )
            if group_doc:
                fresh = group_doc.get("ghl_opp_cache", {}).get("maximum", {})
                if fresh and fresh.get("total_opportunities", 0) > 0:
                    opp_stats = fresh
                else:
                    # Keep whatever was there before — don't overwrite with zeros
                    existing = group_doc.get("gohighlevel_cache", {}).get("metrics", {}).get("opportunity_stats", {})
                    if existing and existing.get("total_opportunities", 0) > 0:
                        opp_stats = existing
        except Exception:
            pass

        # Update cache
        cache_data = {
            "location_id": ghl_location_id,
            "name": location_name,
            "address": location_address,
            "metrics": {
                "total_contacts": total_contacts_in_db,
                "tag_breakdown": tag_metrics,
                "opportunity_stats": opp_stats,
            }
        }

        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "gohighlevel_cache": cache_data,
                    "last_ghl_refresh": datetime.utcnow()
                }
            }
        )

        mode = "FULL LOAD" if is_initial_load else "INCREMENTAL"
        logger.info(
            f"{mode} complete for {location_name}: "
            f"{total_contacts_in_db} total contacts in DB "
            f"({total_new_contacts} new, {total_updated_contacts} updated), "
            f"{len(tag_metrics)} unique tags tracked"
        )

        # Log top 5 tags
        if tag_metrics:
            top_tags = list(tag_metrics.items())[:5]
            logger.info(f"Top tags: {top_tags}")

    except Exception as e:
        logger.error(f"Error in fetch_and_cache_ghl_data_optimized: {e}", exc_info=True)
        raise


# ------------------------------------------------------------------
# get_ghl_contacts_sorted  (line ~6308)
# ------------------------------------------------------------------
async def get_ghl_contacts_sorted(
        user_id: str,
        client_group_id: str,
        mongo_client,
        skip: int = 0,
        limit: int = 100
) -> Tuple[List[Dict], int]:
    """
    Fetch GHL contacts in DESCENDING chronological order (newest first).
    """
    db = mongo_client[DB_NAME]
    contacts_collection = db["ghl_contacts"]

    query = {
        "user_id": user_id,
        "client_group_id": client_group_id
    }

    # Get total count
    total_count = await contacts_collection.count_documents(query)

    # Sort by dateAdded DESC (newest first)
    cursor = contacts_collection.find(query).sort(
        "contact_data.dateAdded", -1  # -1 = DESCENDING (newest first)
    ).skip(skip).limit(limit)

    contacts = []
    async for doc in cursor:
        contact_data = doc.get("contact_data", {})
        contacts.append({
            "contact_id": doc.get("contact_id"),
            "contact_data": contact_data,
            "client_group_name": doc.get("client_group_name"),
            "location_name": doc.get("location_name"),
            "date_added": contact_data.get("dateAdded"),
            "tags": contact_data.get("tags", [])
        })

    return contacts, total_count


# ------------------------------------------------------------------
# get_tag_metrics_from_cache  (line ~6348)
# ------------------------------------------------------------------
async def get_tag_metrics_from_cache(
        mongo_client,
        group_id: str,
        user_id: str
) -> Dict[str, int]:
    """
    Get tag metrics from client_groups cache.

    Returns: {"tag_name": count, ...}
    """
    db = mongo_client[DB_NAME]
    client_groups_collection = db["client_groups"]

    group = await client_groups_collection.find_one(
        {"id": group_id, "user_id": user_id},
        {"gohighlevel_cache.metrics.tag_breakdown": 1}
    )

    if not group:
        return {}

    return (group.get("gohighlevel_cache", {})
            .get("metrics", {})
            .get("tag_breakdown", {}))
