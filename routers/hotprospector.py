"""
routers/hotprospector.py
------------------------
HotProspector integration endpoints extracted from main.py.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Request, Query

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user
from integrations.gohighlevel import get_subaccount_tokens
from integrations.hotprospector import (
    HotProspectorIntegration,
    save_hotprospector_credentials,
    get_hotprospector_credentials,
    save_hotprospector_leads_to_collection,
    get_hotprospector_leads_from_collection,
    get_client_group_mapping,
)
from services.hp_service import fetch_and_cache_hp_call_center, _in_window
from services.hp_members import get_member_dashboard_range

logger = logging.getLogger(__name__)

router = APIRouter()

# On-demand freshness: when a single client is opened and its cache is older than
# this, refetch inline. The hp-tick cron refreshes everything daily in the background.
HP_CALL_CENTER_TTL = 6 * 3600  # 6 hours


# ------------------------------------------------------------------
# POST /api/hotprospector/connect
# ------------------------------------------------------------------
@router.post("/api/hotprospector/connect")
async def connect_hotprospector(request: Request, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            api_uid = body.get("api_uid")
            api_key = body.get("api_key")
            if not api_uid or not api_key:
                raise HTTPException(status_code=400, detail="api_uid and api_key are required")
            test_integration = HotProspectorIntegration(api_uid, api_key)
            success, result = await test_integration.get_member_users()
            if not success:
                raise HTTPException(status_code=400, detail=f"Invalid credentials: {result.get('error', 'Unknown error')}")
            await save_hotprospector_credentials(current_user, api_uid, api_key, mongo_client)
            return {
                "success": True,
                "message": "Hot Prospector connected successfully"
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error connecting Hot Prospector for user {current_user}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to connect Hot Prospector: {str(e)}")


# ------------------------------------------------------------------
# GET /api/hotprospector/leads
# ------------------------------------------------------------------
@router.get("/api/hotprospector/leads")
async def get_all_hotprospector_leads(
        skip: int = 0,
        limit: int = 100,
        include_call_logs: bool = True,
        current_user: str = Depends(get_current_user)
):
    """
    CACHE-FIRST WITH CALL LOGS: Always check MongoDB cache first (1-hour freshness).
    Call logs are now cached with leads to avoid repeated API calls.

    Cache Strategy:
    - MongoDB cache: 1 hour (includes call logs)
    - Client cache: 5 minutes (in frontend localStorage)
    - API fetch: Only if MongoDB cache is stale
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # ============================================
            # STEP 1: Get credentials and locations
            # ============================================
            base_tasks = [
                get_hotprospector_credentials(current_user, mongo_client),
                get_subaccount_tokens(current_user, mongo_client),
                get_client_group_mapping(current_user, mongo_client)
            ]

            base_results = await asyncio.gather(*base_tasks, return_exceptions=True)

            credentials = base_results[0] if not isinstance(base_results[0], Exception) else None
            subaccount_tokens = base_results[1] if not isinstance(base_results[1], Exception) else {}
            client_group_mapping = base_results[2] if not isinstance(base_results[2], Exception) else {}

            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected. Please connect via /api/hotprospector/connect"
                )

            if not subaccount_tokens:
                return {
                    "data": [],
                    "meta": {
                        "total": 0,
                        "skip": skip,
                        "limit": limit,
                        "returned": 0,
                        "locations_processed": 0,
                        "cache_status": "no_locations"
                    },
                    "message": "No GHL locations found."
                }

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            # ============================================
            # STEP 2: CHECK MONGODB CACHE FIRST (1 hour freshness)
            # Now includes call logs in cache check
            # ============================================
            all_cached_leads = []
            stale_locations = []
            fresh_locations = []

            CACHE_TTL = 3600  # 1 hour in seconds

            for ghl_location_id, location_data in subaccount_tokens.items():
                # Check client_groups cache timestamp
                client_group = await client_groups_collection.find_one({
                    "user_id": current_user,
                    "ghl_location_id": ghl_location_id
                })

                cache_is_fresh = False

                if client_group:
                    last_refresh = client_group.get("last_hp_refresh")

                    if last_refresh:
                        cache_age = (datetime.utcnow() - last_refresh).total_seconds()
                        cache_is_fresh = cache_age < CACHE_TTL

                        logger.info(
                            f"Cache for {ghl_location_id}: age={cache_age:.0f}s, "
                            f"fresh={cache_is_fresh}"
                        )

                if cache_is_fresh:
                    # Use cached data (now includes call logs!)
                    cached_leads, _ = await get_hotprospector_leads_from_collection(
                        current_user, ghl_location_id, mongo_client, skip=0, limit=None
                    )

                    if cached_leads:
                        # Update client_name and location_name
                        ghl_location_name = location_data.get("name", "Unknown Location")
                        client_group_name = client_group_mapping.get(ghl_location_id)

                        for lead in cached_leads:
                            lead["client_name"] = client_group_name or "No Client Group"
                            lead["ghl_location_name"] = ghl_location_name
                            # Call logs are already included in cached_leads!

                        all_cached_leads.extend(cached_leads)
                        fresh_locations.append(ghl_location_id)

                        total_calls = sum(lead.get("call_logs_count", 0) for lead in cached_leads)
                        logger.info(
                            f"Using {len(cached_leads)} cached leads with {total_calls} calls for "
                            f"{ghl_location_name} (age: {cache_age:.0f}s)"
                        )
                    else:
                        stale_locations.append(ghl_location_id)
                else:
                    stale_locations.append(ghl_location_id)

            # ============================================
            # STEP 3: Fetch fresh data WITH CALL LOGS for stale locations
            # ============================================
            if stale_locations:
                logger.info(
                    f"Fetching fresh data with call logs for {len(stale_locations)} stale locations"
                )

                async def fetch_fresh_location_with_calls(ghl_location_id: str):
                    """Fetch and cache fresh data INCLUDING call logs for one location"""
                    try:
                        location_data = subaccount_tokens.get(ghl_location_id, {})
                        ghl_location_name = location_data.get("name", "Unknown Location")
                        client_group_name = client_group_mapping.get(ghl_location_id)

                        # Fetch fresh leads from API
                        success, hp_leads = await integration.fetch_all_leads_from_ghl_location(
                            ghl_location_id
                        )

                        if not success:
                            logger.warning(f"Failed to fetch leads for {ghl_location_id}")
                            return []

                        # Normalize leads
                        normalized_leads = [
                            integration.normalize_lead(
                                lead,
                                ghl_location_id,
                                ghl_location_name,
                                client_group_name
                            )
                            for lead in hp_leads
                        ]

                        # FETCH CALL LOGS FOR ALL LEADS
                        if include_call_logs and normalized_leads:
                            lead_ids = [
                                str(lead.get("id"))
                                for lead in normalized_leads
                                if lead.get("id")
                            ]

                            logger.info(f"Fetching call logs for {len(lead_ids)} leads")

                            call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                            # Add call logs to normalized leads
                            for lead in normalized_leads:
                                lead_id = str(lead.get("id"))
                                lead["call_logs"] = call_logs_map.get(lead_id, [])
                                lead["call_logs_count"] = len(lead["call_logs"])

                        # Save to MongoDB cache WITH CALL LOGS
                        await save_hotprospector_leads_to_collection(
                            current_user,
                            ghl_location_id,
                            normalized_leads,  # Now includes call_logs!
                            mongo_client
                        )

                        # Update cache timestamp in client_groups
                        total_calls = sum(lead.get("call_logs_count", 0) for lead in normalized_leads)
                        await client_groups_collection.update_one(
                            {
                                "user_id": current_user,
                                "ghl_location_id": ghl_location_id
                            },
                            {
                                "$set": {
                                    "hotprospector_cache.metrics.total_leads": len(normalized_leads),
                                    "hotprospector_cache.metrics.total_calls": total_calls,
                                    "last_hp_refresh": datetime.utcnow()
                                }
                            }
                        )

                        logger.info(
                            f"Fetched and cached {len(normalized_leads)} leads with "
                            f"{total_calls} calls for {ghl_location_name}"
                        )

                        return normalized_leads

                    except Exception as e:
                        logger.error(f"Error fetching {ghl_location_id}: {str(e)}")
                        return []

                # Fetch all stale locations in parallel
                fetch_tasks = [
                    fetch_fresh_location_with_calls(loc_id)
                    for loc_id in stale_locations
                ]

                fresh_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                for result in fresh_results:
                    if not isinstance(result, Exception):
                        all_cached_leads.extend(result)

            # ============================================
            # STEP 4: Deduplicate and process leads
            # Call logs already included, no need to fetch again!
            # ============================================
            unique_leads = {}
            for lead in all_cached_leads:
                email = lead.get("email", "").lower().strip()
                lead_id = lead.get("id")
                key = email if email else f"no_email_{lead_id}"

                if key not in unique_leads:
                    unique_leads[key] = lead
                else:
                    # Merge data
                    existing = unique_leads[key]
                    for field in ["phone", "company"]:
                        if not existing.get(field) and lead.get(field):
                            existing[field] = lead.get(field)

                    # Merge call logs if duplicate has more calls
                    if lead.get("call_logs_count", 0) > existing.get("call_logs_count", 0):
                        existing["call_logs"] = lead.get("call_logs", [])
                        existing["call_logs_count"] = lead.get("call_logs_count", 0)

            deduplicated_leads = list(unique_leads.values())

            # ============================================
            # STEP 5: Calculate total calls from cached data
            # ============================================
            total_calls = sum(lead.get("call_logs_count", 0) for lead in deduplicated_leads)

            # ============================================
            # STEP 6: Sort and paginate
            # ============================================
            deduplicated_leads.sort(
                key=lambda x: (x.get("client_name", ""), x.get("email", ""))
            )

            total_leads = len(deduplicated_leads)
            paginated_leads = deduplicated_leads[skip:skip + limit]

            elapsed = time.time() - start_time

            # Build location stats
            location_stats = {}
            for location_id in subaccount_tokens.keys():
                location_data = subaccount_tokens[location_id]
                location_stats[location_id] = {
                    "name": location_data.get("name", "Unknown"),
                    "cached": location_id in fresh_locations
                }

            cache_status = "full_cache" if not stale_locations else (
                "partial_cache" if fresh_locations else "no_cache"
            )

            logger.info(
                f"COMPLETED in {elapsed:.2f}s: "
                f"{len(paginated_leads)} leads with {total_calls} calls returned, "
                f"cache: {len(fresh_locations)} fresh, {len(stale_locations)} stale"
            )

            return {
                "data": paginated_leads,
                "meta": {
                    "total": total_leads,
                    "skip": skip,
                    "limit": limit,
                    "returned": len(paginated_leads),
                    "locations_processed": len(subaccount_tokens),
                    "location_stats": location_stats,
                    "total_calls": total_calls,
                    "response_time_ms": int(elapsed * 1000),
                    "cache_status": cache_status,
                    "fresh_from_cache": len(fresh_locations),
                    "fetched_fresh": len(stale_locations)
                },
                "message": (
                    f"Retrieved {len(paginated_leads)} leads with {total_calls} calls "
                    f"({len(fresh_locations)} from cache, {len(stale_locations)} fresh)"
                )
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching HotProspector leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


# ------------------------------------------------------------------
# GET /api/hotprospector/leads/refresh
# ------------------------------------------------------------------
@router.get("/api/hotprospector/leads/refresh")
async def refresh_hotprospector_leads(current_user: str = Depends(get_current_user)):
    """
    ULTRA-OPTIMIZED: Force refresh all HotProspector leads WITH CALL LOGS.
    Single API call per location + batch call log fetching.

    Expected: 10-20x faster than old version
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            # Get credentials and locations
            base_tasks = [
                get_hotprospector_credentials(current_user, mongo_client),
                get_subaccount_tokens(current_user, mongo_client),
                get_client_group_mapping(current_user, mongo_client)
            ]

            base_results = await asyncio.gather(*base_tasks, return_exceptions=True)

            credentials = base_results[0] if not isinstance(base_results[0], Exception) else None
            subaccount_tokens = base_results[1] if not isinstance(base_results[1], Exception) else {}
            client_group_mapping = base_results[2] if not isinstance(base_results[2], Exception) else {}

            if not credentials:
                raise HTTPException(status_code=400, detail="Hot Prospector not connected")

            if not subaccount_tokens:
                raise HTTPException(status_code=400, detail="No GHL locations found")

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            # Clear cache
            db = mongo_client[DB_NAME]
            leads_collection = db["hotprospector_leads"]
            await leads_collection.delete_many({"user_id": current_user})
            logger.info(f"Cleared all cached leads for user {current_user}")

            # Refresh function with call logs
            async def refresh_location_with_calls(ghl_location_id: str, location_data: dict):
                """Refresh leads for a single location - INCLUDES CALL LOGS"""
                try:
                    ghl_location_name = location_data.get("name", "Unknown Location")
                    client_group_name = client_group_mapping.get(ghl_location_id)

                    # SINGLE API CALL for leads
                    success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)

                    if not success:
                        return {
                            "ghl_location_id": ghl_location_id,
                            "ghl_location_name": ghl_location_name,
                            "client_group_name": client_group_name or "No Client Group",
                            "leads_count": 0,
                            "calls_count": 0,
                            "success": False,
                            "error": hp_leads.get('error')
                        }

                    # Normalize leads
                    normalized_leads = [
                        integration.normalize_lead(
                            lead,
                            ghl_location_id,
                            ghl_location_name,
                            client_group_name
                        )
                        for lead in hp_leads
                    ]

                    # FETCH CALL LOGS
                    total_calls = 0
                    if normalized_leads:
                        lead_ids = [str(lead.get("id")) for lead in normalized_leads if lead.get("id")]

                        logger.info(f"Fetching call logs for {len(lead_ids)} leads in {ghl_location_name}")
                        call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                        for lead in normalized_leads:
                            lead_id = str(lead.get("id"))
                            lead["call_logs"] = call_logs_map.get(lead_id, [])
                            lead["call_logs_count"] = len(lead["call_logs"])
                            total_calls += lead["call_logs_count"]

                    # Save to cache WITH CALL LOGS
                    await save_hotprospector_leads_to_collection(
                        current_user,
                        ghl_location_id,
                        normalized_leads,
                        mongo_client
                    )

                    return {
                        "ghl_location_id": ghl_location_id,
                        "ghl_location_name": ghl_location_name,
                        "client_group_name": client_group_name or "No Client Group",
                        "leads_count": len(normalized_leads),
                        "calls_count": total_calls,
                        "success": True
                    }

                except Exception as e:
                    logger.error(f"Error refreshing {ghl_location_id}: {str(e)}")
                    return {
                        "ghl_location_id": ghl_location_id,
                        "ghl_location_name": location_data.get("name", "Unknown"),
                        "client_group_name": client_group_mapping.get(ghl_location_id, "No Client Group"),
                        "leads_count": 0,
                        "calls_count": 0,
                        "success": False,
                        "error": str(e)
                    }

            # Launch all refreshes in parallel
            refresh_tasks = [
                refresh_location_with_calls(location_id, location_data)
                for location_id, location_data in subaccount_tokens.items()
            ]

            logger.info(f"Refreshing {len(refresh_tasks)} locations with call logs in parallel")

            refresh_results = await asyncio.gather(*refresh_tasks, return_exceptions=True)

            # Process results
            results = []
            for result in refresh_results:
                if not isinstance(result, Exception):
                    results.append(result)

            total_leads = sum(r["leads_count"] for r in results if r["success"])
            total_calls = sum(r["calls_count"] for r in results if r["success"])
            successful = sum(1 for r in results if r["success"])
            failed = sum(1 for r in results if not r["success"])

            elapsed = time.time() - start_time

            logger.info(
                f"REFRESH COMPLETED in {elapsed:.2f}s: "
                f"{total_leads} leads with {total_calls} calls, "
                f"{successful} locations succeeded, {failed} failed"
            )

            return {
                "success": True,
                "message": f"Refreshed {total_leads} leads with {total_calls} calls from {len(results)} locations in {elapsed:.2f}s",
                "results": results,
                "meta": {
                    "total_locations": len(results),
                    "successful": successful,
                    "failed": failed,
                    "total_leads": total_leads,
                    "total_calls": total_calls,
                    "response_time_ms": int(elapsed * 1000)
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error refreshing leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to refresh leads: {str(e)}")


# ------------------------------------------------------------------
# GET /api/hotprospector/status  (runtime version — the second definition)
# ------------------------------------------------------------------
@router.get("/api/hotprospector/status")
async def hotprospector_status(current_user: str = Depends(get_current_user)):
    """Check HotProspector connection status and show linked GHL locations"""
    async with get_mongo_client() as mongo_client:
        try:
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)

            ghl_locations = []
            for location_id, location_data in subaccount_tokens.items():
                ghl_locations.append({
                    "location_id": location_id,
                    "name": location_data.get("name", "Unknown"),
                    "address": location_data.get("address")
                })

            return {
                "connected": bool(credentials and credentials.get("connected")),
                "api_uid": credentials.get("api_uid") if credentials else None,
                "ghl_locations": ghl_locations,
                "total_ghl_locations": len(ghl_locations)
            }
        except Exception as e:
            logger.error(f"Error checking Hot Prospector status: {str(e)}")
            return {
                "connected": False,
                "error": str(e),
                "ghl_locations": [],
                "total_ghl_locations": 0
            }


# ------------------------------------------------------------------
# GET /api/hotprospector/members
# ------------------------------------------------------------------
@router.get("/api/hotprospector/members")
async def get_hotprospector_members(current_user: str = Depends(get_current_user)):
    """Get all HotProspector team members"""
    async with get_mongo_client() as mongo_client:
        try:
            # Check cache first
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            user_doc = await users_collection.find_one(
                {"user_id": current_user},
                projection={"integrations.hotprospector.members": 1}
            )

            # Check if cached data exists and is fresh (1 hour)
            if user_doc:
                members_data = (user_doc.get("integrations", {})
                                .get("hotprospector", {})
                                .get("members"))

                if members_data:
                    updated_at = members_data.get("updated_at")
                    if updated_at:
                        cache_age = (datetime.now() - updated_at).total_seconds()
                        if cache_age < 3600:  # 1 hour cache
                            members = members_data.get("data", [])
                            return {
                                "data": members,
                                "meta": {"total": len(members)},
                                "message": f"Retrieved {len(members)} members from cache"
                            }

            # Fetch fresh data
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected"
                )

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            success, members = await integration.get_member_users()
            if not success:
                raise HTTPException(
                    status_code=members.get("status_code", 500),
                    detail=members.get("error", "Failed to fetch members")
                )

            # Save to cache
            await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$set": {
                        "integrations.hotprospector.members": {
                            "data": members,
                            "updated_at": datetime.now()
                        },
                        "updated_at": datetime.now()
                    }
                },
                upsert=True
            )

            return {
                "data": members,
                "meta": {"total": len(members)},
                "message": f"Successfully fetched {len(members)} members"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching Hot Prospector members: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch members: {str(e)}")


async def _get_windowed_leads_with_calls(leads_collection, query, start_date, end_date, skip, limit):
    """
    Aggregation counterpart to the find()-based path above: recompute each
    lead's windowed call_logs (same date-window semantics as _in_window —
    lexicographic yyyy-mm-dd comparison on the call_time_iso date portion),
    drop leads with zero windowed calls, then paginate + count the *filtered*
    set via $facet so meta.total matches what has_calls actually returns.
    """
    conds = [{"$ne": ["$$log.call_time_iso", None]}]
    if start_date:
        conds.append({"$gte": [{"$substrCP": ["$$log.call_time_iso", 0, 10]}, start_date]})
    if end_date:
        conds.append({"$lte": [{"$substrCP": ["$$log.call_time_iso", 0, 10]}, end_date]})

    pipeline = [
        {"$match": query},
        {
            "$addFields": {
                "_windowed_logs": {
                    "$filter": {
                        "input": {"$ifNull": ["$lead_data.call_logs", []]},
                        "as": "log",
                        "cond": {"$and": conds},
                    }
                }
            }
        },
        {"$addFields": {"_call_logs_count": {"$size": "$_windowed_logs"}}},
        {"$match": {"_call_logs_count": {"$gt": 0}}},
        {"$sort": {"created_at": -1}},
        {
            "$facet": {
                "data": [
                    {"$skip": skip},
                    *([{"$limit": limit}] if limit and limit > 0 else []),
                    {"$project": {"_id": 0, "lead_data": 1, "_windowed_logs": 1, "_call_logs_count": 1}},
                ],
                "total": [{"$count": "count"}],
            }
        },
    ]

    result = await leads_collection.aggregate(pipeline).to_list(1)
    facet = result[0] if result else {"data": [], "total": []}
    total = facet["total"][0]["count"] if facet["total"] else 0

    leads = [
        {
            **(doc.get("lead_data") or {}),
            "call_logs": doc["_windowed_logs"],
            "call_logs_count": doc["_call_logs_count"],
        }
        for doc in facet["data"]
    ]
    return total, leads


# ------------------------------------------------------------------
# GET /api/hotprospector/call-center
# ------------------------------------------------------------------
@router.get("/api/hotprospector/call-center")
async def get_hp_call_center(
        location_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        skip: int = 0,
        limit: int = 100,
        has_calls: bool = False,
        refresh: bool = False,
        current_user: str = Depends(get_current_user),
):
    """
    Leads-with-call-logs for the Sales-Hub "Leads" tab, read from the
    hotprospector_leads cache.

    - location_id given   → one client (GHL location). Cache-first: if its
      last_hp_refresh is within HP_CALL_CENTER_TTL we serve from cache, otherwise
      we fetch fresh (SearchByUserInput + bulk FetchUserCallLog) first.
      ?refresh=true forces it.
    - location_id omitted → all of the user's locations (no on-demand fetch; the
      hp-tick cron keeps them fresh daily).

    start_date/end_date (yyyy-mm-dd, optional) window each lead's call_logs by
    call_time so counts match the selected date preset. Leads with no calls in the
    window are still returned (count 0) — unless has_calls=true, which drops them
    (i.e. "hide no-dialer-activity leads"), filtered at the query level against the
    *entire* matching set, not just the returned page.

    Response: { data: [<lead w/ windowed call_logs>],
                meta: { total, returned, skip, limit, location_id, start_date, end_date } }
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]
            leads_collection = db["hotprospector_leads"]

            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected. Add credentials in Settings → Integrations.",
                )

            # Single-location view: cache-first with on-demand refresh.
            if location_id:
                group = await client_groups_collection.find_one(
                    {"user_id": current_user, "ghl_location_id": location_id},
                    projection={"last_hp_refresh": 1, "hp_backfill_status": 1},
                )
                last_refresh = group.get("last_hp_refresh") if group else None
                backfilled = (group or {}).get("hp_backfill_status") == "complete"
                # Windows that include today/yesterday get a short TTL (near real-time);
                # purely historical windows use the long TTL.
                window_has_recent = (not end_date) or end_date >= date.today().isoformat()
                ttl = 900 if window_has_recent else HP_CALL_CENTER_TTL  # 15 min vs 6h
                is_fresh = bool(
                    last_refresh
                    and not refresh
                    and (datetime.utcnow() - last_refresh).total_seconds() < ttl
                )
                if not is_fresh:
                    await fetch_and_cache_hp_call_center(
                        current_user, location_id, mongo_client,
                        mode="incremental" if backfilled else "backfill",
                    )

            # Read (optionally location-scoped) leads from the cache collection.
            query = {"user_id": current_user}
            if location_id:
                query["ghl_location_id"] = location_id

            windowed = bool(start_date or end_date)

            if has_calls and windowed:
                # Windowed counts only exist after per-request filtering of each
                # lead's call_logs, so "has calls in this window" can't be a plain
                # field filter — it needs an aggregation that recomputes the
                # windowed count and drops zero-count leads before paginating,
                # otherwise a page could come back empty while later pages still
                # have matches.
                total, leads = await _get_windowed_leads_with_calls(
                    leads_collection, query, start_date, end_date, skip, limit,
                )
            else:
                if has_calls:
                    # No window: the stored count already reflects full call
                    # history, so a plain field filter is enough.
                    query["lead_data.call_logs_count"] = {"$gt": 0}

                total = await leads_collection.count_documents(query)
                cursor = (
                    leads_collection.find(query, projection={"lead_data": 1, "_id": 0})
                    .sort("created_at", -1)
                    .skip(skip)
                )
                if limit and limit > 0:
                    cursor = cursor.limit(limit)

                leads = []
                async for doc in cursor:
                    lead = doc.get("lead_data") or {}
                    if windowed:
                        logs = [
                            l for l in (lead.get("call_logs") or [])
                            if _in_window(l.get("call_time_iso"), start_date, end_date)
                        ]
                        lead = {**lead, "call_logs": logs, "call_logs_count": len(logs)}
                    leads.append(lead)

            return {
                "data": leads,
                "meta": {
                    "total": total,
                    "returned": len(leads),
                    "skip": skip,
                    "limit": limit,
                    "location_id": location_id,
                    "start_date": start_date,
                    "end_date": end_date,
                    "has_calls": has_calls,
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in HP call-center (loc={location_id}): {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Failed to load call center data: {str(e)}"
            )


# ------------------------------------------------------------------
# GET /api/hotprospector/members/dashboard
# ------------------------------------------------------------------
@router.get("/api/hotprospector/members/dashboard")
async def get_hp_member_dashboard(
        date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        current_user: str = Depends(get_current_user),
):
    """
    Team list merged with per-agent dashboard metrics (getMemberDashboardData),
    aggregated over a date window.

    Pass `start_date`+`end_date` (Y-m-d) for a range, or `date` for a single day
    (omit all for today). Historical days are summed from the hotprospector_member_daily
    store; TODAY and YESTERDAY in the window are fetched live. Each row carries the
    member's identity plus an aggregated `dashboard` object (empty when no activity).
    """
    async with get_mongo_client() as mongo_client:
        try:
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected. Add credentials in Settings → Integrations.",
                )
            integration = HotProspectorIntegration(
                credentials.get("api_uid"), credentials.get("api_key")
            )

            # Resolve the window: explicit range, else single date, else today.
            if not (start_date or end_date) and date:
                start_date = end_date = date

            rows = await get_member_dashboard_range(
                current_user, start_date, end_date, integration, mongo_client
            )

            # Merge with the team list so inactive agents still appear.
            ok_m, members = await integration.get_member_users()
            members = members if (ok_m and isinstance(members, list)) else []
            by_email = {str(r.get("agentEmail") or "").lower(): r for r in rows if r.get("agentEmail")}
            by_id = {str(r.get("agentId")): r for r in rows if r.get("agentId") is not None}

            out, matched = [], set()
            for m in members:
                r = by_email.get(str(m.get("email") or "").lower()) or by_id.get(str(m.get("memberId") or ""))
                if r is not None:
                    matched.add(id(r))
                out.append({**m, "dashboard": (r or {}).get("dashboard", {})})

            for r in rows:
                if id(r) in matched:
                    continue
                name = (r.get("agentName") or "").strip()
                out.append({
                    "memberId": r.get("agentId"),
                    "first_name": name.split(" ")[0] if name else "",
                    "last_name": " ".join(name.split(" ")[1:]) if " " in name else "",
                    "email": r.get("agentEmail"),
                    "member_status": "Active",
                    "dashboard": r.get("dashboard", {}),
                })

            return {
                "data": out,
                "meta": {
                    "start_date": start_date,
                    "end_date": end_date,
                    "total": len(out),
                    "team": len(members),
                    "with_metrics": len(rows),
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching HP member dashboard: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to load member dashboard: {str(e)}")
