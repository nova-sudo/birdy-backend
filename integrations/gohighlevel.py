import httpx
import json
import urllib.parse
from datetime import datetime, timedelta
import logging
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
import asyncio
from typing import Optional, Dict, Tuple, List

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# GHL OAuth configuration
OAUTH_CONFIG = {
    "auth_url": "https://marketplace.gohighlevel.com/oauth/chooselocation",
    "token_url": "https://services.leadconnectorhq.com/oauth/token",
    "locations_url": "https://services.leadconnectorhq.com/locations",
    "location_token_url": "https://services.leadconnectorhq.com/oauth/locationToken",
    "contacts_url": "https://services.leadconnectorhq.com/contacts/",
    "scopes": [
        "oauth.readonly",
        "oauth.write",
        "contacts.readonly",
        "contacts.write",
        "locations.readonly",
        "locations.write",
        "opportunities.readonly",
        "opportunities.write"
    ],
    "max_concurrent_requests": 5,
    "request_timeout": 30.0,
    "retry_attempts": 3,
    "retry_delay": 2
}

Agency_CLIENT_ID = os.getenv("CLIENT_ID")
Agency_CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")
APP_ID = "68bfa19909174c84efb94531"

# In-memory cache for pending requests
_pending_requests: Dict[str, asyncio.Future] = {}
_cache_lock = asyncio.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers for opportunity statistics.
# The GHL `date` search param only filters by creation date, which is useless
# for windowed dashboard stats (long sales cycles → zero "won this week").
# Instead we paginate every opp once and derive windowed stats from
# lastStatusChangeAt / createdAt.
# ─────────────────────────────────────────────────────────────────────────────

def _opp_monetary_value(opp: dict) -> float:
    raw = opp.get("monetaryValue") or opp.get("value") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _in_window(iso_ts: str, start_iso: Optional[str], end_iso: Optional[str]) -> bool:
    """Return True if iso_ts (yyyy-mm-dd[T...]) is between start and end (inclusive, yyyy-mm-dd)."""
    if not iso_ts:
        return False
    day = iso_ts[:10]
    if start_iso and day < start_iso:
        return False
    if end_iso and day > end_iso:
        return False
    return True


def compute_opp_stats(
    opps: List[Dict],
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
) -> Dict:
    """
    Derive opp statistics from an opportunity list.

    Lifetime mode (both dates None):
      Counts every opp by its current status. This is the pipeline snapshot.

    Windowed mode (dates given, yyyy-mm-dd):
      won/lost/abandoned → opps whose lastStatusChangeAt falls in the window
      open               → opps created in the window that are still open
      total_opportunities = won + lost + abandoned + open
      won_revenue = sum(monetaryValue) for opps counted as 'won' in the window
    """
    stats = {
        "won": 0, "lost": 0, "open": 0, "abandoned": 0,
        "total_opportunities": 0, "won_revenue": 0.0, "total_revenue": 0.0,
    }
    lifetime = window_start is None and window_end is None

    for opp in opps:
        status = (opp.get("status") or "open").lower()
        if status not in ("won", "lost", "open", "abandoned"):
            continue
        val = _opp_monetary_value(opp)

        if lifetime:
            stats[status] += 1
            stats["total_opportunities"] += 1
            if status == "won":
                stats["won_revenue"] += val
        else:
            last_change = opp.get("lastStatusChangeAt") or ""
            created = opp.get("createdAt") or ""
            if status in ("won", "lost", "abandoned"):
                if _in_window(last_change, window_start, window_end):
                    stats[status] += 1
                    stats["total_opportunities"] += 1
                    if status == "won":
                        stats["won_revenue"] += val
            else:  # open
                if _in_window(created, window_start, window_end):
                    stats["open"] += 1
                    stats["total_opportunities"] += 1

    stats["won_revenue"] = round(stats["won_revenue"], 2)
    stats["total_revenue"] = stats["won_revenue"]
    return stats


class GHLIntegration:
    def __init__(self, client_id, client_secret, redirect_uri):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._semaphore = asyncio.Semaphore(OAUTH_CONFIG["max_concurrent_requests"])

    def generate_auth_url(self):
        """Generate auth URL for agency flow"""
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(OAUTH_CONFIG["scopes"]),
            "user_type": "Company"
        }
        return f"{OAUTH_CONFIG['auth_url']}?{urllib.parse.urlencode(params)}"

    async def _make_token_request(self, data: dict, operation: str = "token_operation"):
        """Centralized token request with retry logic and deduplication"""
        cache_key = json.dumps(data, sort_keys=True)

        async with _cache_lock:
            if cache_key in _pending_requests:
                logger.info(f"Returning cached pending request for {operation}")
                return await _pending_requests[cache_key]

            future = asyncio.Future()
            _pending_requests[cache_key] = future

        try:
            async with self._semaphore:
                async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
                    for attempt in range(OAUTH_CONFIG["retry_attempts"]):
                        try:
                            response = await client.post(
                                OAUTH_CONFIG["token_url"],
                                data=data,
                                headers={
                                    "Accept": "application/json",
                                    "Content-Type": "application/x-www-form-urlencoded"
                                }
                            )

                            if response.status_code != 200:
                                error_msg = response.json().get("error", f"{operation} failed")
                                logger.error(f"{operation} failed: {response.status_code} {response.text}")

                                if attempt < OAUTH_CONFIG["retry_attempts"] - 1 and response.status_code >= 500:
                                    await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                                    continue

                                result = (False, {
                                    "error": error_msg,
                                    "status_code": response.status_code
                                })
                                future.set_result(result)
                                return result

                            tokens = response.json()
                            tokens["expires_at"] = (
                                    datetime.now() + timedelta(seconds=tokens.get("expires_in", 3600))
                            ).isoformat()

                            logger.info(f"{operation} successful")
                            result = (True, tokens)
                            future.set_result(result)
                            return result

                        except httpx.RequestError as e:
                            logger.error(f"Network error during {operation} (attempt {attempt + 1}): {str(e)}")
                            if attempt < OAUTH_CONFIG["retry_attempts"] - 1:
                                await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                                continue

                            result = (False, {"error": f"Network error: {str(e)}"})
                            future.set_exception(e)
                            return result
        finally:
            async with _cache_lock:
                _pending_requests.pop(cache_key, None)

    async def refresh_agency_token(self, refresh_token):
        """Refresh the access token for the agency"""
        return await self._make_token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "user_type": "Company"
        }, "refresh_agency_token")

    async def exchange_code_for_token(self, code):
        """Exchange authorization code for access token"""
        return await self._make_token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "user_type": "Company",
            "redirect_uri": self.redirect_uri
        }, "exchange_code")

    async def refresh_location_token(self, location_id, refresh_token):
        """Refresh the access token for a specific location"""
        success, result = await self._make_token_request({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "user_type": "Company"
        }, f"refresh_location_token_{location_id}")

        if success:
            logger.info(f"Refreshed token for location_id: {location_id}")

        return success, result

    async def fetch_locations(self, company_id, access_token, retries=3, delay=2):
        """
        Fetch ALL sub-accounts for a company.

        Strategy: try multiple GHL endpoints in order and keep whichever
        gives us the most unique locations. Each endpoint has its own
        quirks/caps so we don't rely on a single source of truth.

        Endpoints tried (results merged & deduped by locationId):
          1. /locations/search?companyId=... + skip pagination
             (limit up to 1000; the proper agency-listing endpoint).
          2. /oauth/installedLocations?isInstalled=true/false
             (covers app-install marketplace listings; supports skip).
          3. /locations?companyId=... (legacy, hard cap at 100 — fallback only).
        """
        seen_ids: set = set()
        merged: list = []
        any_ok = False

        def _loc_id(loc):
            return loc.get("_id") or loc.get("id") or loc.get("locationId")

        def _extend(chunk):
            added = 0
            for loc in chunk:
                lid = _loc_id(loc)
                if not lid or lid in seen_ids:
                    continue
                seen_ids.add(lid)
                merged.append(loc)
                added += 1
            return added

        async def _paginate(client, build_url, label, page_size, max_pages=50):
            """Generic skip-pagination helper. Returns True if endpoint ok."""
            nonlocal any_ok
            skip = 0
            ok = False
            for page in range(max_pages):
                url = build_url(skip, page_size)
                logger.info(f"{label} page={page + 1} skip={skip} limit={page_size}")

                chunk = None
                for attempt in range(retries):
                    try:
                        response = await client.get(
                            url,
                            headers={
                                "Accept": "application/json",
                                "Version": "2021-07-28",
                                "Authorization": f"Bearer {access_token}",
                            },
                        )
                        if response.status_code == 200:
                            chunk = response.json().get("locations", []) or []
                            ok = True
                            any_ok = True
                            break
                        if response.status_code >= 500 and attempt < retries - 1:
                            await asyncio.sleep(delay)
                            continue
                        logger.warning(
                            f"{label} returned {response.status_code}: "
                            f"{response.text[:200]}"
                        )
                        break
                    except httpx.RequestError as e:
                        logger.error(f"{label} network error (attempt {attempt + 1}): {e}")
                        if attempt < retries - 1:
                            await asyncio.sleep(delay)
                            continue
                        break

                if chunk is None or not chunk:
                    break
                added = _extend(chunk)
                if added == 0:
                    logger.warning(f"{label} page {page + 1}: only duplicates — stopping")
                    break
                if len(chunk) < page_size:
                    break
                skip += page_size
            return ok

        async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
            # ── 1. /locations/search (proper agency listing, supports skip+limit) ──
            def _search_url(skip, lim):
                return (
                    f"https://services.leadconnectorhq.com/locations/search"
                    f"?companyId={company_id}&limit={lim}&skip={skip}"
                )
            await _paginate(client, _search_url, "locations/search", page_size=500)

            # ── 2. /oauth/installedLocations for both install states ──
            for installed_flag in ("true", "false"):
                def _inst_url(skip, lim, flag=installed_flag):
                    return (
                        f"https://services.leadconnectorhq.com/oauth/installedLocations"
                        f"?companyId={company_id}&appId={APP_ID}"
                        f"&isInstalled={flag}&limit={lim}&skip={skip}"
                    )
                await _paginate(
                    client, _inst_url,
                    f"installedLocations[isInstalled={installed_flag}]",
                    page_size=500,
                )

            # ── 3. legacy /locations as last resort (capped at 100) ──
            if not any_ok:
                logger.warning("All paginated endpoints failed — trying legacy /locations")
                try:
                    response = await client.get(
                        f"{OAUTH_CONFIG['locations_url']}"
                        f"?companyId={company_id}&limit=100",
                        headers={
                            "Accept": "application/json",
                            "Version": "2021-07-28",
                            "Authorization": f"Bearer {access_token}",
                        },
                    )
                    if response.status_code == 200:
                        _extend(response.json().get("locations", []) or [])
                    else:
                        return False, {
                            "error": response.text[:200] or "Failed to fetch locations",
                            "status_code": response.status_code,
                        }
                except httpx.RequestError as e:
                    return False, {"error": f"Network error: {e}", "status_code": 500}

            normalized = self._normalize_locations(merged)
            logger.info(f"✅ Fetched {len(normalized)} unique locations total")
            return True, normalized

    async def _fetch_locations_alternative(self, company_id, access_token, client):
        """Try alternative installedLocations endpoint"""
        try:
            alt_url = (
                f"https://services.leadconnectorhq.com/oauth/installedLocations"
                f"?companyId={company_id}&limit=1000&appId={APP_ID}&isInstalled=False"
            )
            logger.info(f"Fetching from alternative endpoint")

            response = await client.get(
                alt_url,
                headers={
                    "Accept": "application/json",
                    "Version": "2021-07-28",
                    "Authorization": f"Bearer {access_token}"
                }
            )

            if response.status_code == 200:
                data = response.json()
                locations = data.get("locations", [])
                normalized_locations = self._normalize_locations(locations)
                logger.info(f"✅ Fetched {len(normalized_locations)} locations from alternative endpoint")
                return True, normalized_locations

            return False, {"error": f"Alternative endpoint returned {response.status_code}"}

        except Exception as e:
            logger.error(f"Error with alternative endpoint: {str(e)}")
            return False, {"error": str(e)}

    def _normalize_locations(self, locations):
        """Normalize location data structure"""
        return [
            {
                "locationId": loc.get("_id") or loc.get("id"),
                "name": loc.get("name", "Unknown Location"),
                "address": loc.get("address"),
                "isInstalled": loc.get("isInstalled", False),
                "trial": loc.get("trial", {})
            }
            for loc in locations
        ]

    async def generate_location_token(self, company_id, location_id, access_token):
        """Generate location token with retry logic"""
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
                for attempt in range(OAUTH_CONFIG["retry_attempts"]):
                    try:
                        logger.info(f"Generating token for location_id: {location_id} (attempt {attempt + 1})")

                        response = await client.post(
                            OAUTH_CONFIG["location_token_url"],
                            data={
                                "companyId": company_id,
                                "locationId": location_id
                            },
                            headers={
                                "Accept": "application/json",
                                "Content-Type": "application/x-www-form-urlencoded",
                                "Version": "2021-07-28",
                                "Authorization": f"Bearer {access_token}"
                            }
                        )

                        if response.status_code == 201:
                            tokens = response.json()
                            tokens["expires_at"] = (
                                    datetime.now() + timedelta(seconds=tokens.get("expires_in", 3600))
                            ).isoformat()
                            logger.info(f"✅ Generated token for location_id: {location_id}")
                            return True, tokens

                        error_msg = response.json().get("error", "Failed to generate location token")
                        logger.error(f"Failed to generate token: {response.status_code}")

                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1 and response.status_code >= 500:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue

                        return False, {"error": error_msg, "status_code": response.status_code}

                    except httpx.RequestError as e:
                        logger.error(f"Network error (attempt {attempt + 1}): {str(e)}")
                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue
                        return False, {"error": f"Network error: {str(e)}"}

    async def fetch_location_contacts(self, location_id, access_token, limit=100):
        """Fetch contacts with full contact data"""
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
                for attempt in range(OAUTH_CONFIG["retry_attempts"]):
                    try:
                        url = f"{OAUTH_CONFIG['contacts_url']}?locationId={location_id}&limit={limit}"

                        response = await client.get(
                            url,
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {access_token}",
                                "Version": "2021-07-28"
                            }
                        )

                        if response.status_code == 200:
                            data = response.json()
                            contacts = data.get("contacts", [])
                            logger.info(f"✅ Fetched {len(contacts)} contacts for location {location_id}")
                            return True, contacts

                        error_msg = response.json().get("error", "Failed to fetch contacts")
                        logger.error(f"Failed to fetch contacts: {response.status_code}")

                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1 and response.status_code >= 500:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue

                        return False, {"error": error_msg, "status_code": response.status_code}

                    except httpx.RequestError as e:
                        logger.error(f"Network error (attempt {attempt + 1}): {str(e)}")
                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue
                        return False, {"error": f"Network error: {str(e)}"}

    async def fetch_location_contacts_client_groups(self, location_id, access_token, limit=100):
        """OPTIMIZED: Fetch only contact count from meta.total for client groups cache"""
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
                for attempt in range(OAUTH_CONFIG["retry_attempts"]):
                    try:
                        url = f"{OAUTH_CONFIG['contacts_url']}?locationId={location_id}&limit={limit}"

                        response = await client.get(
                            url,
                            headers={
                                "Accept": "application/json",
                                "Authorization": f"Bearer {access_token}",
                                "Version": "2021-07-28"
                            }
                        )

                        if response.status_code == 200:
                            data = response.json()

                            # Safely access nested dictionary for total count
                            meta_data = data.get("meta", {})
                            total_count = meta_data.get("total", 0)

                            logger.info(f"✅ Fetched total count: {total_count} for location {location_id}")

                            # Return the total_count instead of the contacts list
                            return True, total_count

                        error_msg = response.json().get("error", "Failed to fetch contacts")
                        logger.error(f"Failed to fetch contacts: {response.status_code}")

                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1 and response.status_code >= 500:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue

                        return False, {"error": error_msg, "status_code": response.status_code}

                    except httpx.RequestError as e:
                        logger.error(f"Network error (attempt {attempt + 1}): {str(e)}")
                        if attempt < OAUTH_CONFIG["retry_attempts"] - 1:
                            await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                            continue
                        return False, {"error": f"Network error: {str(e)}"}

    async def fetch_all_location_contacts(self, location_id, access_token):
        """Fetch ALL contacts with automatic pagination"""
        all_contacts = []
        start_after = None

        while True:
            try:
                url = f"{OAUTH_CONFIG['contacts_url']}?locationId={location_id}&limit=100"
                if start_after:
                    url += f"&startAfterId={start_after}"

                async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
                    response = await client.get(
                        url,
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {access_token}",
                            "Version": "2021-07-28"
                        }
                    )

                    if response.status_code != 200:
                        logger.error(f"Failed to fetch contacts page: {response.status_code}")
                        break

                    data = response.json()
                    contacts = data.get("contacts", [])

                    if not contacts:
                        break

                    all_contacts.extend(contacts)
                    logger.info(f"Fetched batch of {len(contacts)} contacts (total: {len(all_contacts)})")

                    meta = data.get("meta", {})
                    start_after = meta.get("startAfterId")

                    if not start_after or len(contacts) < 100:
                        break

            except Exception as e:
                logger.error(f"Error fetching contacts page: {str(e)}")
                break

        logger.info(f"✅ Fetched total of {len(all_contacts)} contacts for location {location_id}")
        return True, all_contacts

    async def fetch_contacts_search(
            self,
            location_id: str,
            access_token: str,
            page: int = 1,
            limit: int = 500
    ) -> Tuple[bool, Dict]:
        """
        NEW METHOD: Fetch contacts using the /contacts/search endpoint

        ACTUAL API RESPONSE FORMAT:
        {
            "contacts": [...],
            "total": 2152,
            "traceId": "91ff098e-6df4-4933-aadb-942cbc1cb63d"
        }

        Args:
            location_id: GHL location ID
            access_token: OAuth access token
            page: Page number (starts at 1)
            limit: Results per page (max 500)

        Returns:
            Tuple of (success: bool, data: dict)
            On success: {
                "contacts": [...],
                "total": 2152,
                "traceId": "...",
                "meta": {
                    "total": 2152,
                    "currentPage": 1,
                    "totalPages": 5,
                    "hasNext": true,
                    "hasPrev": false,
                    "pageLimit": 500
                }
            }
            On failure: {"error": str, "status_code": int}
        """
        try:
            url = "https://services.leadconnectorhq.com/contacts/search"

            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Version": "2021-07-28",
                "Accept": "application/json"
            }

            payload = {
                "locationId": location_id,
                "pageLimit": limit,
                "page": page
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code == 200:
                    data = response.json()

                    # Extract from ACTUAL response format
                    contacts = data.get("contacts", [])
                    total = data.get("total", 0)
                    trace_id = data.get("traceId", "")

                    # Calculate pagination metadata ourselves
                    total_pages = (total + limit - 1) // limit if total > 0 else 0
                    has_next = page < total_pages
                    has_prev = page > 1

                    logger.info(
                        f"✅ Fetched page {page}: {len(contacts)} contacts "
                        f"(total: {total}, pages: {total_pages})"
                    )

                    return True, {
                        "contacts": contacts,
                        "total": total,
                        "traceId": trace_id,
                        # Add calculated metadata for convenience
                        "meta": {
                            "total": total,
                            "currentPage": page,
                            "totalPages": total_pages,
                            "hasNext": has_next,
                            "hasPrev": has_prev,
                            "pageLimit": limit
                        }
                    }
                else:
                    error_detail = response.text
                    logger.error(
                        f"❌ GHL Search API error: {response.status_code} - {error_detail}"
                    )
                    return False, {
                        "error": f"API error: {response.status_code}",
                        "status_code": response.status_code,
                        "detail": error_detail
                    }

        except httpx.TimeoutException:
            logger.error(f"⏱️ Timeout fetching contacts for location {location_id}")
            return False, {"error": "Request timeout", "status_code": 408}
        except Exception as e:
            logger.error(f"❌ Error fetching contacts: {str(e)}", exc_info=True)
            return False, {"error": str(e), "status_code": 500}

    async def fetch_all_opportunities(
            self,
            location_id: str,
            access_token: str,
    ) -> Tuple[bool, List[Dict]]:
        """
        Paginate through every opportunity for a location (no date filter).

        Returns (success, opps). On any pagination error, returns (False, partial_list)
        so callers can decide whether to trust partial data. The downstream
        `compute_opp_stats_all_presets` only writes if success is True.
        """
        url = "https://services.leadconnectorhq.com/opportunities/search"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": "2021-07-28",
            "Accept": "application/json",
        }

        opps: list = []
        page = 1
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                while page <= 500:  # generous safety cap
                    params = {
                        "location_id": location_id,
                        "status": "all",
                        "limit": 100,
                        "page": page,
                    }
                    try:
                        resp = await client.get(url, headers=headers, params=params)
                    except Exception as e:
                        logger.warning("GHL opp fetch network error page %s: %s", page, e)
                        return False, opps

                    if resp.status_code != 200:
                        logger.warning(
                            "GHL opp fetch returned %s on page %s: %s",
                            resp.status_code, page, resp.text[:200],
                        )
                        return False, opps

                    data = resp.json()
                    chunk = data.get("opportunities", []) or []
                    if not chunk:
                        break
                    opps.extend(chunk)

                    meta = data.get("meta", {}) or {}
                    total = meta.get("total", 0)
                    if not meta.get("nextPage") and page * 100 >= total:
                        break
                    page += 1

            logger.info("Fetched %d opportunities for location %s (%d pages)", len(opps), location_id, page)
            return True, opps

        except Exception as e:
            logger.error("Failed to fetch opportunities for location %s: %s", location_id, e, exc_info=True)
            return False, opps

    async def fetch_opportunity_stats(
            self,
            location_id: str,
            access_token: str,
            date_start: str = None,
            date_end: str = None,
    ) -> Tuple[bool, Dict]:
        """
        Single-preset convenience wrapper. Paginates all opps once then filters.

        For multi-preset refresh jobs, prefer `fetch_all_opportunities` + in-memory
        computation (see services.metric_orchestrator or services.ghl_service).

        date_start/date_end are ISO yyyy-mm-dd; both None → lifetime stats.
        """
        ok, opps = await self.fetch_all_opportunities(location_id, access_token)
        if not ok:
            empty = {"won": 0, "lost": 0, "open": 0, "abandoned": 0,
                     "total_opportunities": 0, "won_revenue": 0.0, "total_revenue": 0.0}
            return False, empty

        stats = compute_opp_stats(opps, date_start, date_end)
        date_label = f" [{date_start} to {date_end}]" if date_start else ""
        logger.info(
            "📊 GHL opp stats for location %s%s: won=%d lost=%d open=%d abandoned=%d revenue=%.2f",
            location_id, date_label, stats["won"], stats["lost"], stats["open"],
            stats["abandoned"], stats["won_revenue"],
        )
        return True, stats

    async def fetch_all_contacts_search(
            self,
            location_id: str,
            access_token: str,
            max_pages: Optional[int] = None
    ) -> Tuple[bool, List[Dict]]:
        """
        Fetch ALL contacts using pagination with the new search endpoint

        Args:
            location_id: GHL location ID
            access_token: OAuth access token
            max_pages: Maximum number of pages to fetch (None = fetch all)

        Returns:
            Tuple of (success: bool, contacts: list)
        """
        all_contacts = []
        page = 1
        total_pages = None
        total_contacts = None

        try:
            while True:
                # Check max_pages limit
                if max_pages and page > max_pages:
                    logger.info(f"⚠️ Reached max_pages limit ({max_pages})")
                    break

                success, result = await self.fetch_contacts_search(
                    location_id,
                    access_token,
                    page=page,
                    limit=500
                )

                if not success:
                    logger.error(f"Failed to fetch page {page}: {result.get('error')}")
                    # Return what we have so far
                    return len(all_contacts) > 0, all_contacts

                contacts = result.get("contacts", [])
                meta = result.get("meta", {})

                # Add to results
                all_contacts.extend(contacts)

                # Get total from first page response
                if total_contacts is None:
                    total_contacts = result.get("total", 0)
                    total_pages = meta.get("totalPages", 0)
                    logger.info(
                        f"📊 Total contacts: {total_contacts}, "
                        f"Total pages to fetch: {total_pages}"
                    )

                # Check if we're done
                if len(contacts) == 0 or not meta.get("hasNext", False):
                    logger.info(f"✅ Completed fetching all {len(all_contacts)} contacts")
                    break

                # Check if we've reached expected total
                if len(all_contacts) >= total_contacts:
                    logger.info(f"✅ Fetched all {total_contacts} contacts")
                    break

                # Move to next page
                page += 1

                # Small delay to avoid rate limiting
                await asyncio.sleep(0.2)

            return True, all_contacts

        except Exception as e:
            logger.error(f"Error in fetch_all_contacts_search: {e}", exc_info=True)
            return len(all_contacts) > 0, all_contacts


# MongoDB Helper Functions

async def ensure_integrations_initialized(user_id: str, mongo_client: AsyncIOMotorClient):
    """Ensure user document has integrations structure"""
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    users_collection = db["users"]

    await users_collection.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "created_at": datetime.now(),
                "integrations": {}
            }
        },
        upsert=True
    )

    await users_collection.update_one(
        {"user_id": user_id, "integrations.gohighlevel": {"$exists": False}},
        {
            "$set": {
                "integrations.gohighlevel": {},
                "updated_at": datetime.now()
            }
        }
    )


async def save_agency_token(user_id: str, tokens: dict, mongo_client: AsyncIOMotorClient):
    """Save agency tokens to MongoDB"""
    await ensure_integrations_initialized(user_id, mongo_client)

    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    users_collection = db["users"]

    token_doc = {
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "token_type": tokens.get("token_type"),
        "expires_at": datetime.fromisoformat(tokens.get("expires_at")) if isinstance(tokens.get("expires_at"),
                                                                                     str) else tokens.get("expires_at"),
        "scope": tokens.get("scope"),
        "refreshTokenId": tokens.get("refreshTokenId"),
        "userType": tokens.get("userType"),
        "company_id": tokens.get("companyId"),
        "user_id": tokens.get("userId"),
        "location_id": tokens.get("locationId"),
        "created_at": datetime.now(),
        "updated_at": datetime.now()
    }

    await users_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "integrations.gohighlevel.agency": token_doc,
                "updated_at": datetime.now()
            }
        }
    )

    logger.info(f"Saved agency tokens for user: {user_id}")


async def save_subaccount_token(
        user_id: str,
        location_id: str,
        token_data: dict,
        mongo_client,
        location_details: dict = None,
        contact_count: int = 0  # ✅ Changed from contacts list to count
):
    """
    Save subaccount token with ONLY contact count (not full array)
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]

        # Calculate expiration
        expires_in = token_data.get("expires_in", 3600)
        expires_at = datetime.now() + timedelta(seconds=expires_in)

        # Prepare token data
        token_doc = {
            "access_token": token_data.get("access_token"),
            "refresh_token": token_data.get("refresh_token"),
            "expires_at": expires_at,
            "location_id": location_id,
            "updated_at": datetime.now()
        }

        # Add location details if provided
        if location_details:
            token_doc.update({
                "name": location_details.get("name", ""),
                "address": location_details.get("address", ""),
                "isInstalled": location_details.get("isInstalled", False),
                "trial": location_details.get("trial", {})
            })

        # ✅ FIX: Store ONLY contact count, not full contacts array
        token_doc["contact_count"] = contact_count
        token_doc["contacts_updated_at"] = datetime.now()

        # Save to database
        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    f"integrations.gohighlevel.subaccounts.{location_id}": token_doc,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )

        logger.info(f"✅ Saved subaccount token for {location_id} with {contact_count} contacts (count only)")

    except Exception as e:
        logger.error(f"Failed to save subaccount token: {str(e)}")
        raise


async def fetch_ghl_contacts_on_demand(
        user_id: str,
        location_id: str,
        mongo_client,
        limit: int = 100,
        skip: int = 0,
        cursor: str = None
):
    """
    Fetch contacts directly from GHL API on-demand (no database storage)
    Uses GHL's native pagination

    Returns: (contacts, next_cursor, total_count)
    """
    try:
        # Get access token
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_data = subaccount_tokens.get(location_id, {})
        access_token = location_data.get("access_token")

        if not access_token:
            logger.warning(f"No access token for location {location_id}")
            return [], None, 0

        # Build GHL API URL with pagination
        ghl_url = f"https://services.leadconnectorhq.com/contacts/"
        params = {
            "locationId": location_id,
            "limit": limit
        }

        # Add cursor if provided
        if cursor:
            cursor_parts = cursor.split(":")
            if len(cursor_parts) == 2:
                params["startAfter"] = cursor_parts[0]
                params["startAfterId"] = cursor_parts[1]

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": "2021-07-28",
            "Accept": "application/json"
        }

        # Fetch from GHL API
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(ghl_url, params=params, headers=headers)
            response.raise_for_status()
            data = response.json()

        contacts = data.get("contacts", [])
        meta = data.get("meta", {})

        # Get total count from location data
        total_count = location_data.get("contact_count", 0)

        # Extract cursor for next page
        start_after = meta.get("startAfter")
        start_after_id = meta.get("startAfterId")
        next_cursor = f"{start_after}:{start_after_id}" if start_after and start_after_id else None

        logger.info(
            f"✅ Fetched {len(contacts)} contacts on-demand for location {location_id}"
        )

        return contacts, next_cursor, total_count

    except Exception as e:
        logger.error(f"Error fetching contacts on-demand: {str(e)}")
        return [], None, 0


async def get_contact_count_from_ghl(location_id: str, access_token: str) -> int:
    """
    Fetch ONLY the contact count from GHL API (lightweight)
    Uses limit=1 to minimize data transfer
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"https://services.leadconnectorhq.com/contacts/",
                params={
                    "locationId": location_id,
                    "limit": 1  # Only need count, not data
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Version": "2021-07-28"
                }
            )

            if response.status_code == 200:
                data = response.json()
                # GHL returns total count in meta
                count = data.get("meta", {}).get("total", 0)
                logger.info(f"✅ Fetched contact count for {location_id}: {count}")
                return count
            else:
                logger.error(f"Failed to fetch contact count: {response.status_code}")
                return 0

    except Exception as e:
        logger.error(f"Error fetching contact count: {str(e)}")
        return 0

async def get_subaccount_contacts(
        user_id: str,
        location_id: str = None,
        client_group_id: str = None,
        mongo_client=None,
        limit: int = 100
):
    """
    Fetch contacts from hierarchical structure with flexible filtering

    Args:
        user_id: Required - filter by user
        location_id: Optional - filter by specific location
        client_group_id: Optional - filter by specific client group
        limit: Max number of contacts to return

    Returns:
        List of contact data
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        contacts_collection = db["ghl_contacts"]

        # Build query based on provided filters
        query = {"user_id": user_id}

        if client_group_id:
            query["client_group_id"] = client_group_id
        elif location_id:
            query["location_id"] = location_id

        cursor = contacts_collection.find(
            query,
            {"contact_data": 1, "client_group_name": 1, "location_name": 1}
        ).limit(limit)

        contact_docs = await cursor.to_list(length=limit)

        # Enrich contacts with hierarchy info
        contacts = []
        for doc in contact_docs:
            contact = doc["contact_data"]
            contact["_client_group_name"] = doc.get("client_group_name", "Unassigned")
            contact["_location_name"] = doc.get("location_name", "Unknown")
            contacts.append(contact)

        return contacts

    except Exception as e:
        logger.error(f"Error fetching contacts: {str(e)}")
        return []


async def get_contacts_by_client_group(
        user_id: str,
        client_group_id: str,
        mongo_client,
        skip: int = 0,
        limit: int = 100
):
    """
    Get all contacts for a specific client group with pagination

    Returns: (contacts_list, total_count)
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        contacts_collection = db["ghl_contacts"]

        query = {
            "user_id": user_id,
            "client_group_id": client_group_id
        }

        # Get total count
        total_count = await contacts_collection.count_documents(query)

        # Get paginated contacts
        cursor = contacts_collection.find(
            query,
            {"contact_data": 1, "location_name": 1}
        ).skip(skip).limit(limit)

        contact_docs = await cursor.to_list(length=limit)
        contacts = [doc["contact_data"] for doc in contact_docs]

        return contacts, total_count

    except Exception as e:
        logger.error(f"Error fetching contacts by client group: {str(e)}")
        return [], 0


async def get_user_contacts_summary(user_id: str, mongo_client):
    """
    Get aggregated summary of contacts across all client groups

    Returns hierarchical structure:
    {
        "total_contacts": 150,
        "client_groups": [
            {
                "client_group_id": "...",
                "client_group_name": "...",
                "contact_count": 50,
                "locations": [
                    {
                        "location_id": "...",
                        "location_name": "...",
                        "contact_count": 25
                    }
                ]
            }
        ]
    }
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        contacts_collection = db["ghl_contacts"]

        # Aggregation pipeline to get hierarchical summary
        pipeline = [
            {"$match": {"user_id": user_id}},
            {
                "$group": {
                    "_id": {
                        "client_group_id": "$client_group_id",
                        "client_group_name": "$client_group_name",
                        "location_id": "$location_id",
                        "location_name": "$location_name"
                    },
                    "contact_count": {"$sum": 1}
                }
            },
            {
                "$group": {
                    "_id": {
                        "client_group_id": "$_id.client_group_id",
                        "client_group_name": "$_id.client_group_name"
                    },
                    "total_contacts": {"$sum": "$contact_count"},
                    "locations": {
                        "$push": {
                            "location_id": "$_id.location_id",
                            "location_name": "$_id.location_name",
                            "contact_count": "$contact_count"
                        }
                    }
                }
            },
            {"$sort": {"_id.client_group_name": 1}}
        ]

        results = await contacts_collection.aggregate(pipeline).to_list(None)

        # Format results
        client_groups = []
        total_contacts = 0

        for result in results:
            group_data = {
                "client_group_id": result["_id"]["client_group_id"],
                "client_group_name": result["_id"]["client_group_name"],
                "contact_count": result["total_contacts"],
                "locations": result["locations"]
            }
            client_groups.append(group_data)
            total_contacts += result["total_contacts"]

        return {
            "total_contacts": total_contacts,
            "client_groups": client_groups
        }

    except Exception as e:
        logger.error(f"Error fetching contacts summary: {str(e)}")
        return {
            "total_contacts": 0,
            "client_groups": []
        }


# SOLUTION 2: Add compound indexes for hierarchical queries

async def create_contacts_indexes(mongo_client):
    """
    Create indexes for efficient hierarchical queries
    user > client_group > contacts
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    contacts_collection = db["ghl_contacts"]

    # Index 1: Primary hierarchy lookup (user > client_group)
    await contacts_collection.create_index([
        ("user_id", 1),
        ("client_group_id", 1)
    ], name="user_client_group_idx")

    # Index 2: Location-based lookup (user > location)
    await contacts_collection.create_index([
        ("user_id", 1),
        ("location_id", 1)
    ], name="user_location_idx")

    # Index 3: Individual contact lookup
    await contacts_collection.create_index([
        ("user_id", 1),
        ("contact_id", 1)
    ], name="user_contact_idx", unique=True)

    # Index 4: For aggregation queries (summary statistics)
    await contacts_collection.create_index([
        ("user_id", 1),
        ("client_group_id", 1),
        ("location_id", 1)
    ], name="hierarchy_aggregation_idx")

    # Index 5: For timestamp-based queries (most recent contacts)
    await contacts_collection.create_index([
        ("user_id", 1),
        ("created_at", -1)
    ], name="user_timestamp_idx")

    logger.info("✅ Created hierarchical indexes for ghl_contacts collection")



async def get_location_contacts(
        user_id: str,
        location_id: str,
        mongo_client: AsyncIOMotorClient
) -> list:
    """
    Retrieve contacts for a specific location from separate collection.
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    contacts_collection = db["ghl_contacts"]

    contact_docs = await contacts_collection.find({
        "user_id": user_id,
        "location_id": location_id
    }).to_list(None)

    return [doc["contact_data"] for doc in contact_docs]

async def get_agency_token(user_id: str, mongo_client: AsyncIOMotorClient):
    """Retrieve agency token from MongoDB"""
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    users_collection = db["users"]

    user_doc = await users_collection.find_one(
        {"user_id": user_id},
        projection={"integrations.gohighlevel.agency": 1}
    )

    if user_doc and user_doc.get("integrations", {}).get("gohighlevel", {}).get("agency"):
        return user_doc["integrations"]["gohighlevel"]["agency"]

    return None



# SOLUTION 2: Add index for performance
async def create_contacts_indexes(mongo_client):
    """
    Create indexes for the new contacts collection
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    contacts_collection = db["ghl_contacts"]

    # Compound index for efficient querying
    await contacts_collection.create_index([
        ("user_id", 1),
        ("location_id", 1)
    ])

    # Index for contact lookups
    await contacts_collection.create_index([
        ("user_id", 1),
        ("contact_id", 1)
    ])

    logger.info("✅ Created indexes for ghl_contacts collection")

async def get_subaccount_tokens(user_id: str, mongo_client: AsyncIOMotorClient):
    """
    Retrieve subaccount tokens from MongoDB.
    NOTE: This now returns tokens WITHOUT contacts array.
    Use get_location_contacts() separately to fetch contacts.
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    users_collection = db["users"]

    user_doc = await users_collection.find_one(
        {"user_id": user_id},
        projection={"integrations.gohighlevel.subaccounts": 1}
    )

    if user_doc and user_doc.get("integrations", {}).get("gohighlevel", {}).get("subaccounts"):
        return user_doc["integrations"]["gohighlevel"]["subaccounts"]

    return {}

async def fetch_location_details(location_id: str, access_token: str):
        """Fetch location details with retry logic"""
        async with httpx.AsyncClient(timeout=OAUTH_CONFIG["request_timeout"]) as client:
            for attempt in range(OAUTH_CONFIG["retry_attempts"]):
                try:
                    response = await client.get(
                        f"https://services.leadconnectorhq.com/locations/{location_id}?locationId={location_id}",
                        headers={
                            "Accept": "application/json",
                            "Authorization": f"Bearer {access_token}",
                            "Version": "2021-07-28"
                        }
                    )

                    if response.status_code == 200:
                        data = response.json()
                        location = data.get("location", {})
                        logger.info(f"✅ Fetched location details for {location_id}")
                        return {
                            "name": location.get("name"),
                            "address": location.get("address"),
                            "isInstalled": location.get("isInstalled", False),
                            "trial": location.get("trial", {})
                        }

                    if attempt < OAUTH_CONFIG["retry_attempts"] - 1 and response.status_code >= 500:
                        await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                        continue

                    return None

                except Exception as e:
                    logger.error(f"Error fetching location details (attempt {attempt + 1}): {str(e)}")
                    if attempt < OAUTH_CONFIG["retry_attempts"] - 1:
                        await asyncio.sleep(OAUTH_CONFIG["retry_delay"])
                        continue
                    return None



# Instantiate GHL integration
ghl_integration = GHLIntegration(Agency_CLIENT_ID, Agency_CLIENT_SECRET, REDIRECT_URI)