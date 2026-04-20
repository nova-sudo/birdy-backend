"""
routers/client_groups.py
------------------------
Client group CRUD and related endpoints extracted from main.py.
"""

import asyncio
import logging
import os
import time
from collections import Counter
from datetime import date as _date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response

from core.database import DB_NAME
from core.constants import META_CACHE_PRESETS, PRESET_ALIAS, GHL_PRESET_DATE_RANGE, ghl_date_bounds
from core.models import ClientGroupRequest
from core.utils import mongo_to_dict, get_result_value
from dependencies import get_current_user, get_mongo_client
from billing_middleware import check_client_limit

# Integration imports
from integrations.gohighlevel import (
    ghl_integration,
    get_agency_token,
    get_subaccount_tokens,
    save_subaccount_token,
    fetch_location_details,
    get_contact_count_from_ghl,
)
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.facebook_leads import (
    fetch_and_cache_facebook_leads_FIXED,
)
from integrations.facebook_utils.facebook_campaigns import (
    fetch_and_cache_campaign_insights,
)
from integrations.facebook_utils.facebook_adsets import (
    fetch_and_cache_adset_insights,
)
from integrations.facebook_utils.facebook_ads import (
    fetch_and_cache_ad_insights,
)
from integrations.facebook_utils.facebook_optimized import (
    fetch_all_accounts_parallel,
)
from integrations.hotprospector import (
    get_hotprospector_credentials,
    get_hotprospector_leads_from_collection,
    save_hotprospector_leads_to_collection,
    HotProspectorIntegration,
)

# Service imports
from services.meta_service import (
    get_facebook_data,
    save_facebook_data,
    fetch_meta_data_for_group,
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)
from services.ghl_service import (
    fetch_and_cache_ghl_data_optimized,
    get_ghl_contacts_sorted,
    get_tag_metrics_from_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/client-groups
# ---------------------------------------------------------------------------

@router.get("/api/client-groups")
async def get_client_groups(
    date_preset: Optional[str] = "maximum",
    current_user: str = Depends(get_current_user),
):
    """
    Return all client groups for the current user.

    Meta metrics  -> served from facebook_cache.<preset_key> sub-document.
    GHL contacts  -> total_contacts always lifetime (from gohighlevel_cache);
                     new_contacts aggregated live from ghl_contacts filtered
                     by contact_data.dateAdded for the requested window.

    Accepted date_preset values:
        maximum (default) | today | yesterday | this_week | this_week_mon_today |
        this_week_sun_today | last_3d | last_7d | last_14d | last_28d | last_30d |
        last_90d | this_month | last_month | this_quarter | last_quarter |
        this_year | last_year
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Resolve alias -> canonical cache key
            resolved_preset = PRESET_ALIAS.get(date_preset or "maximum", "maximum")

            # -- GHL: compute ISO date bounds for the window --
            ghl_start, ghl_end = ghl_date_bounds(resolved_preset)
            is_all_time = ghl_start is None

            # -- GHL: aggregate new_contacts + windowed tag_breakdown --
            ghl_windowed: dict = {}
            if not is_all_time:
                contacts_col = db["ghl_contacts"]
                # Two-stage aggregation: count contacts, then unwind tags in Mongo
                ghl_date_match = {
                    "user_id": current_user,
                    "contact_data.dateAdded": {
                        "$gte": f"{ghl_start}T00:00:00.000Z",
                        "$lte": f"{ghl_end}T23:59:59.999Z",
                    },
                }
                pipeline = [
                    {"$match": ghl_date_match},
                    {"$facet": {
                        "counts": [
                            {"$group": {
                                "_id": "$client_group_id",
                                "new_contacts": {"$sum": 1},
                            }},
                        ],
                        "tags": [
                            {"$unwind": {"path": "$contact_data.tags", "preserveNullAndEmptyArrays": False}},
                            {"$group": {
                                "_id": {"group": "$client_group_id", "tag": "$contact_data.tags"},
                                "count": {"$sum": 1},
                            }},
                            {"$sort": {"count": -1}},
                            {"$group": {
                                "_id": "$_id.group",
                                "tags": {"$push": {"tag": "$_id.tag", "count": "$count"}},
                            }},
                        ],
                    }},
                ]
                facet_result = await contacts_col.aggregate(pipeline).to_list(1)
                if facet_result:
                    facet = facet_result[0]
                    # Build counts map
                    for row in facet.get("counts", []):
                        ghl_windowed[row["_id"]] = {
                            "new_contacts": row["new_contacts"],
                            "tag_breakdown": {},
                        }
                    # Merge tags
                    for row in facet.get("tags", []):
                        group_id = row["_id"]
                        if group_id not in ghl_windowed:
                            ghl_windowed[group_id] = {"new_contacts": 0, "tag_breakdown": {}}
                        ghl_windowed[group_id]["tag_breakdown"] = {
                            t["tag"]: t["count"] for t in row.get("tags", [])
                        }
                    # NOTE: Opportunity stats are now read from ghl_opp_cache (per-preset),
                    # no longer aggregated live from contact_data.opportunities

            # -- Fetch groups from DB --
            # Only project the specific preset bucket requested, not all 13
            fb_projection = {
                f"facebook_cache.{resolved_preset}": 1,
                "facebook_cache.ad_account_id": 1,
                "facebook_cache.name": 1,
                "facebook_cache.currency": 1,
                "facebook_cache.original_currency": 1,
                "facebook_cache.total_leads": 1,
                "facebook_cache.metrics": 1,
                "facebook_cache.campaigns": 1,
                "facebook_cache.adsets": 1,
                "facebook_cache.ads": 1,
            }
            client_groups = await client_groups_collection.find(
                {"user_id": current_user},
                {
                    "_id": 1, "id": 1, "name": 1,
                    "ghl_location_id": 1, "meta_ad_account_id": 1,
                    "ad_account_currency": 1, "notes": 1,
                    "created_at": 1, "updated_at": 1,
                    "gohighlevel_cache": 1,
                    f"ghl_opp_cache.{resolved_preset}": 1,
                    "ghl_opp_cache.maximum": 1,
                    **fb_projection,
                    "hotprospector_cache": 1,
                    "last_ghl_refresh": 1, "last_meta_refresh": 1, "last_hp_refresh": 1,
                    "status": 1,
                    "meta_token_error": 1,
                    "client_status": 1,
                },
            ).to_list(None)

            if not client_groups:
                return {
                    "client_groups": [],
                    "meta": {
                        "total_groups": 0,
                        "total_ghl_locations": 0,
                        "total_meta_ad_accounts": 0,
                        "total_hotprospector_enabled": 0,
                    },
                    "message": "No client groups found",
                }

            result = []
            for group in client_groups:
                group_data = mongo_to_dict(group)
                gid = group_data.get("id") or ""

                # -- GHL: merge lifetime cache + windowed counts + preset opp stats --
                ghl_cache = group.get("gohighlevel_cache") or {}
                ghl_opp_cache = group.get("ghl_opp_cache") or {}
                legacy_stats = (ghl_cache.get("metrics") or {}).get("opportunity_stats") or {}

                # Pick the per-preset stats. A zero-activity preset is a legitimate
                # result (means "no won/lost/open events in this window") and must
                # be used as-is rather than falling back to lifetime. Only fall
                # back if the preset is genuinely missing from the cache.
                preset_stats = ghl_opp_cache.get(resolved_preset)
                if isinstance(preset_stats, dict):
                    preset_opp = preset_stats
                elif isinstance(ghl_opp_cache.get("maximum"), dict):
                    # Preset missing — use lifetime snapshot as backup
                    preset_opp = ghl_opp_cache["maximum"]
                else:
                    # No per-preset cache at all — fall back to legacy (pre-migration) data
                    preset_opp = legacy_stats

                if is_all_time:
                    ghl_cache_copy = {**ghl_cache}
                    if preset_opp:
                        metrics = ghl_cache_copy.get("metrics") or {}
                        ghl_cache_copy["metrics"] = {**metrics, "opportunity_stats": preset_opp}
                    group_data["gohighlevel"] = ghl_cache_copy
                else:
                    windowed = ghl_windowed.get(gid, {})
                    cached_metrics = ghl_cache.get("metrics") or {}
                    group_data["gohighlevel"] = {
                        **ghl_cache,
                        "metrics": {
                            **cached_metrics,
                            "total_contacts": windowed.get("new_contacts", 0),
                            "tag_breakdown": windowed.get("tag_breakdown", {}),
                            "opportunity_stats": preset_opp,
                            "lifetime_total_contacts": cached_metrics.get("total_contacts", 0),
                        },
                    }

                # -- HP cache (unchanged) --
                group_data["hotprospector"] = group.get("hotprospector_cache", {})

                # -- Meta: read preset sub-document --
                facebook_cache = group.get("facebook_cache") or {}
                preset_doc = facebook_cache.get(resolved_preset)

                if preset_doc and isinstance(preset_doc, dict):
                    group_data["facebook"] = {
                        "ad_account_id": facebook_cache.get("ad_account_id", group.get("meta_ad_account_id", "")),
                        "name": facebook_cache.get("name", ""),
                        "currency": facebook_cache.get("currency", ""),
                        "total_leads": facebook_cache.get("total_leads", 0),
                        "campaigns": preset_doc.get("campaigns", []),
                        "adsets": preset_doc.get("adsets", []),
                        "ads": preset_doc.get("ads", []),
                        "metrics": preset_doc.get("metrics", {}),
                        "date_preset": resolved_preset,
                    }
                else:
                    group_data["facebook"] = facebook_cache

                group_data.pop("gohighlevel_cache", None)
                group_data.pop("facebook_cache", None)
                group_data.pop("hotprospector_cache", None)
                group_data.pop("ghl_opp_cache", None)

                result.append(group_data)

            elapsed = time.time() - start_time
            logger.info(
                f"Served {len(result)} client groups "
                f"[preset={resolved_preset}] in {elapsed:.3f}s"
            )

            return {
                "client_groups": result,
                "meta": {
                    "total_groups": len(result),
                    "date_preset": resolved_preset,
                    "is_all_time": is_all_time,
                    "total_ghl_locations": sum(1 for g in result if g.get("gohighlevel", {}).get("location_id")),
                    "total_meta_ad_accounts": sum(1 for g in result if g.get("facebook", {}).get("ad_account_id")),
                    "total_hotprospector_enabled": sum(
                        1 for g in result
                        if g.get("hotprospector", {}).get("metrics", {}).get("total_leads", 0) > 0
                    ),
                },
                "message": f"Successfully fetched {len(result)} client groups from cache",
            }

        except Exception as e:
            logger.error(
                f"Error fetching client groups for user {current_user}: {str(e)}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch client groups: {str(e)}",
            )


# ---------------------------------------------------------------------------
# POST /api/client-groups
# ---------------------------------------------------------------------------

@router.post("/api/client-groups")
async def create_client_group_optimized(
    request: ClientGroupRequest,
    current_user: str = Depends(get_current_user),
):
    """
    Create a new client group with OPTIMIZED GHL data fetching.

    Features:
    - Fetches ALL contacts in descending chronological order
    - Calculates tag metrics and stores in cache
    - Smart data organization (user > client_group > contacts)
    """
    async with get_mongo_client() as mongo_client:
        await check_client_limit(current_user, mongo_client)
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            if not request.name:
                raise HTTPException(status_code=400, detail="Client group name is required")

            # DUPLICATE CHECK
            duplicate_filter = {"user_id": current_user}

            if request.ghl_location_id and request.meta_ad_account_id:
                duplicate_filter["$and"] = [
                    {"ghl_location_id": request.ghl_location_id},
                    {"meta_ad_account_id": request.meta_ad_account_id},
                ]
            elif request.ghl_location_id:
                duplicate_filter["ghl_location_id"] = request.ghl_location_id
            elif request.meta_ad_account_id:
                duplicate_filter["meta_ad_account_id"] = request.meta_ad_account_id
            else:
                duplicate_filter = None

            if duplicate_filter:
                existing = await client_groups_collection.find_one(
                    duplicate_filter, {"name": 1, "id": 1}
                )
                if existing:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"A client group named \"{existing.get('name')}\" already uses "
                            f"{'this GHL location' if request.ghl_location_id else ''}"
                            f"{' and ' if request.ghl_location_id and request.meta_ad_account_id else ''}"
                            f"{'this Meta ad account' if request.meta_ad_account_id else ''}. "
                            f"Each integration can only be linked to one client group at a time."
                        ),
                    )

            # STEP 1: Generate GHL location token if provided
            if request.ghl_location_id:
                agency_token = await get_agency_token(current_user, mongo_client)
                if not agency_token:
                    raise HTTPException(
                        status_code=400,
                        detail="No agency token available. Please connect GoHighLevel in Settings.",
                    )

                company_id = agency_token.get("company_id")
                access_token = agency_token.get("access_token")

                if not company_id or not access_token:
                    raise HTTPException(status_code=400, detail="Invalid agency token")

                success, loc_tokens = await ghl_integration.generate_location_token(
                    company_id, request.ghl_location_id, access_token
                )

                if not success:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to generate location token: {loc_tokens.get('error', 'Unknown error')}",
                    )

                location_details = await fetch_location_details(
                    request.ghl_location_id, loc_tokens.get("access_token")
                )

                contact_count = await get_contact_count_from_ghl(
                    request.ghl_location_id, loc_tokens.get("access_token")
                )

                await save_subaccount_token(
                    current_user,
                    request.ghl_location_id,
                    loc_tokens,
                    mongo_client,
                    location_details,
                    contact_count=contact_count,
                )

                logger.info(f"Generated and saved token for GHL location {request.ghl_location_id}")

            # STEP 2: Create client group
            group_id = f"{current_user}_{int(datetime.now().timestamp())}"
            client_group = {
                "id": group_id,
                "user_id": current_user,
                "name": request.name,
                "ad_account_currency": request.ad_account_currency,
                "ghl_location_id": request.ghl_location_id,
                "meta_ad_account_id": request.meta_ad_account_id,
                "notes": request.notes or "",
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "status": "creating",
                "status_message": "Fetching data...",
                "gohighlevel_cache": {},
                "facebook_cache": {},
                "hotprospector_cache": {},
                "last_ghl_refresh": None,
                "last_meta_refresh": None,
                "last_hp_refresh": None,
                "client_status": "Active"
            }

            await client_groups_collection.insert_one(client_group)
            logger.info(f"Created client group {group_id}")

            # STEP 3: Fetch GHL data with OPTIMIZED function
            if request.ghl_location_id:
                await fetch_and_cache_ghl_data_optimized(
                    group_id,
                    request.ghl_location_id,
                    current_user,
                    mongo_client,
                    is_initial_load=True,
                )

            # Fetch Meta data via resilient refresh manager
            if request.meta_ad_account_id:
                if not request.ad_account_currency:
                    raise HTTPException(
                        status_code=400,
                        detail="ad_account_currency is required when meta_ad_account_id is provided",
                    )
                from services.meta_refresh_manager import start_refresh

                group_for_refresh = {
                    "meta_ad_account_id": request.meta_ad_account_id,
                    "ad_account_currency": request.ad_account_currency,
                    "name": request.name,
                }
                await start_refresh(group_id, current_user, group_for_refresh, mongo_client)

            # STEP 4: Mark as complete
            await client_groups_collection.update_one(
                {"id": group_id},
                {
                    "$set": {
                        "status": "complete",
                        "status_message": "Client group created successfully",
                    }
                },
            )

            final_group = await client_groups_collection.find_one({"id": group_id})

            return {
                "client_group": mongo_to_dict(final_group),
                "message": "Client group created successfully",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating client group: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to create client group: {str(e)}")


# ---------------------------------------------------------------------------
# GET /api/client-groups/{group_id}/tag-metrics
# ---------------------------------------------------------------------------

@router.get("/api/client-groups/{group_id}/tag-metrics")
async def get_client_group_tag_metrics(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    Get tag metrics for a client group.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user,
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            tag_metrics = await get_tag_metrics_from_cache(
                mongo_client, group_id, current_user
            )

            total_contacts = (
                group.get("gohighlevel_cache", {})
                .get("metrics", {})
                .get("total_contacts", 0)
            )

            return {
                "tag_metrics": tag_metrics,
                "total_contacts": total_contacts,
                "total_unique_tags": len(tag_metrics),
                "message": f"Retrieved metrics for {len(tag_metrics)} unique tags",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting tag metrics: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/contacts/ghl-sorted
# ---------------------------------------------------------------------------

@router.get("/api/contacts/ghl-sorted")
async def get_ghl_contacts_sorted_endpoint(
    group_id: str = Query(...),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    current_user: str = Depends(get_current_user),
):
    """
    Get GHL contacts in DESCENDING chronological order (newest first).
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user,
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            skip = (page - 1) * limit

            contacts, total_count = await get_ghl_contacts_sorted(
                current_user, group_id, mongo_client, skip=skip, limit=limit
            )

            elapsed = time.time() - start_time
            total_pages = (total_count + limit - 1) // limit

            logger.info(
                f"Fetched page {page} in descending order: {len(contacts)} contacts "
                f"in {elapsed:.3f}s"
            )

            return {
                "contacts": contacts,
                "meta": {
                    "total_contacts": total_count,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(contacts),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "dateAdded_desc",
                },
                "message": f"Retrieved {len(contacts)} contacts in descending chronological order",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching sorted contacts: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/contacts/ghl/refresh-incremental
# ---------------------------------------------------------------------------

@router.post("/api/contacts/ghl/refresh-incremental")
async def refresh_ghl_incremental(
    group_id: str = Query(...),
    current_user: str = Depends(get_current_user),
):
    """
    Smart incremental refresh for GHL contacts.
    Only fetches NEW contacts since last refresh.
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user,
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            if not group.get("ghl_location_id"):
                raise HTTPException(
                    status_code=400,
                    detail="Client group does not have a GHL location",
                )

            logger.info(f"Starting incremental refresh for group {group_id}")

            await fetch_and_cache_ghl_data_optimized(
                group_id,
                group["ghl_location_id"],
                current_user,
                mongo_client,
                is_initial_load=False,
            )

            updated_group = await client_groups_collection.find_one({"id": group_id})

            total_contacts = (
                updated_group.get("gohighlevel_cache", {})
                .get("metrics", {})
                .get("total_contacts", 0)
            )

            tag_metrics = (
                updated_group.get("gohighlevel_cache", {})
                .get("metrics", {})
                .get("tag_breakdown", {})
            )

            elapsed = time.time() - start_time

            logger.info(
                f"Incremental refresh complete for group {group_id}: "
                f"{total_contacts} total contacts, {len(tag_metrics)} unique tags "
                f"in {elapsed:.2f}s"
            )

            return {
                "success": True,
                "message": f"Incremental refresh completed for '{group['name']}'",
                "group_id": group_id,
                "total_contacts": total_contacts,
                "total_unique_tags": len(tag_metrics),
                "refresh_mode": "incremental",
                "timestamp": datetime.utcnow().isoformat(),
                "performance": {
                    "response_time_seconds": round(elapsed, 2),
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in incremental refresh: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to refresh contacts: {str(e)}",
            )


# ---------------------------------------------------------------------------
# GET /api/client-groups/{group_id}/status
# ---------------------------------------------------------------------------

@router.get("/api/client-groups/{group_id}/status")
async def get_client_group_status(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    Get the current status of a client group creation.
    Use this endpoint to poll for completion status.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one(
                {"id": group_id, "user_id": current_user},
                {
                    "status": 1,
                    "status_message": 1,
                    "creation_progress": 1,
                    "updated_at": 1,
                },
            )

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            return {
                "status": group.get("status", "unknown"),
                "status_message": group.get("status_message", ""),
                "progress": group.get("creation_progress", {}),
                "updated_at": group.get("updated_at").isoformat() if group.get("updated_at") else None,
                "is_complete": group.get("status") == "complete",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error checking status: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/client-groups/{group_id}
# ---------------------------------------------------------------------------

@router.get("/api/client-groups/{group_id}")
async def get_client_group_comprehensive(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    OPTIMIZED: Fetch comprehensive data for a single client group.

    Performance improvements:
    - Parallel fetching of all integrations (GHL, Meta, HP)
    - Single-pass lead processing
    - Efficient caching strategy (1.5 days = 129600 seconds)
    - Reduced database queries
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # STEP 1: Fetch client group and check cache
            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user,
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            CACHE_DURATION_SECONDS = 929600
            cache_age = None
            has_fresh_cache = False

            if all([
                group.get("gohighlevel_cache"),
                group.get("facebook_cache"),
                group.get("last_ghl_refresh"),
            ]):
                refresh_times = [
                    t for t in [
                        group.get("last_ghl_refresh"),
                        group.get("last_meta_refresh"),
                        group.get("last_hp_refresh"),
                    ]
                    if t is not None
                ]
                if refresh_times:
                    oldest_refresh = min(refresh_times)
                    cache_age = (datetime.utcnow() - oldest_refresh).total_seconds()
                    has_fresh_cache = cache_age < CACHE_DURATION_SECONDS

            # Initialize response structure
            response_data = {
                "group_info": mongo_to_dict(group),
                "ghl_data": {},
                "meta_data": {},
                "hotprospector_data": {},
                "insights": {},
                "marketing": {},
                "leads": {},
                "call_center": {},
                "cache_info": {
                    "used_cache": has_fresh_cache,
                    "cache_age_seconds": cache_age,
                    "cache_duration_seconds": CACHE_DURATION_SECONDS,
                },
            }

            # STEP 2: Parallel data fetching
            async def fetch_ghl_data():
                """Fetch GHL data with caching"""
                if not group.get("ghl_location_id"):
                    return None

                try:
                    ghl_location_id = group["ghl_location_id"]

                    if has_fresh_cache and group.get("gohighlevel_cache"):
                        cached = group["gohighlevel_cache"]
                        return {
                            "location_id": ghl_location_id,
                            "location_name": cached.get("name", ""),
                            "location_address": cached.get("address"),
                            "total_contacts": cached.get("metrics", {}).get("total_contacts", 0),
                            "contacts": [],
                            "status_breakdown": {"Open": 0, "Won": 0, "Abandoned": 0, "Lost": 0},
                            "total_value": 0,
                            "from_cache": True,
                        }

                    subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
                    location_data = subaccount_tokens.get(ghl_location_id, {})

                    contacts = location_data.get("contacts", [])
                    contacts_updated_at = location_data.get("contacts_updated_at")
                    cache_valid = contacts_updated_at and (datetime.now() - contacts_updated_at).total_seconds() < CACHE_DURATION_SECONDS

                    if not cache_valid and location_data.get("access_token"):
                        success, fresh_contacts = await ghl_integration.fetch_location_contacts(
                            ghl_location_id, location_data.get("access_token"), limit=100
                        )
                        if success:
                            contacts = fresh_contacts

                    ghl_leads = []
                    contact_status_counts = {"Open": 0, "Won": 0, "Abandoned": 0, "Lost": 0}
                    total_contact_value = 0

                    for contact in contacts:
                        contact_status = contact.get("contactType", "Open")
                        if contact_status not in contact_status_counts:
                            contact_status = "Open"
                        contact_status_counts[contact_status] += 1

                        contact_value = 0
                        custom_fields = contact.get("customField", {})
                        if isinstance(custom_fields, dict):
                            try:
                                contact_value = float(custom_fields.get("value", 0) or 0)
                            except (ValueError, TypeError):
                                contact_value = 0
                        total_contact_value += contact_value

                        ghl_leads.append({
                            "id": contact.get("id"),
                            "name": contact.get("fullName", ""),
                            "email": contact.get("email", ""),
                            "phone": contact.get("phone", ""),
                            "status": contact_status,
                            "source": "GHL",
                            "created": contact.get("dateAdded", ""),
                            "tags": contact.get("tags", []),
                            "value": contact_value,
                            "businessName": location_data.get("name", ""),
                            "contactType": contact.get("contactType", "contact"),
                            "website": contact.get("website", ""),
                            "address1": contact.get("address1", ""),
                            "country": contact.get("country", ""),
                        })

                    return {
                        "location_id": ghl_location_id,
                        "location_name": location_data.get("name", ""),
                        "location_address": location_data.get("address"),
                        "total_contacts": len(contacts),
                        "contacts": ghl_leads,
                        "status_breakdown": contact_status_counts,
                        "total_value": total_contact_value,
                        "from_cache": False,
                    }

                except Exception as e:
                    logger.error(f"Error fetching GHL data: {str(e)}")
                    return None

            async def fetch_meta_data():
                """Fetch Meta data with caching"""
                if not group.get("meta_ad_account_id"):
                    return None

                try:
                    meta_ad_account_id = group["meta_ad_account_id"]

                    if has_fresh_cache and group.get("facebook_cache"):
                        cached = group["facebook_cache"]
                        primary_preset = (
                            cached.get("data_maximum")
                            or cached.get("maximum")
                            or {}
                        )
                        if primary_preset:
                            summary_insights = primary_preset.get("metrics", {}).get("insights", {})
                            campaigns = primary_preset.get("campaigns", [])
                            adsets = primary_preset.get("adsets", [])
                            ads = primary_preset.get("ads", [])
                        else:
                            summary_insights = cached.get("metrics", {}).get("insights", {})
                            campaigns = cached.get("campaigns", [])
                            adsets = cached.get("adsets", [])
                            ads = cached.get("ads", [])
                        return {
                            "ad_account_id": meta_ad_account_id,
                            "campaigns": campaigns,
                            "adsets": adsets,
                            "ads": ads,
                            "leads": [],
                            "summary": summary_insights,
                            "from_cache": True,
                        }

                    token = await get_facebook_token(current_user, mongo_client)
                    if not token or not token.get("access_token"):
                        return None

                    account_data = await get_facebook_data(
                        current_user, meta_ad_account_id, "account_data", mongo_client
                    )

                    if not account_data:
                        async with httpx.AsyncClient(timeout=300.0) as client:
                            response = await client.get(
                                f"https://graph.facebook.com/v25.0/{meta_ad_account_id}/campaigns",
                                params={
                                    "fields": "name,status,insights.date_preset(maximum){actions,spend,results,reach,impressions,cpm,clicks,cpc,ctr},adsets{name,status,insights.date_preset(maximum){actions,spend,results,impressions,cpm,clicks,cpc,ctr}},ads{name,adset_id,status,insights.date_preset(maximum){actions,spend,results,impressions,cpm,clicks,cpc,ctr}}",
                                    "access_token": token["access_token"],
                                },
                            )
                            if response.status_code == 200:
                                account_data = response.json()
                                await save_facebook_data(
                                    current_user,
                                    meta_ad_account_id,
                                    account_data,
                                    "account_data",
                                    mongo_client,
                                )

                    campaigns_list = []
                    adsets_list = []
                    ads_list = []

                    total_spend = 0
                    total_impressions = 0
                    total_clicks = 0
                    total_reach = 0
                    total_results = 0

                    if account_data:
                        for campaign in account_data.get("data", []):
                            campaign_insights = campaign.get("insights", {}).get("data", [])
                            campaign_spend = 0
                            campaign_impressions = 0
                            campaign_clicks = 0
                            campaign_reach = 0
                            campaign_results = 0

                            if campaign_insights:
                                insight = campaign_insights[0]
                                try:
                                    campaign_spend = float(insight.get("spend", 0) or 0)
                                    campaign_impressions = int(insight.get("impressions", 0) or 0)
                                    campaign_clicks = int(insight.get("clicks", 0) or 0)
                                    campaign_reach = int(insight.get("reach", 0) or 0)
                                    campaign_results = get_result_value(campaign_insights, "lead")

                                    total_spend += campaign_spend
                                    total_impressions += campaign_impressions
                                    total_clicks += campaign_clicks
                                    total_reach += campaign_reach
                                    total_results += campaign_results
                                except (ValueError, TypeError):
                                    pass

                            campaigns_list.append({
                                "id": campaign.get("id"),
                                "name": campaign.get("name"),
                                "status": campaign.get("status", "").title(),
                                "spend": round(campaign_spend, 2),
                                "impressions": campaign_impressions,
                                "clicks": campaign_clicks,
                                "reach": campaign_reach,
                                "results": campaign_results,
                                "cpm": round((campaign_spend / campaign_impressions * 1000), 2) if campaign_impressions > 0 else 0,
                                "cpc": round((campaign_spend / campaign_clicks), 2) if campaign_clicks > 0 else 0,
                                "ctr": round((campaign_clicks / campaign_impressions * 100), 2) if campaign_impressions > 0 else 0,
                                "leads": campaign_results,
                            })

                            for adset in campaign.get("adsets", {}).get("data", []):
                                adset_insights = adset.get("insights", {}).get("data", [])
                                if adset_insights:
                                    insight = adset_insights[0]
                                    adsets_list.append({
                                        "id": adset.get("id"),
                                        "name": adset.get("name"),
                                        "status": adset.get("status", "").title(),
                                        "spend": round(float(insight.get("spend", 0) or 0), 2),
                                        "impressions": int(insight.get("impressions", 0) or 0),
                                        "clicks": int(insight.get("clicks", 0) or 0),
                                    })

                            for ad in campaign.get("ads", {}).get("data", []):
                                ad_insights = ad.get("insights", {}).get("data", [])
                                if ad_insights:
                                    insight = ad_insights[0]
                                    ads_list.append({
                                        "id": ad.get("id"),
                                        "campaign_id": campaign.get("id"),
                                        "adset_id": ad.get("adset_id"),
                                        "name": ad.get("name"),
                                        "status": ad.get("status", "").title(),
                                        "spend": round(float(insight.get("spend", 0) or 0), 2),
                                        "impressions": int(insight.get("impressions", 0) or 0),
                                        "clicks": int(insight.get("clicks", 0) or 0),
                                    })

                        leads_data = await get_facebook_data(
                            current_user, meta_ad_account_id, "leads", mongo_client
                        )

                        if not leads_data:
                            leads_data = []

                        total_leads = len(leads_data) if isinstance(leads_data, list) else 0

                        avg_cpm = (total_spend / total_impressions * 1000) if total_impressions > 0 else 0
                        avg_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
                        avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
                        cost_per_lead = (total_spend / total_leads) if total_leads > 0 else 0

                        return {
                            "ad_account_id": meta_ad_account_id,
                            "campaigns": campaigns_list,
                            "adsets": adsets_list,
                            "ads": ads_list,
                            "leads": leads_data,
                            "summary": {
                                "total_campaigns": len(campaigns_list),
                                "total_adsets": len(adsets_list),
                                "total_ads": len(ads_list),
                                "total_leads": total_leads,
                                "total_spend": round(total_spend, 2),
                                "total_impressions": total_impressions,
                                "total_clicks": total_clicks,
                                "total_reach": total_reach,
                                "total_results": total_results,
                                "avg_cpm": round(avg_cpm, 2),
                                "avg_cpc": round(avg_cpc, 2),
                                "avg_ctr": round(avg_ctr, 2),
                                "cost_per_lead": round(cost_per_lead, 2),
                            },
                            "from_cache": False,
                        }

                    return None

                except Exception as e:
                    logger.error(f"Error fetching Meta data: {str(e)}")
                    return None

            async def fetch_hp_data():
                """Fetch HotProspector data with caching"""
                if not group.get("ghl_location_id"):
                    return None

                try:
                    hp_credentials = await get_hotprospector_credentials(current_user, mongo_client)
                    if not hp_credentials:
                        return None

                    ghl_location_id = group["ghl_location_id"]

                    if has_fresh_cache and group.get("hotprospector_cache"):
                        cached = group["hotprospector_cache"]
                        return {
                            "ghl_location_id": ghl_location_id,
                            "total_leads": cached.get("metrics", {}).get("total_leads", 0),
                            "leads": [],
                            "total_calls": 0,
                            "from_cache": True,
                        }

                    cached_leads, total_count = await get_hotprospector_leads_from_collection(
                        current_user, ghl_location_id, mongo_client, skip=0, limit=None
                    )

                    if not cached_leads:
                        integration = HotProspectorIntegration(
                            hp_credentials.get("api_uid"),
                            hp_credentials.get("api_key"),
                        )

                        success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)

                        if success:
                            location_name = response_data.get("ghl_data", {}).get("location_name", "")
                            cached_leads = [
                                integration.normalize_lead(lead, ghl_location_id, location_name, group.get("name"))
                                for lead in hp_leads
                            ]

                            await save_hotprospector_leads_to_collection(
                                current_user, ghl_location_id, cached_leads, mongo_client
                            )

                    if cached_leads:
                        lead_ids = [str(lead.get("id")) for lead in cached_leads if lead.get("id")]
                        integration = HotProspectorIntegration(
                            hp_credentials.get("api_uid"),
                            hp_credentials.get("api_key"),
                        )
                        call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                        for lead in cached_leads:
                            lead_id = str(lead.get("id"))
                            lead["call_logs"] = call_logs_map.get(lead_id, [])
                            lead["call_logs_count"] = len(lead["call_logs"])

                    total_calls = sum(lead.get("call_logs_count", 0) for lead in (cached_leads or []))

                    return {
                        "ghl_location_id": ghl_location_id,
                        "total_leads": len(cached_leads) if cached_leads else 0,
                        "leads": cached_leads or [],
                        "total_calls": total_calls,
                        "from_cache": False,
                    }

                except Exception as e:
                    logger.error(f"Error fetching HP data: {str(e)}")
                    return None

            # Execute all fetches in parallel
            ghl_data, meta_data, hp_data = await asyncio.gather(
                fetch_ghl_data(),
                fetch_meta_data(),
                fetch_hp_data(),
                return_exceptions=True,
            )

            if isinstance(ghl_data, Exception):
                logger.error(f"GHL fetch failed: {ghl_data}")
                ghl_data = None
            if isinstance(meta_data, Exception):
                logger.error(f"Meta fetch failed: {meta_data}")
                meta_data = None
            if isinstance(hp_data, Exception):
                logger.error(f"HP fetch failed: {hp_data}")
                hp_data = None

            if ghl_data:
                response_data["ghl_data"] = ghl_data
            if meta_data:
                response_data["meta_data"] = meta_data
            if hp_data:
                response_data["hotprospector_data"] = hp_data

            # STEP 3: Build aggregated views (single pass)
            all_leads = []

            if ghl_data and ghl_data.get("contacts"):
                all_leads.extend(ghl_data["contacts"])

            if meta_data and meta_data.get("leads"):
                for lead in meta_data["leads"]:
                    all_leads.append({
                        "id": lead.get("id"),
                        "name": lead.get("full_name", ""),
                        "email": lead.get("email", ""),
                        "phone": lead.get("phone_number", ""),
                        "status": "Open",
                        "created": lead.get("created_time", ""),
                        "businessName": group.get("name", ""),
                        "callCount": 0,
                        "lastContact": "N/A",
                        "value": 0,
                    })

            if hp_data and hp_data.get("leads"):
                hp_leads_dict = {
                    (lead.get("email") or "").lower().strip(): lead
                    for lead in hp_data["leads"]
                    if lead.get("email")
                }

                for lead in all_leads:
                    email = (lead.get("email") or "").lower().strip()
                    if email in hp_leads_dict:
                        hp_lead = hp_leads_dict[email]
                        lead["callCount"] = hp_lead.get("call_logs_count", 0)
                        lead["call_logs"] = hp_lead.get("call_logs", [])
                        if hp_lead.get("call_logs"):
                            last_call = max(hp_lead["call_logs"], key=lambda x: x.get("created_date", ""))
                            lead["lastContact"] = last_call.get("created_date", "N/A")

            status_breakdown = ghl_data.get("status_breakdown", {}) if ghl_data else {}
            response_data["insights"] = {
                "tag_with_best_roas": {
                    "tag_name": "Summer Sale",
                    "roas": 3.5,
                    "change_percentage": 15,
                },
                "offer_with_best_booking_rate": {
                    "offer_name": "Free Consultation",
                    "booking_rate": 0.18,
                    "change_percentage": 8,
                },
                "avg_booking_delay_days": {
                    "days": 2.3,
                    "change_percentage": 12,
                },
                "total_bookings_this_month": {
                    "count": status_breakdown.get("Won", 0),
                    "change_percentage": 18,
                },
            }

            response_data["marketing"] = {
                "campaigns": meta_data.get("campaigns", []) if meta_data else [],
                "adsets": meta_data.get("adsets", []) if meta_data else [],
                "ads": meta_data.get("ads", []) if meta_data else [],
                "summary": meta_data.get("summary", {}) if meta_data else {},
            }

            response_data["leads"] = {
                "all_leads": all_leads,
                "status_breakdown": status_breakdown,
                "total_leads": len(all_leads),
                "qualified_leads": status_breakdown.get("Won", 0),
                "conversion_rate": round(
                    (status_breakdown.get("Won", 0) / len(all_leads) * 100) if len(all_leads) > 0 else 0,
                    2,
                ),
            }

            total_calls = hp_data.get("total_calls", 0) if hp_data else 0
            response_data["call_center"] = {
                "total_calls": total_calls,
                "leads_with_calls": sum(1 for lead in all_leads if lead.get("callCount", 0) > 0),
                "avg_calls_per_lead": round(
                    (total_calls / len(all_leads)) if len(all_leads) > 0 else 0,
                    2,
                ),
                "leads_by_call_count": all_leads,
            }

            elapsed = time.time() - start_time
            logger.info(
                f"Fetched comprehensive data for group {group_id} in {elapsed:.2f}s "
                f"(cache: {has_fresh_cache}, {len(all_leads)} leads, {total_calls} calls)"
            )

            return {
                "success": True,
                "data": response_data,
                "message": f"Successfully fetched comprehensive data for client group {group_id}",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                    "used_cache": has_fresh_cache,
                    "cache_age_seconds": cache_age,
                    "cache_duration_seconds": CACHE_DURATION_SECONDS,
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching comprehensive data for client group {group_id}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch client group data: {str(e)}")


# ---------------------------------------------------------------------------
# PATCH /api/client-groups/{group_id}/notes
# ---------------------------------------------------------------------------

@router.patch("/api/client-groups/{group_id}/notes")
async def update_client_group_notes(
    group_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Update notes for a client group"""
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            notes = body.get("notes", "")

            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            result = await client_groups_collection.update_one(
                {"id": group_id, "user_id": current_user},
                {
                    "$set": {
                        "notes": notes,
                        "updated_at": datetime.now(),
                    }
                },
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Client group not found")

            return {"message": "Notes updated successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating notes: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to update notes: {str(e)}")


# ---------------------------------------------------------------------------
# POST /api/prefetch-data
# ---------------------------------------------------------------------------

@router.post("/api/prefetch-data")
async def prefetch_marketing_data(
    current_user: str = Depends(get_current_user),
):
    """Prefetch all marketing data in the background"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups = await db["client_groups"].find(
                {"user_id": current_user},
                {"meta_ad_account_id": 1},
            ).to_list(None)

            account_ids = list(set(
                group["meta_ad_account_id"]
                for group in client_groups
                if group.get("meta_ad_account_id")
            ))

            if not account_ids:
                return {"message": "No ad accounts to prefetch", "prefetched": 0}

            token = await get_facebook_token(current_user, mongo_client)
            if not token:
                return {"message": "No Meta token available", "prefetched": 0}

            asyncio.create_task(
                fetch_all_accounts_parallel(
                    current_user, mongo_client, token, account_ids
                )
            )

            return {
                "message": "Prefetch started",
                "account_ids": account_ids,
                "prefetched": len(account_ids),
                "note": "Data will be cached in ~2-5 seconds",
            }

        except Exception as e:
            logger.error(f"Prefetch error: {str(e)}")
            return {"message": "Prefetch failed", "error": str(e), "prefetched": 0}


# ---------------------------------------------------------------------------
# GET /api/contacts/ghl-paginated
# ---------------------------------------------------------------------------

@router.get("/api/contacts/ghl-paginated")
async def get_ghl_contacts_paginated_v2(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=15, ge=1, le=500),
    groups: str = Query(default=""),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    """
    Fetch GHL contacts with currency conversion on opportunities.
    Returns contacts in DESCENDING order (newest first).
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            contacts_collection = db["ghl_contacts"]
            users_collection = db["users"]

            # Fetch user's default currency
            user_doc = await users_collection.find_one(
                {"user_id": current_user},
                {"default_currency": 1},
            )
            user_currency = user_doc.get("default_currency") if user_doc else None
            logger.info(f"User [{current_user}] default currency: {user_currency}")

            # Parse group IDs
            group_ids = [g.strip() for g in groups.split(",") if g.strip()] if groups else []

            # Build query
            query = {"user_id": current_user}

            if group_ids:
                query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}

                if start_date:
                    try:
                        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                        start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        date_filter["$gte"] = start_dt.isoformat()
                    except ValueError:
                        logger.warning(f"Invalid start_date format: {start_date}")

                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
                        date_filter["$lte"] = end_dt.isoformat()
                    except ValueError:
                        logger.warning(f"Invalid end_date format: {end_date}")

                if date_filter:
                    query["contact_data.dateAdded"] = date_filter

            # Get total count + aggregate opportunity stats across ALL matching contacts
            total_contacts = await contacts_collection.count_documents(query)
            logger.info(f"Total contacts found: {total_contacts} for user [{current_user}]")

            # Aggregate opportunity stats (runs on the full query, not paginated)
            stats_pipeline = [
                {"$match": query},
                {"$unwind": {"path": "$contact_data.opportunities", "preserveNullAndEmptyArrays": False}},
                {"$group": {
                    "_id": {"$ifNull": ["$contact_data.opportunities.status", "open"]},
                    "count": {"$sum": 1},
                    "total_value": {"$sum": {"$ifNull": [{"$toDouble": "$contact_data.opportunities.monetaryValue"}, 0]}},
                }},
            ]
            stats_cursor = contacts_collection.aggregate(stats_pipeline)
            stats_docs = await stats_cursor.to_list(length=20)

            opportunity_stats = {"won": 0, "lost": 0, "open": 0, "abandoned": 0}
            total_value = 0
            for doc in stats_docs:
                status = doc["_id"]
                if status in opportunity_stats:
                    opportunity_stats[status] = doc["count"]
                total_value += doc.get("total_value", 0)

            total_opportunities = sum(opportunity_stats.values())

            if total_contacts == 0:
                return {
                    "contacts": [],
                    "meta": {
                        "total_contacts": 0,
                        "current_page": page,
                        "total_pages": 0,
                        "per_page": limit,
                        "has_next": False,
                        "has_prev": False,
                        "stats": {
                            "total_opportunities": 0,
                            "won": 0,
                            "lost": 0,
                            "open": 0,
                            "abandoned": 0,
                            "conversion_rate": 0,
                            "total_value": 0,
                        },
                    },
                    "message": "No contacts found",
                }

            # Calculate pagination
            skip = (page - 1) * limit
            total_pages = (total_contacts + limit - 1) // limit

            # Fetch contacts (sorted newest first)
            cursor = contacts_collection.find(
                query,
                {
                    "contact_data": 1,
                    "client_group_name": 1,
                    "location_name": 1,
                    "client_group_id": 1,
                    "location_id": 1,
                },
            ).sort("contact_data.dateAdded", -1).skip(skip).limit(limit)

            contact_docs = await cursor.to_list(length=limit)
            logger.info(f"Page {page}: fetched {len(contact_docs)} contacts | user_currency={user_currency}")

            # Helper: convert opportunity -- GBP is the default source currency
            def convert_opportunity(opp: dict) -> dict:
                if not user_currency or not opp:
                    return opp

                opp = opp.copy()
                monetary_fields = ["monetaryValue", "value", "amount"]
                opp_id = opp.get("id", "unknown")

                opp_currency = "GBP"

                if opp_currency == user_currency:
                    opp["display_currency"] = user_currency
                    return opp

                from utils.currency_exchange import CurrencyService
                for field in monetary_fields:
                    if field in opp and opp[field] is not None:
                        try:
                            original = float(opp[field])
                            converted = CurrencyService.convert(
                                amount=original,
                                from_currency=opp_currency,
                                to_currency=user_currency,
                            )
                            opp[f"{field}_original"] = original
                            opp[f"{field}_original_currency"] = opp_currency
                            opp[field] = converted
                            logger.info(
                                f"Opportunity [{opp_id}] {field}: "
                                f"{opp_currency} {original} -> {user_currency} {converted}"
                            )
                        except (ValueError, TypeError, Exception) as e:
                            logger.error(f"Opportunity [{opp_id}] failed to convert {field}: {e}")

                opp["display_currency"] = user_currency
                opp["original_currency"] = opp_currency
                return opp

            # Format contacts
            contacts = []
            for doc in contact_docs:
                contact = doc.get("contact_data", {})

                # Normalize email
                email = contact.get("email")
                if email is None or (isinstance(email, str) and not email.strip()):
                    email = f"no_email_ghl_{contact.get('id')}"
                elif isinstance(email, str):
                    email = email.strip().lower()

                # Normalize name
                contact_name = contact.get("contactName", "") or ""
                if isinstance(contact_name, str):
                    contact_name = contact_name.strip()

                if not contact_name:
                    first_name = (contact.get("firstName") or "").strip()
                    last_name = (contact.get("lastName") or "").strip()
                    contact_name = f"{first_name} {last_name}".strip() if first_name or last_name else "Unknown"

                # Convert opportunities
                raw_opportunities = contact.get("opportunities") or []
                logger.info(
                    f"Contact [{contact.get('id')}] '{contact_name}' "
                    f"has {len(raw_opportunities)} opportunities"
                )
                converted_opportunities = [convert_opportunity(opp) for opp in raw_opportunities]

                formatted_contact = {
                    # Basic info
                    "contactId": contact.get("id"),
                    "contactName": contact_name,
                    "firstName": contact.get("firstName") or "",
                    "lastName": contact.get("lastName") or "",
                    "email": email,
                    "phone": contact.get("phone") or "",

                    # Location/group info
                    "locationId": doc.get("location_id"),
                    "groupName": doc.get("client_group_name", "Unknown Group"),

                    # Dates
                    "dateAdded": contact.get("dateAdded") or "",
                    "dateUpdated": contact.get("dateUpdated") or "",
                    "dateOfBirth": contact.get("dateOfBirth") or "",

                    # Contact details
                    "tags": contact.get("tags") or [],
                    "source": contact.get("source") or "",
                    "type": contact.get("type") or "lead",
                    "contactType": contact.get("contactType") or contact.get("type") or "lead",

                    # Address
                    "address1": contact.get("address1") or "",
                    "city": contact.get("city") or "",
                    "state": contact.get("state") or "",
                    "postalCode": contact.get("postalCode") or "",
                    "country": contact.get("country") or "",
                    "timezone": contact.get("timezone") or "",

                    # Business info
                    "companyName": contact.get("companyName") or "",
                    "website": contact.get("website") or "",
                    "businessId": contact.get("businessId") or "",

                    # Additional fields
                    "dnd": contact.get("dnd", False),
                    "dndSettings": contact.get("dndSettings") or {},
                    "customFields": contact.get("customFields") or [],
                    "followers": contact.get("followers") or [],
                    "assignedTo": contact.get("assignedTo") or "",

                    # Converted opportunities
                    "opportunities": converted_opportunities,

                    # Attribution
                    "attributionSource": contact.get("attributionSource") or {},
                    "lastAttributionSource": contact.get("lastAttributionSource") or {},

                    # Currency metadata
                    "display_currency": user_currency,
                }

                contacts.append(formatted_contact)

            elapsed = time.time() - start_time

            logger.info(
                f"Fetched page {page} (newest first): {len(contacts)} contacts "
                f"in {elapsed:.3f}s"
            )

            return {
                "contacts": contacts,
                "meta": {
                    "total_contacts": total_contacts,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(contacts),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "newest_first",
                    "date_filtered": bool(start_date or end_date),
                    "start_date": start_date,
                    "end_date": end_date,
                    "display_currency": user_currency,
                    "stats": {
                        "total_opportunities": total_opportunities,
                        "won": opportunity_stats["won"],
                        "lost": opportunity_stats["lost"],
                        "open": opportunity_stats["open"],
                        "abandoned": opportunity_stats["abandoned"],
                        "conversion_rate": round((opportunity_stats["won"] / total_opportunities) * 100, 1) if total_opportunities > 0 else 0,
                        "total_value": round(total_value, 2),
                    },
                },
                "message": f"Retrieved {len(contacts)} contacts (newest first)"
                           + (f" from {start_date} to {end_date}" if (start_date or end_date) else ""),
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                    "source": "mongodb",
                },
            }

        except Exception as e:
            logger.error(
                f"Error fetching paginated contacts for user {current_user}: {str(e)}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch contacts: {str(e)}",
            )


# ---------------------------------------------------------------------------
# GET /api/campaigns/tag-rollup  — Tag counts per campaign/adset/ad
# ---------------------------------------------------------------------------

@router.get("/api/campaigns/tag-rollup")
async def get_campaign_tag_rollup(
    groups: str = Query(default=""),
    current_user: str = Depends(get_current_user),
):
    """
    Aggregate GHL contact tags by campaign/adset/ad attribution.
    Returns tag counts grouped by campaignId, adset (utmMedium), and adId.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]

            group_ids = [g.strip() for g in groups.split(",") if g.strip()] if groups else []
            match_q = {"user_id": current_user}
            if group_ids:
                match_q["client_group_id"] = {"$in": group_ids}

            pipeline = [
                {"$match": match_q},
                {"$unwind": {"path": "$contact_data.tags", "preserveNullAndEmptyArrays": False}},
                {"$group": {
                    "_id": {
                        "campaign_id": {"$ifNull": ["$contact_data.attributionSource.campaignId", ""]},
                        "adset": {"$ifNull": ["$contact_data.attributionSource.utmMedium", ""]},
                        "ad_id": {"$ifNull": ["$contact_data.attributionSource.adId", ""]},
                        "tag": "$contact_data.tags",
                    },
                    "count": {"$sum": 1},
                }},
            ]

            docs = await db["ghl_contacts"].aggregate(pipeline).to_list(None)

            by_campaign = {}
            by_adset = {}
            by_ad = {}

            for doc in docs:
                tag = doc["_id"]["tag"]
                count = doc["count"]
                cid = doc["_id"]["campaign_id"]
                adset = doc["_id"]["adset"]
                aid = doc["_id"]["ad_id"]

                if cid:
                    by_campaign.setdefault(cid, {})[tag] = by_campaign.get(cid, {}).get(tag, 0) + count
                if adset:
                    by_adset.setdefault(adset, {})[tag] = by_adset.get(adset, {}).get(tag, 0) + count
                if aid:
                    by_ad.setdefault(aid, {})[tag] = by_ad.get(aid, {}).get(tag, 0) + count

            return {
                "by_campaign": by_campaign,
                "by_adset": by_adset,
                "by_ad": by_ad,
            }

        except Exception as e:
            logger.error(f"Error computing tag rollup: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/leads/unified  — Cross-source lead matching
# ---------------------------------------------------------------------------

@router.get("/api/leads/unified")
async def get_unified_leads(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=15, ge=1, le=500),
    groups: str = Query(default=""),
    start_date: Optional[str] = Query(default=None),
    end_date: Optional[str] = Query(default=None),
    current_user: str = Depends(get_current_user),
):
    """
    Unified leads endpoint: returns GHL contacts enriched with Meta and HP data.
    Matches across sources using normalized email/phone match_keys.
    """
    import time as _time
    async with get_mongo_client() as mongo_client:
        try:
            start_time = _time.time()
            db = mongo_client[DB_NAME]
            ghl_col = db["ghl_contacts"]
            meta_col = db["facebook_leads"]
            hp_col = db["hotprospector_leads"]

            group_ids = [g.strip() for g in groups.split(",") if g.strip()] if groups else []

            # Build GHL query (same as ghl-paginated)
            query = {"user_id": current_user}
            if group_ids:
                query["client_group_id"] = {"$in": group_ids}
            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = f"{start_date}T00:00:00.000Z"
                if end_date:
                    date_filter["$lte"] = f"{end_date}T23:59:59.999Z"
                query["contact_data.dateAdded"] = date_filter

            # Paginate GHL contacts
            total_contacts = await ghl_col.count_documents(query)
            total_pages = max(1, -(-total_contacts // limit))
            skip = (page - 1) * limit

            contact_docs = await ghl_col.find(query).sort(
                "contact_data.dateAdded", -1
            ).skip(skip).limit(limit).to_list(limit)

            # Collect all match_keys from this page
            all_keys = set()
            for doc in contact_docs:
                for key in (doc.get("match_keys") or []):
                    all_keys.add(key)

            # Parallel lookup in Meta and HP collections
            meta_matches = {}
            hp_matches = {}

            if all_keys:
                keys_list = list(all_keys)
                meta_q = {"user_id": current_user, "match_keys": {"$in": keys_list}}
                hp_q = {"user_id": current_user, "match_keys": {"$in": keys_list}}

                meta_docs, hp_docs = await asyncio.gather(
                    meta_col.find(meta_q).to_list(None),
                    hp_col.find(hp_q).to_list(None),
                )

                # Build ad→campaign/adset lookup from facebook_cache for resolving missing names
                ad_to_meta = {}  # ad_id → {campaign_name, adset_name, campaign_id, adset_id}
                # Load ALL user's groups (not just current page) since a Meta lead
                # might come from a different group's ad account
                if True:
                    group_docs = await db["client_groups"].find(
                        {"user_id": current_user},
                        {"facebook_cache.maximum.ads": 1, "facebook_cache.maximum.campaigns": 1, "facebook_cache.maximum.adsets": 1,
                         "facebook_cache.ads": 1, "facebook_cache.campaigns": 1, "facebook_cache.adsets": 1}
                    ).to_list(None)
                    for gdoc in group_docs:
                        fb = gdoc.get("facebook_cache") or {}
                        preset = fb.get("maximum") or fb
                        # Build campaign_id → name map
                        camp_map = {c["id"]: c.get("name", "") for c in (preset.get("campaigns") or [])}
                        # Build adset_id → {name, campaign_id} map
                        adset_map = {a["id"]: {"name": a.get("name", ""), "campaign_id": a.get("campaign_id", "")} for a in (preset.get("adsets") or [])}
                        # Build ad_id AND ad_name → full resolution
                        ad_name_to_meta = {}  # ad_name → resolution (fallback when ad_id is missing)
                        for ad in (preset.get("ads") or []):
                            cid = ad.get("campaign_id", "")
                            asid = ad.get("adset_id", "")
                            resolution = {
                                "campaign_name": camp_map.get(cid, ""),
                                "campaign_id": cid,
                                "adset_name": adset_map.get(asid, {}).get("name", ""),
                                "adset_id": asid,
                                "ad_name": ad.get("name", ""),
                            }
                            aid = ad.get("id")
                            if aid:
                                ad_to_meta[aid] = resolution
                            aname = ad.get("name")
                            if aname:
                                ad_name_to_meta[aname] = resolution

                # Build lookup by match_key → enrichment data
                for md in meta_docs:
                    ld = md.get("lead_data", {})

                    # Resolve missing campaign/adset from the cached ad hierarchy
                    campaign_name = ld.get("campaign_name") or ""
                    adset_name = ld.get("adset_name") or ""
                    campaign_id = ld.get("campaign_id") or ""
                    adset_id = ld.get("adset_id") or ""
                    ad_id = ld.get("ad_id") or ""

                    if not campaign_name or not adset_name:
                        # Try resolving by ad_id first, then by ad_name as fallback
                        resolved = None
                        if ad_id and ad_id in ad_to_meta:
                            resolved = ad_to_meta[ad_id]
                        elif ld.get("ad_name") and ld["ad_name"] in ad_name_to_meta:
                            resolved = ad_name_to_meta[ld["ad_name"]]
                        if resolved:
                            if not campaign_name:
                                campaign_name = resolved.get("campaign_name", "")
                                campaign_id = resolved.get("campaign_id", "") or campaign_id
                            if not adset_name:
                                adset_name = resolved.get("adset_name", "")
                                adset_id = resolved.get("adset_id", "") or adset_id
                            if not ad_id:
                                ad_id = resolved.get("ad_id", "") if "ad_id" in resolved else ad_id

                    enrichment = {
                        "campaign_name": campaign_name,
                        "campaign_id": campaign_id,
                        "ad_name": ld.get("ad_name", ""),
                        "ad_id": ad_id,
                        "adset_name": adset_name,
                        "adset_id": adset_id,
                        "created_time": ld.get("created_time", ""),
                        "form_id": ld.get("form_id", ""),
                    }
                    for key in (md.get("match_keys") or []):
                        meta_matches[key] = enrichment

                for hd in hp_docs:
                    ld = hd.get("lead_data", {})
                    enrichment = {
                        "call_logs_count": ld.get("call_logs_count", 0),
                        "last_call_date": "",
                    }
                    calls = ld.get("call_logs") or []
                    if calls:
                        enrichment["last_call_date"] = calls[0].get("call_time", "")
                    for key in (hd.get("match_keys") or []):
                        hp_matches[key] = enrichment

            # Opportunity stats (same as ghl-paginated)
            stats_pipeline = [
                {"$match": query},
                {"$unwind": {"path": "$contact_data.opportunities", "preserveNullAndEmptyArrays": False}},
                {"$group": {
                    "_id": {"$ifNull": ["$contact_data.opportunities.status", "open"]},
                    "count": {"$sum": 1},
                    "total_value": {"$sum": {"$ifNull": [{"$toDouble": "$contact_data.opportunities.monetaryValue"}, 0]}},
                }},
            ]
            stats_docs = await ghl_col.aggregate(stats_pipeline).to_list(20)
            opportunity_stats = {"won": 0, "lost": 0, "open": 0, "abandoned": 0}
            total_value = 0.0
            for doc in stats_docs:
                status = doc["_id"]
                if status in opportunity_stats:
                    opportunity_stats[status] = doc["count"]
                total_value += doc.get("total_value", 0)
            total_opportunities = sum(opportunity_stats.values())

            # Format contacts with enrichment
            contacts = []
            for doc in contact_docs:
                contact = doc.get("contact_data", {})
                doc_keys = doc.get("match_keys") or []

                # Check source matches
                has_meta = any(k in meta_matches for k in doc_keys)
                has_hp = any(k in hp_matches for k in doc_keys)

                # Get enrichment data (use first matching key)
                meta_enrich = None
                hp_enrich = None
                for k in doc_keys:
                    if k in meta_matches and not meta_enrich:
                        meta_enrich = meta_matches[k]
                    if k in hp_matches and not hp_enrich:
                        hp_enrich = hp_matches[k]

                # Fallback: if matched but still missing campaign/adset,
                # use GHL contact's attributionSource which has campaign + utmMedium (adset)
                if meta_enrich and (not meta_enrich.get("campaign_name") or not meta_enrich.get("adset_name")):
                    attr = contact.get("attributionSource") or {}
                    if not meta_enrich.get("campaign_name") and attr.get("campaign"):
                        meta_enrich["campaign_name"] = attr["campaign"]
                    if not meta_enrich.get("campaign_id") and attr.get("campaignId"):
                        meta_enrich["campaign_id"] = attr["campaignId"]
                    if not meta_enrich.get("adset_name") and attr.get("utmMedium"):
                        meta_enrich["adset_name"] = attr["utmMedium"]
                    if not meta_enrich.get("ad_name") and attr.get("utmContent"):
                        meta_enrich["ad_name"] = attr["utmContent"]
                    if not meta_enrich.get("ad_id") and attr.get("adId"):
                        meta_enrich["ad_id"] = attr["adId"]

                # Normalize email
                email = contact.get("email")
                if not email or (isinstance(email, str) and not email.strip()):
                    email = f"no_email_ghl_{contact.get('id')}"
                elif isinstance(email, str):
                    email = email.strip().lower()

                # Normalize name
                contact_name = contact.get("contactName", "") or ""
                if not contact_name:
                    first = (contact.get("firstName") or "").strip()
                    last = (contact.get("lastName") or "").strip()
                    contact_name = f"{first} {last}".strip() or "Unknown"

                formatted = {
                    "contactId": contact.get("id"),
                    "contactName": contact_name,
                    "firstName": contact.get("firstName") or "",
                    "lastName": contact.get("lastName") or "",
                    "email": email,
                    "phone": contact.get("phone") or "",
                    "locationId": doc.get("location_id"),
                    "groupName": doc.get("client_group_name", "Unknown Group"),
                    "dateAdded": contact.get("dateAdded") or "",
                    "dateUpdated": contact.get("dateUpdated") or "",
                    "dateOfBirth": contact.get("dateOfBirth") or "",
                    "tags": contact.get("tags") or [],
                    "source": contact.get("source") or "",
                    "type": contact.get("type") or "lead",
                    "contactType": contact.get("contactType") or contact.get("type") or "lead",
                    "address1": contact.get("address1") or "",
                    "city": contact.get("city") or "",
                    "state": contact.get("state") or "",
                    "postalCode": contact.get("postalCode") or "",
                    "country": contact.get("country") or "",
                    "timezone": contact.get("timezone") or "",
                    "companyName": contact.get("companyName") or "",
                    "website": contact.get("website") or "",
                    "businessId": contact.get("businessId") or "",
                    "dnd": contact.get("dnd", False),
                    "dndSettings": contact.get("dndSettings") or {},
                    "customFields": contact.get("customFields") or [],
                    "followers": contact.get("followers") or [],
                    "assignedTo": contact.get("assignedTo") or "",
                    "opportunities": contact.get("opportunities") or [],
                    "attributionSource": contact.get("attributionSource") or {},
                    "lastAttributionSource": contact.get("lastAttributionSource") or {},
                    # Cross-source enrichment
                    "sources": {
                        "ghl": True,
                        "meta": has_meta,
                        "hp": has_hp,
                    },
                    "meta_enrichment": meta_enrich,
                    "hp_enrichment": hp_enrich,
                }
                contacts.append(formatted)

            elapsed = _time.time() - start_time

            return {
                "contacts": contacts,
                "meta": {
                    "total_contacts": total_contacts,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(contacts),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "newest_first",
                    "date_filtered": bool(start_date or end_date),
                    "start_date": start_date,
                    "end_date": end_date,
                    "stats": {
                        "total_opportunities": total_opportunities,
                        "won": opportunity_stats["won"],
                        "lost": opportunity_stats["lost"],
                        "open": opportunity_stats["open"],
                        "abandoned": opportunity_stats["abandoned"],
                        "conversion_rate": round((opportunity_stats["won"] / total_opportunities) * 100, 1) if total_opportunities > 0 else 0,
                        "total_value": round(total_value, 2),
                    },
                },
                "message": f"Retrieved {len(contacts)} unified leads (newest first)",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                    "source": "unified",
                },
            }

        except Exception as e:
            logger.error(f"Error fetching unified leads: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch unified leads: {str(e)}")


# ---------------------------------------------------------------------------
# POST /api/client-groups/{group_id}/refresh/{integration}
# ---------------------------------------------------------------------------

@router.post("/api/client-groups/{group_id}/refresh/{integration}")
async def refresh_integration_data(
    group_id: str,
    integration: str,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
):
    """Kick off a full invalidate + refresh for a single integration on a group."""
    if integration not in ("meta", "ghl"):
        raise HTTPException(status_code=400, detail="integration must be 'meta' or 'ghl'")

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        group = await db.client_groups.find_one({"id": group_id, "user_id": current_user})
        if not group:
            raise HTTPException(status_code=404, detail="Client group not found")

        status_field = f"{integration}_refresh_status"
        await db.client_groups.update_one(
            {"id": group_id},
            {"$set": {status_field: "running"}},
        )

    background_tasks.add_task(_run_refresh, group_id, integration, current_user, group)
    return {"status": "started", "integration": integration}


async def _run_refresh(group_id: str, integration: str, user_id: str, group: dict):
    """Background task: full invalidate + refresh for one integration."""
    status_field = f"{integration}_refresh_status"
    refresh_field = f"last_{integration}_refresh"

    try:
        async with get_mongo_client() as mongo_client:
            db = mongo_client[DB_NAME]

            if integration == "meta":
                from services.meta_refresh_manager import start_refresh

                ad_account_id = group.get("meta_ad_account_id")
                if not ad_account_id:
                    logger.warning(f"No Meta ad account for group {group_id}")
                    await db.client_groups.update_one(
                        {"id": group_id}, {"$set": {status_field: "error"}}
                    )
                    return

                # Delegate to the resilient refresh manager
                await start_refresh(group_id, user_id, group, mongo_client)
                return  # manager handles status updates

            elif integration == "ghl":
                location_id = group.get("ghl_location_id")
                if not location_id:
                    logger.warning(f"No GHL location for group {group_id}")
                    await db.client_groups.update_one(
                        {"id": group_id}, {"$set": {status_field: "error"}}
                    )
                    return

                await fetch_and_cache_ghl_data_optimized(
                    group_id=group_id,
                    ghl_location_id=location_id,
                    user_id=user_id,
                    mongo_client=mongo_client,
                    is_initial_load=True,
                )

            # Mark complete
            await db.client_groups.update_one(
                {"id": group_id},
                {"$set": {
                    status_field: "complete",
                    refresh_field: datetime.utcnow(),
                }},
            )
            logger.info(f"Refresh complete: {integration} for group {group_id}")

    except Exception as e:
        logger.error(f"Refresh failed: {integration} for group {group_id}: {e}", exc_info=True)
        error_msg = str(e)[:300]
        try:
            async with get_mongo_client() as mc:
                await mc[DB_NAME].client_groups.update_one(
                    {"id": group_id},
                    {"$set": {
                        status_field: "error",
                        f"{integration}_refresh_error": error_msg,
                    }},
                )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GET /api/client-groups/{group_id}/refresh-status
# ---------------------------------------------------------------------------

@router.get("/api/client-groups/{group_id}/refresh-status")
async def get_refresh_status(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """Poll the refresh status for a group's integrations."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        group = await db.client_groups.find_one(
            {"id": group_id, "user_id": current_user},
            {
                "meta_refresh_status": 1, "ghl_refresh_status": 1,
                "meta_refresh_error": 1, "ghl_refresh_error": 1,
                "meta_token_error": 1, "_id": 0,
            },
        )
        if not group:
            raise HTTPException(status_code=404, detail="Client group not found")

        # Get granular Meta refresh progress from job tracker
        from services.meta_refresh_manager import get_refresh_progress
        meta_progress = await get_refresh_progress(group_id, mongo_client)

        return {
            "meta_refresh_status": group.get("meta_refresh_status", "idle"),
            "ghl_refresh_status": group.get("ghl_refresh_status", "idle"),
            "meta_refresh_error": group.get("meta_refresh_error"),
            "ghl_refresh_error": group.get("ghl_refresh_error"),
            "meta_token_error": group.get("meta_token_error", False),
            "meta_refresh_progress": meta_progress,
        }


# ---------------------------------------------------------------------------
# POST /api/client-groups/refresh-all          — start the cycle
# DELETE /api/client-groups/refresh-all        — stop it
# GET  /api/client-groups/refresh-all/status   — poll progress
# ---------------------------------------------------------------------------

@router.post("/api/client-groups/refresh-all")
async def start_refresh_all(current_user: str = Depends(get_current_user)):
    """Start a staggered refresh cycle: one group every 15 min."""
    from jobs.refresh_all_job import start_cycle
    return start_cycle()


@router.delete("/api/client-groups/refresh-all")
async def stop_refresh_all(current_user: str = Depends(get_current_user)):
    """Cancel a running refresh cycle."""
    from jobs.refresh_all_job import stop_cycle
    return stop_cycle()


@router.get("/api/client-groups/refresh-all/status")
async def refresh_all_status(current_user: str = Depends(get_current_user)):
    """Get progress of the current refresh cycle."""
    from jobs.refresh_all_job import get_cycle_status
    return get_cycle_status()

# ---------------------------------------------------------------------------
# DELETE /api/client-groups/{group_id}
# ---------------------------------------------------------------------------

@router.delete("/api/client-groups/{group_id}")
async def delete_client_group(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    Permanently delete a client group and all associated data:
      1. GHL contacts (ghl_contacts collection)
      2. The client group document itself
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Verify ownership
            group = await client_groups_collection.find_one(
                {"id": group_id, "user_id": current_user},
                {"id": 1, "name": 1},
            )
            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            group_name = group.get("name", group_id)

            # 1. Delete all GHL contacts for this group
            contacts_result = await db["ghl_contacts"].delete_many(
                {"user_id": current_user, "client_group_id": group_id}
            )
            logger.info(
                f"Deleted {contacts_result.deleted_count} GHL contacts "
                f"for group {group_id} ('{group_name}')"
            )

            # 2. Delete the client group itself
            await client_groups_collection.delete_one(
                {"id": group_id, "user_id": current_user}
            )
            logger.info(f"Deleted client group {group_id} ('{group_name}') for user {current_user}")

            return {
                "success": True,
                "message": f"Client group '{group_name}' and all associated data have been deleted",
                "deleted": {
                    "group_id": group_id,
                    "ghl_contacts_removed": contacts_result.deleted_count,
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting client group {group_id}: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to delete client group: {str(e)}",
            )

# ---------------------------------------------------------------------------
# PATCH /api/client-groups/{group_id}/client-status
# ---------------------------------------------------------------------------

@router.patch("/api/client-groups/{group_id}/client-status")
async def update_client_status(
    group_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Update the client_status (Active / Inactive) for a client group"""
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            client_status = body.get("client_status", "").strip()

            if client_status not in ("Active", "Inactive"):
                raise HTTPException(
                    status_code=400,
                    detail="client_status must be 'Active' or 'Inactive'",
                )

            db = mongo_client[DB_NAME]
            result = await db["client_groups"].update_one(
                {"id": group_id, "user_id": current_user},
                {"$set": {"client_status": client_status, "updated_at": datetime.now()}},
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Client group not found")

            return {"message": "Client status updated successfully", "client_status": client_status}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating client status: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to update client status: {str(e)}")