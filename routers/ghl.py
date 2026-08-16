"""
routers/ghl.py
--------------
GoHighLevel integration endpoints extracted from main.py.
"""

import json
import logging
import urllib.parse
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user
from integrations.gohighlevel import (
    ghl_integration,
    get_agency_token,
    get_subaccount_tokens,
    save_agency_token,
    save_subaccount_token,
    fetch_location_details,
    get_contact_count_from_ghl,
    fetch_ghl_contacts_on_demand,
)
from services.ghl_service import fetch_and_cache_ghl_data_optimized

logger = logging.getLogger(__name__)

router = APIRouter()


# ------------------------------------------------------------------
# GET /api/connect
# ------------------------------------------------------------------
@router.get("/api/connect")
async def connect(current_user: str = Depends(get_current_user)):
    auth_url = ghl_integration.generate_auth_url()
    logger.info(f"Generated auth URL for GoHighLevel agency flow for user: {current_user}")
    return {"auth_url": auth_url}


# ------------------------------------------------------------------
# GET /oauth/callback
# ------------------------------------------------------------------
@router.get("/oauth/callback")
async def oauth_callback(code: str, response: Response, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        logger.info(f"Received GoHighLevel callback with code: {code} for user: {current_user}")
        success, result = await ghl_integration.exchange_code_for_token(code)
        if not success:
            logger.error(f"GoHighLevel OAuth callback error: {result}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(result.get('error', 'OAuth callback failed'))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        try:
            await save_agency_token(current_user, result, mongo_client)
        except Exception as e:
            logger.error(f"Failed to save GoHighLevel agency tokens: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        result_for_cookie = result.copy()
        try:
            cookie_value = json.dumps(result_for_cookie)
            response.set_cookie(
                key="gohighlevel_tokens",
                value=cookie_value,
                httponly=True,
                secure=True,
                samesite="none",
                max_age=result.get("expires_in", 3600)
            )
        except Exception as e:
            logger.error(f"Failed to set GoHighLevel cookie: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=success&tokens={urllib.parse.quote(cookie_value)}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}


# ------------------------------------------------------------------
# GET /api/subaccount/locations
# ------------------------------------------------------------------
@router.get("/api/subaccount/locations")
async def get_subaccount_locations(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            agency_token = await get_agency_token(current_user, mongo_client)
            if not agency_token:
                raise HTTPException(status_code=400, detail="No agency token available. Please connect GoHighLevel in Settings.")
            company_id = agency_token.get("company_id")
            access_token = agency_token.get("access_token")
            if not company_id or not access_token:
                raise HTTPException(status_code=400, detail="Invalid agency token")
            success, locations = await ghl_integration.fetch_locations(company_id, access_token)
            if not success:
                status_code = locations.get("status_code", 400)
                if status_code == 404:
                    raise HTTPException(status_code=404, detail="No locations available or incorrect GoHighLevel API configuration.")
                raise HTTPException(status_code=status_code, detail=f"Failed to fetch locations: {locations.get('error', 'Unknown error')}")
            return {
                "locations": locations,
                "message": f"Successfully fetched {len(locations)} locations"
            }
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error fetching locations for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Invalid response from GoHighLevel API: {str(e)}")
        except Exception as e:
            logger.error(f"Error fetching locations for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch locations: {str(e)}")


# ------------------------------------------------------------------
# POST /api/add-subaccount
# ------------------------------------------------------------------
@router.post("/api/add-subaccount")
async def add_subaccount(request: Request, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            location_id = body.get("location_id")
            if not location_id:
                raise HTTPException(status_code=400, detail="location_id is required")

            agency_token = await get_agency_token(current_user, mongo_client)
            if not agency_token:
                raise HTTPException(status_code=400,
                                    detail="No agency token available. Please connect GoHighLevel in Settings.")

            company_id = agency_token.get("company_id")
            access_token = agency_token.get("access_token")
            if not company_id or not access_token:
                raise HTTPException(status_code=400, detail="Invalid agency token")

            success, loc_tokens = await ghl_integration.generate_location_token(company_id, location_id, access_token)
            if not success:
                raise HTTPException(status_code=400,
                                    detail=f"Failed to generate location token: {loc_tokens.get('error', 'Unknown error')}")

            location_details = await fetch_location_details(location_id, loc_tokens.get("access_token"))

            # Only fetch contact COUNT, not full contacts
            contact_count = await get_contact_count_from_ghl(location_id, loc_tokens.get("access_token"))

            # Save only count, not contacts array
            await save_subaccount_token(
                current_user,
                location_id,
                loc_tokens,
                mongo_client,
                location_details,
                contact_count=contact_count
            )

            return {
                "success": True,
                "location_id": location_id,
                "location_details": location_details,
                "contact_count": contact_count  # Return count, not contacts
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding subaccount for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to add subaccount: {str(e)}")


# ------------------------------------------------------------------
# GET /api/location-data
# ------------------------------------------------------------------
@router.get("/api/location-data")
async def get_all_location_data(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
            if not subaccount_tokens:
                return {"locations": [], "message": "No subaccount tokens found"}
            locations_data = []
            for location_id, token_data in subaccount_tokens.items():
                combined_data = {
                    "id": location_id,
                    "location_id": location_id,
                    "token_expires_at": token_data.get("expires_at").isoformat() if token_data.get("expires_at") else None,
                    "token_expired": token_data.get("expires_at") and datetime.now() >= token_data.get("expires_at"),
                    "name": token_data.get("name", "Unknown Location"),
                    "address": token_data.get("address"),
                    "isInstalled": token_data.get("isInstalled", False),
                    "trial": token_data.get("trial", {})
                }
                locations_data.append(combined_data)
            return {
                "locations": locations_data,
                "total_count": len(locations_data),
                "message": f"Successfully fetched {len(locations_data)} locations"
            }
        except Exception as e:
            logger.error(f"Error in get_all_location_data for user {current_user}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch location data: {str(e)}")


# ------------------------------------------------------------------
# POST /api/contacts/ghl/full-refresh
# ------------------------------------------------------------------
@router.post("/api/contacts/ghl/full-refresh")
async def force_full_ghl_refresh(
        group_id: str = Query(...),
        current_user: str = Depends(get_current_user)
):
    """
    Force a FULL refresh of GHL contacts for a specific client group.

    This will:
    - Delete all existing cached contacts
    - Fetch ALL contacts from GHL (not incremental)
    - Rebuild the entire contact database

    Use this when:
    - You suspect data inconsistencies
    - Initial cache was incomplete
    - Major changes were made in GHL

    Warning: This can take several minutes for large contact lists
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Verify group belongs to user
            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            if not group.get("ghl_location_id"):
                raise HTTPException(
                    status_code=400,
                    detail="Client group does not have a GHL location"
                )

            logger.info(f"Starting FULL refresh for group {group_id}")

            # Force FULL refresh (initial load mode)
            await fetch_and_cache_ghl_data_optimized(
                group_id,
                group["ghl_location_id"],
                current_user,
                mongo_client,
                is_initial_load=True  # This forces full reload
            )

            # Get updated count
            contacts_collection = db["ghl_contacts"]
            total_contacts = await contacts_collection.count_documents({
                "user_id": current_user,
                "location_id": group["ghl_location_id"]
            })

            logger.info(f"FULL refresh complete for group {group_id}: {total_contacts} contacts")

            return {
                "success": True,
                "message": f"Full refresh completed for '{group['name']}'",
                "group_id": group_id,
                "total_contacts": total_contacts,
                "refresh_mode": "full",
                "timestamp": datetime.utcnow().isoformat()
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in full refresh: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to refresh contacts: {str(e)}"
            )


# ------------------------------------------------------------------
# GET /api/contacts/ghl/refresh-status
# ------------------------------------------------------------------
@router.get("/api/contacts/ghl/refresh-status")
async def get_ghl_refresh_status(current_user: str = Depends(get_current_user)):
    """
    Get refresh status for all GHL client groups.

    Shows:
    - Last refresh time
    - Time since last refresh
    - Total contacts cached
    - Refresh mode used (full/incremental)
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]
            contacts_collection = db["ghl_contacts"]

            groups = await client_groups_collection.find({
                "user_id": current_user,
                "ghl_location_id": {"$exists": True, "$ne": None}
            }).to_list(None)

            status_list = []
            now = datetime.utcnow()

            # One grouped count for every location instead of a count_documents()
            # per group inside the loop. The old shape issued N round-trips and,
            # before idx_ghl_user_location existed, each one re-scanned that
            # location's entire contact set.
            location_ids = [
                g["ghl_location_id"] for g in groups if g.get("ghl_location_id")
            ]
            counts_by_location = {}
            if location_ids:
                counts_by_location = {
                    row["_id"]: row["n"]
                    for row in await contacts_collection.aggregate([
                        {"$match": {
                            "user_id": current_user,
                            "location_id": {"$in": location_ids},
                        }},
                        {"$group": {"_id": "$location_id", "n": {"$sum": 1}}},
                    ]).to_list(None)
                }

            for group in groups:
                last_refresh = group.get("last_ghl_refresh")

                contact_count = counts_by_location.get(group["ghl_location_id"], 0)

                time_since_refresh = None
                if last_refresh:
                    time_since_refresh = (now - last_refresh).total_seconds() / 3600  # hours

                status_list.append({
                    "group_id": group["id"],
                    "group_name": group["name"],
                    "location_id": group["ghl_location_id"],
                    "total_contacts": contact_count,
                    "last_refresh": last_refresh.isoformat() if last_refresh else None,
                    "hours_since_refresh": round(time_since_refresh, 1) if time_since_refresh else None,
                    "next_incremental_refresh": "Automatic (hourly)" if last_refresh else "Not yet refreshed"
                })

            return {
                "groups": status_list,
                "total_groups": len(status_list),
                "message": "Refresh status retrieved successfully"
            }

        except Exception as e:
            logger.error(f"Error getting refresh status: {e}")
            raise HTTPException(status_code=500, detail=str(e))
