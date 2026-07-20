"""
routers/meta.py
---------------
Meta (Facebook) API endpoints extracted from main.py.
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from core.database import DB_NAME
from core.utils import mongo_to_dict
from dependencies import get_current_user, get_mongo_client
from integrations.facebook_utils.facebook import (
    facebook_integration,
    get_facebook_token,
    save_facebook_token,
)
from integrations.facebook_utils.facebook_optimized import (
    MetaDataFetcher,
    fetch_all_accounts_parallel,
)
from integrations.facebook_utils.facebook_leads import (
    fetch_and_cache_facebook_leads_FIXED,
)
from integrations.gohighlevel import (
    ghl_integration, get_agency_token, get_subaccount_tokens, save_agency_token,
)
from services.meta_service import get_facebook_data, save_facebook_data
from services.meta_status_service import set_object_status, MetaStatusError

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /api/facebook/debug-token — check granted permissions
# ---------------------------------------------------------------------------

@router.get("/api/facebook/debug-token")
async def debug_facebook_token(current_user: str = Depends(get_current_user)):
    """Check what permissions the current Meta token actually has."""
    async with get_mongo_client() as mongo_client:
        token_doc = await get_facebook_token(current_user, mongo_client)
        if not token_doc or not token_doc.get("access_token"):
            raise HTTPException(status_code=400, detail="No Meta token found")

        access_token = token_doc["access_token"]

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Check token permissions
            resp = await client.get(
                "https://graph.facebook.com/v25.0/me/permissions",
                params={"access_token": access_token},
            )
            permissions = resp.json() if resp.status_code == 200 else {"error": resp.text}

            # Check which pages the token can access
            pages_resp = await client.get(
                "https://graph.facebook.com/v25.0/me/accounts",
                params={"access_token": access_token, "fields": "id,name,access_token"},
            )
            pages = pages_resp.json() if pages_resp.status_code == 200 else {"error": pages_resp.text}

        return {
            "permissions": permissions,
            "pages": pages,
            "token_expires_at": token_doc.get("expires_at"),
        }


# ---------------------------------------------------------------------------
# GET /api/connect/facebook
# ---------------------------------------------------------------------------

@router.get("/api/connect/facebook")
async def connect_facebook(current_user: str = Depends(get_current_user)):
    auth_url = facebook_integration.generate_auth_url()
    logger.info(f"Generated auth URL for Meta OAuth flow for user: {current_user}")
    return {"auth_url": auth_url}


# ---------------------------------------------------------------------------
# GET /oauth/callback/facebook
# ---------------------------------------------------------------------------

@router.get("/oauth/callback/facebook")
async def oauth_callback_facebook(
    code: str,
    response: Response,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        logger.info(f"Received Meta callback with code: {code} for user: {current_user}")
        success, result = await facebook_integration.exchange_code_for_token(code)
        if not success:
            logger.error(f"Meta OAuth callback error: {result}")
            redirect_url = (
                f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error"
                f"&error={urllib.parse.quote(result.get('error', 'Meta OAuth callback failed'))}"
            )
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        try:
            await save_facebook_token(current_user, result, mongo_client)
        except Exception as e:
            logger.error(f"Failed to save Meta tokens: {str(e)}")
            redirect_url = (
                f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error"
                f"&error={urllib.parse.quote(str(e))}"
            )
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}

        # Circuit breaker: clear meta_token_error on every client_group this
        # user owns. Set by services/meta_refresh_manager::_mark_all_auth_error
        # when a refresh confirms the token is dead; schedule_stale_groups
        # skips flagged groups, so without this clear the auto cron would
        # never resume even after the user has reconnected.
        try:
            db = mongo_client[DB_NAME]
            await db["client_groups"].update_many(
                {"user_id": current_user, "meta_token_error": True},
                {"$unset": {"meta_token_error": "", "meta_token_error_at": ""}},
            )
        except Exception as e:
            # Don't fail the callback over the cleanup — the user's now
            # reconnected. Worst case: a refresh runs, succeeds, and
            # _finalize_job clears the flag itself.
            logger.warning(f"Failed to clear meta_token_error on reconnect: {e}")
        result_for_cookie = result.copy()
        try:
            cookie_value = json.dumps(result_for_cookie)
            response.set_cookie(
                key="facebook_tokens",
                value=cookie_value,
                httponly=True,
                secure=True,
                samesite="none",
                max_age=result.get("expires_in", 60 * 24 * 60 * 60),
            )
        except Exception as e:
            logger.error(f"Failed to set Meta cookie: {str(e)}")
            redirect_url = (
                f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error"
                f"&error={urllib.parse.quote(str(e))}"
            )
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        redirect_url = (
            f"https://birdy-beta.vercel.app/settings?tab=integrations&status=success"
            f"&tokens={urllib.parse.quote(cookie_value)}"
        )
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@router.get("/api/status")
async def api_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            agency_token = await get_agency_token(current_user, mongo_client)
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
            facebook_token = await get_facebook_token(current_user, mongo_client)

            # ── Auto-refresh expired GHL agency token ──────────────
            if agency_token:
                expires_at = agency_token.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                if is_expired and agency_token.get("refresh_token"):
                    try:
                        success, result = await ghl_integration.refresh_agency_token(
                            agency_token["refresh_token"]
                        )
                        if success:
                            await save_agency_token(current_user, result, mongo_client)
                            agency_token = await get_agency_token(current_user, mongo_client)
                            logger.info(f"Auto-refreshed GHL agency token for {current_user}")

                            # Also regenerate location tokens
                            company_id = result.get("companyId") or agency_token.get("company_id")
                            fresh_access = result.get("access_token")
                            if company_id and fresh_access:
                                for loc_id in (subaccount_tokens or {}):
                                    try:
                                        ok, loc_tokens = await ghl_integration.generate_location_token(
                                            company_id, loc_id, fresh_access
                                        )
                                        if ok:
                                            from integrations.gohighlevel import (
                                                save_subaccount_token,
                                                fetch_location_details,
                                                get_contact_count_from_ghl,
                                            )
                                            details = await fetch_location_details(
                                                loc_id, loc_tokens.get("access_token")
                                            )
                                            count = await get_contact_count_from_ghl(
                                                loc_id, loc_tokens.get("access_token")
                                            )
                                            await save_subaccount_token(
                                                current_user, loc_id, loc_tokens,
                                                mongo_client, details, contact_count=count,
                                            )
                                    except Exception as e:
                                        logger.warning(f"Auto-refresh location {loc_id} failed: {e}")
                                subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
                        else:
                            logger.warning(f"GHL agency token refresh failed for {current_user}")
                    except Exception as e:
                        logger.error(f"GHL auto-refresh error for {current_user}: {e}")

            status = {
                "connected": bool(agency_token or facebook_token),
                "gohighlevel": {
                    "agency": {},
                    "subaccounts": {},
                },
                "facebook": {},
            }
            if agency_token:
                expires_at = agency_token.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["gohighlevel"]["agency"] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired,
                    "company_id": agency_token.get("company_id"),
                }
            for location_id, token_data in subaccount_tokens.items():
                expires_at = token_data.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["gohighlevel"]["subaccounts"][location_id] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired,
                    "name": token_data.get("name", "Unknown Location"),
                }
            if facebook_token:
                expires_at = facebook_token.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["facebook"] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired,
                }
            return status
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting status for user {current_user}: {str(e)}")
            return {
                "connected": False,
                "error": str(e),
                "gohighlevel": {"agency": {}, "subaccounts": {}},
                "facebook": {},
            }


# ---------------------------------------------------------------------------
# GET /api/facebook/adaccounts
# ---------------------------------------------------------------------------

@router.get("/api/facebook/adaccounts")
async def get_facebook_adaccounts(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            token = await get_facebook_token(current_user, mongo_client)
            if not token or not token.get("access_token"):
                raise HTTPException(
                    status_code=400,
                    detail="No Meta token available. Please connect Meta in Settings.",
                )

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(
                    "https://graph.facebook.com/v25.0/me/adaccounts",
                    params={
                        "fields": "name,currency,created_time,owner",
                        "access_token": token["access_token"],
                        "limit": 1000,
                    },
                )

                if response.status_code != 200:
                    error_detail = response.json().get("error", {})
                    logger.error(f"Meta API error: {response.status_code} - {error_detail}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=error_detail.get("message", "Failed to fetch ad accounts"),
                    )

                data = response.json()
                accounts = data.get("data", [])

                logger.info(f"Fetched {len(accounts)} ad accounts for user {current_user}")

                return {
                    "data": data,
                    "meta": {"total": len(accounts)},
                    "message": f"Successfully fetched {len(accounts)} ad accounts",
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching ad accounts: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch ad accounts: {str(e)}",
            )


# ---------------------------------------------------------------------------
# GET /api/facebook-leads/paginated
# ---------------------------------------------------------------------------

@router.get("/api/facebook-leads/paginated")
async def get_facebook_leads_paginated(
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
    groups: str = Query(default=""),
    current_user: str = Depends(get_current_user),
):
    """Get Facebook leads in DESCENDING order (newest first) with pagination."""
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            leads_collection = db["facebook_leads"]

            # Parse group IDs
            group_ids = [g.strip() for g in groups.split(",") if g.strip()] if groups else []

            # Build query
            query = {"user_id": current_user}
            if group_ids:
                query["client_group_id"] = {"$in": group_ids}

            # Get total count
            total_leads = await leads_collection.count_documents(query)

            if total_leads == 0:
                return {
                    "leads": [],
                    "meta": {
                        "total_leads": 0,
                        "current_page": page,
                        "total_pages": 0,
                        "per_page": limit,
                        "has_next": False,
                        "has_prev": False,
                    },
                    "message": "No leads found",
                }

            # Calculate pagination
            skip = (page - 1) * limit
            total_pages = (total_leads + limit - 1) // limit

            # Fetch leads (sorted newest first)
            cursor = leads_collection.find(
                query,
                {
                    "lead_data": 1,
                    "client_group_name": 1,
                    "ad_account_id": 1,
                    "client_group_id": 1,
                },
            ).sort("lead_data.created_time", -1).skip(skip).limit(limit)

            lead_docs = await cursor.to_list(length=limit)

            # Format leads
            leads = []
            for doc in lead_docs:
                lead_data = doc.get("lead_data", {})
                leads.append({
                    "lead_id": lead_data.get("id"),
                    "full_name": lead_data.get("full_name", ""),
                    "email": lead_data.get("email", ""),
                    "phone_number": lead_data.get("phone_number", ""),
                    "ad_name": lead_data.get("ad_name", ""),
                    "platform": lead_data.get("platform", ""),
                    "created_time": lead_data.get("created_time", ""),
                    "group_name": doc.get("client_group_name", "Unknown Group"),
                    "ad_account_id": doc.get("ad_account_id"),
                    "field_data": lead_data.get("field_data", {}),
                })

            elapsed = time.time() - start_time

            logger.info(
                f"Fetched page {page} (newest first): {len(leads)} leads "
                f"in {elapsed:.3f}s"
            )

            return {
                "leads": leads,
                "meta": {
                    "total_leads": total_leads,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(leads),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "newest_first",
                },
                "message": f"Retrieved {len(leads)} leads (newest first)",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                },
            }

        except Exception as e:
            logger.error(f"Error fetching paginated leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


# ---------------------------------------------------------------------------
# GET /disconnect
# ---------------------------------------------------------------------------

@router.get("/disconnect")
async def disconnect(response: Response, current_user: str = Depends(get_current_user)):
    try:
        response.delete_cookie(
            key="gohighlevel_tokens",
            path="/",
            httponly=True,
            secure=True,
            samesite="strict",
        )
        response.delete_cookie(
            key="facebook_tokens",
            path="/",
            httponly=True,
            secure=True,
            samesite="strict",
        )
        logger.info(f"Cleared integration data for user: {current_user}")
        return {"status": "GoHighLevel and Meta integrations disconnected successfully"}
    except Exception as e:
        logger.error(f"Failed to disconnect integration for user {current_user}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect integration: {str(e)}")


# ---------------------------------------------------------------------------
# GET /api/facebook/adaccounts/{account_id}/data/optimized
# ---------------------------------------------------------------------------

@router.get("/api/facebook/adaccounts/{account_id}/data/optimized")
async def get_facebook_account_data_optimized(
    account_id: str,
    current_user: str = Depends(get_current_user),
):
    """OPTIMIZED: Fetch account data with 5-minute cache"""
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            token = await get_facebook_token(current_user, mongo_client)
            if not token or not token.get("access_token"):
                raise HTTPException(status_code=400, detail="No Meta token available")

            fetcher = MetaDataFetcher(current_user, mongo_client, token)
            data = await fetcher.fetch_account_data_parallel(account_id)

            if not data:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to fetch data for account {account_id}",
                )

            elapsed = time.time() - start_time

            return {
                "data": data,
                "meta": {
                    "total_campaigns": len(data.get("data", [])),
                    "response_time_ms": int(elapsed * 1000),
                },
                "message": f"Successfully fetched data for account {account_id}",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# POST /api/facebook/batch-accounts
# ---------------------------------------------------------------------------

@router.post("/api/facebook/batch-accounts")
async def get_facebook_batch_accounts(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """NEW ENDPOINT: Fetch multiple ad accounts in parallel"""
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()
            body = await request.json()
            account_ids = body.get("account_ids", [])

            if not account_ids:
                raise HTTPException(status_code=400, detail="account_ids is required")

            token = await get_facebook_token(current_user, mongo_client)
            if not token:
                raise HTTPException(status_code=400, detail="No Meta token")

            # Fetch all in parallel
            account_data = await fetch_all_accounts_parallel(
                current_user, mongo_client, token, account_ids
            )

            elapsed = time.time() - start_time
            logger.info(
                f"Fetched {len(account_data)} accounts in {elapsed:.2f}s "
                f"(avg {elapsed / len(account_data):.2f}s per account)"
            )

            return {
                "data": account_data,
                "meta": {
                    "total_accounts": len(account_data),
                    "requested": len(account_ids),
                    "response_time_ms": int(elapsed * 1000),
                },
                "message": f"Successfully fetched {len(account_data)} accounts",
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/campaign-insights
# ---------------------------------------------------------------------------

@router.get("/api/campaign-insights")
async def get_campaign_insights(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    groups: Optional[str] = None,
    current_user: str = Depends(get_current_user),
):
    """Get campaign insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            insights_collection = db["facebook_campaign_insights"]

            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(",") if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights),
            }
        except Exception as e:
            logger.error(f"Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/adset-insights
# ---------------------------------------------------------------------------

@router.get("/api/adset-insights")
async def get_adset_insights(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    groups: Optional[str] = None,
    current_user: str = Depends(get_current_user),
):
    """Get adset insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            insights_collection = db["facebook_adset_insights"]

            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(",") if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/ad-insights
# ---------------------------------------------------------------------------

@router.get("/api/ad-insights")
async def get_ad_insights(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    groups: Optional[str] = None,
    current_user: str = Depends(get_current_user),
):
    """Get ad insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            insights_collection = db["facebook_ad_insights"]

            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(",") if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# GET /api/facebook-leads/filtered
# ---------------------------------------------------------------------------

@router.get("/api/facebook-leads/filtered")
async def get_facebook_leads_filtered(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    groups: Optional[str] = None,
    limit: int = Query(default=5000, ge=1, le=10000),
    current_user: str = Depends(get_current_user),
):
    """
    Get Facebook leads filtered by date range and groups.
    Used by the Marketing Hub for date-based queries.
    """
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            db = mongo_client[DB_NAME]
            leads_collection = db["facebook_leads"]

            # Build query
            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(",") if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["lead_data.created_time"] = date_filter

            # Fetch leads (sorted newest first) — include match_keys for GHL enrichment
            cursor = leads_collection.find(
                query,
                {
                    "lead_data": 1,
                    "match_keys": 1,
                    "client_group_name": 1,
                    "ad_account_id": 1,
                    "client_group_id": 1,
                },
            ).sort("lead_data.created_time", -1).limit(limit)

            lead_docs = await cursor.to_list(length=limit)

            # ── GHL enrichment: match Meta leads to GHL contacts ──
            all_keys = set()
            for doc in lead_docs:
                for key in (doc.get("match_keys") or []):
                    all_keys.add(key)

            ghl_matches = {}
            if all_keys:
                ghl_col = db["ghl_contacts"]
                ghl_docs = await ghl_col.find(
                    {"user_id": current_user, "match_keys": {"$in": list(all_keys)}},
                    {"match_keys": 1, "contact_data.tags": 1, "contact_data.opportunities": 1,
                     "contact_data.firstName": 1, "contact_data.lastName": 1,
                     "contact_data.dateAdded": 1, "contact_data.source": 1}
                ).to_list(None)
                for gd in ghl_docs:
                    cd = gd.get("contact_data", {})
                    opps = cd.get("opportunities") or []
                    # Determine primary opportunity status
                    opp_status = ""
                    opp_value = 0
                    if opps:
                        # Pick the most relevant: won > open > lost > abandoned
                        for priority in ["won", "open", "lost", "abandoned"]:
                            match = next((o for o in opps if o.get("status") == priority), None)
                            if match:
                                opp_status = match.get("status", "")
                                opp_value = match.get("monetaryValue", 0) or 0
                                break
                    enrichment = {
                        "ghl_matched": True,
                        "ghl_tags": cd.get("tags") or [],
                        "ghl_opportunity_status": opp_status,
                        "ghl_opportunity_value": opp_value,
                        "ghl_date_added": cd.get("dateAdded", ""),
                    }
                    for key in (gd.get("match_keys") or []):
                        ghl_matches[key] = enrichment

            # Format leads with GHL enrichment
            leads = []
            for doc in lead_docs:
                lead_data = doc.get("lead_data", {})
                doc_keys = doc.get("match_keys") or []

                # Find GHL enrichment
                ghl_enrich = None
                for k in doc_keys:
                    if k in ghl_matches:
                        ghl_enrich = ghl_matches[k]
                        break

                lead_entry = {
                    "lead_id": lead_data.get("id"),
                    "full_name": lead_data.get("full_name", ""),
                    "email": lead_data.get("email", ""),
                    "phone_number": lead_data.get("phone_number", ""),
                    "ad_id": lead_data.get("ad_id", ""),
                    "ad_name": lead_data.get("ad_name", ""),
                    "adset_id": lead_data.get("adset_id", ""),
                    "adset_name": lead_data.get("adset_name", ""),
                    "campaign_id": lead_data.get("campaign_id", ""),
                    "campaign_name": lead_data.get("campaign_name", ""),
                    "platform": lead_data.get("platform", ""),
                    "created_time": lead_data.get("created_time", ""),
                    "group_name": doc.get("client_group_name", "Unknown Group"),
                    "ad_account_id": doc.get("ad_account_id"),
                    "field_data": lead_data.get("field_data", {}),
                    # GHL enrichment
                    "ghl_matched": ghl_enrich is not None,
                    "ghl_tags": ghl_enrich["ghl_tags"] if ghl_enrich else [],
                    "ghl_opportunity_status": ghl_enrich["ghl_opportunity_status"] if ghl_enrich else "",
                    "ghl_opportunity_value": ghl_enrich["ghl_opportunity_value"] if ghl_enrich else 0,
                    "ghl_date_added": ghl_enrich["ghl_date_added"] if ghl_enrich else "",
                }
                leads.append(lead_entry)

            elapsed = time.time() - start_time

            logger.info(
                f"Fetched {len(leads)} filtered leads in {elapsed:.3f}s "
                f"(date range: {start_date} to {end_date})"
            )

            return {
                "leads": leads,
                "meta": {
                    "total": len(leads),
                    "returned": len(leads),
                    "limit": limit,
                    "start_date": start_date,
                    "end_date": end_date,
                },
                "message": f"Retrieved {len(leads)} leads",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                },
            }

        except Exception as e:
            logger.error(f"Error fetching filtered leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


# ---------------------------------------------------------------------------
# POST /api/facebook/update-status
# ---------------------------------------------------------------------------

from pydantic import BaseModel

class StatusUpdateRequest(BaseModel):
    object_id: str
    object_type: str  # "campaign", "adset", "ad"
    status: str       # "ACTIVE", "PAUSED"

@router.post("/api/facebook/update-status")
async def update_facebook_object_status(
    body: StatusUpdateRequest,
    current_user: str = Depends(get_current_user),
):
    """Toggle a campaign, ad set, or ad between ACTIVE and PAUSED via Meta Graph API."""
    async with get_mongo_client() as mongo_client:
        try:
            return await set_object_status(
                current_user, body.object_id, body.object_type, body.status, mongo_client
            )
        except MetaStatusError as e:
            raise HTTPException(status_code=e.status_code, detail=e.message)
