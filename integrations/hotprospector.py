import httpx
import json
import random
import time
import re
from datetime import datetime, date, timedelta
import logging
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from typing import List, Tuple, Optional, Dict
import asyncio

from utils.phone_normalize import compute_match_keys

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Hot Prospector API configuration
#
# HooksCall "Custom API v2" (CustomApiV2): operations now run against
# service.hookscall.com and authenticate with a Bearer access token exchanged
# from api_uId/api_key (6h access token + long-lived refresh token) instead of
# carrying the credentials in every request body. The operation contract itself
# is unchanged — same Method-in-body routing and {"response":"true","Results":[...]}
# envelope — so only the transport here changes; every method/normalizer is intact.
#
# Everything is env-overridable, and HOTPROSPECTOR_API_VERSION=v1 reverts to the
# legacy app.hotprospector.com path (creds in body, no token) without a deploy.
HOTPROSPECTOR_CONFIG = {
    "api_version": os.getenv("HOTPROSPECTOR_API_VERSION", "v2"),
    "base_url": os.getenv("HOTPROSPECTOR_BASE_URL", "https://service.hookscall.com/glu/api/v2/request"),
    "auth_token_url": os.getenv("HOTPROSPECTOR_AUTH_URL", "https://service.hookscall.com/glu/api/v2/auth/token"),
    "auth_refresh_url": os.getenv("HOTPROSPECTOR_REFRESH_URL", "https://service.hookscall.com/glu/api/v2/auth/refresh"),
    "legacy_base_url": os.getenv("HOTPROSPECTOR_LEGACY_URL", "https://app.hotprospector.com/glu/custom_api"),
    "max_concurrent_requests": 10,
    "batch_size": 500,
    "request_timeout": 60.0,
    "leads_cache_duration": 300  # 5 minutes cache for leads
}

# In-memory cache for pending requests
_pending_requests: Dict[str, asyncio.Future] = {}
_cache_lock = asyncio.Lock()

# Bearer-token cache (v2), keyed by api_uId. A serverless cold start re-auths at
# most once per account; within a warm invocation the 6h token is reused across
# every call. Per-uid locks collapse a burst of concurrent calls into one exchange.
_token_cache: Dict[str, dict] = {}          # api_uid -> {access_token, refresh_token, expires_at}
_token_locks: Dict[str, asyncio.Lock] = {}
_token_locks_guard = asyncio.Lock()

# ── Rate limiting ────────────────────────────────────────────────────────────
# HotProspector enforces a PER-ENDPOINT rate limit (e.g. SearchByUserInput returns
# 429 "Rate limit exceeded ... Try again in 60 seconds" after a short burst). Two
# defenses, both env-overridable:
#   1. Proactive: a minimum interval between calls to the SAME Method, so we rarely
#      hit the limit. Only the known-tight methods are throttled; the rest rely on (2).
#   2. Reactive: on a 429, honor the server's retry delay (Retry-After header or the
#      "try again in N seconds" message) and retry, bounded.
_RATE_MIN_INTERVAL = {
    # Proactive per-method spacing. HP's SearchByUserInput enforces roughly a
    # 5-6 request/min ceiling — a 6s gap gives us ~10 req/min in the worst case,
    # which is still tight but leaves the reactive 429 handler as the safety
    # net for burst violations. The one-shot backfill raises this via env to
    # ~13s so it proactively stays under the limit.
    #
    # Was 3s previously — that let a burst of paginated leads calls trip HP's
    # limit on the very first request per tick, and with only 1 retry (below)
    # the whole location's sync failed. Bumped to 6s along with the reactive
    # settings so tick-level retries actually recover.
    "SearchByUserInput": float(os.getenv("HP_SEARCH_MIN_INTERVAL", "6")),
}
_RATE_DEFAULT_MIN_INTERVAL = float(os.getenv("HP_DEFAULT_MIN_INTERVAL", "0"))
_rate_state: Dict[str, dict] = {}           # method -> {"lock": asyncio.Lock, "last": monotonic ts}
_rate_state_guard = asyncio.Lock()

# Reactive 429 backoff.
#
# Previous defaults were RETRIES=1, WAIT=30, TOTAL=45 — chosen to keep a single
# hp-tick under Vercel's function timeout. In practice that meant: HP responds
# "Rate limit exceeded ... Try again in 60 seconds", we sleep 30s (capped from
# 60), retry too early, get 429 again, give up. Every location whose first
# SearchByUserInput hit the limit failed the whole tick, leaving groups stuck
# in hp_refresh_status="error" with "Failed to fetch leads on first page".
#
# New defaults let us honor the server's 60s advice AT LEAST once:
#   - WAIT=65: hold up to 65s per retry (server says 60s + 5s slack)
#   - RETRIES=2: two 60s retries beat the 5-req/min ceiling in most bursts
#   - TOTAL=180: 2 retries × 65s + normal fetch time, still under Vercel's
#                default 300s function timeout even for the busiest group
_HP_MAX_429_RETRIES = int(os.getenv("HP_MAX_429_RETRIES", "2"))
_HP_MAX_429_WAIT = float(os.getenv("HP_MAX_429_WAIT", "65"))
_HP_MAX_TOTAL_BACKOFF = float(os.getenv("HP_MAX_TOTAL_BACKOFF", "180"))

# Jitter fraction added to every 429 sleep. HP_GROUPS_PER_TICK=3 workers hit
# HP concurrently — without jitter they all wake at the same moment and
# stampede HP's rate-limit window together on the next request.
_HP_JITTER_FRACTION = float(os.getenv("HP_JITTER_FRACTION", "0.15"))


async def _throttle_method(method: str):
    """Proactively space out same-Method requests to respect per-endpoint limits."""
    interval = _RATE_MIN_INTERVAL.get(method, _RATE_DEFAULT_MIN_INTERVAL)
    if interval <= 0:
        return
    async with _rate_state_guard:
        st = _rate_state.get(method)
        if st is None:
            st = {"lock": asyncio.Lock(), "last": 0.0}
            _rate_state[method] = st
    # Serialize calls to this Method and ensure >= interval seconds between them.
    async with st["lock"]:
        wait = st["last"] + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        st["last"] = time.monotonic()


def _retry_after_seconds(response) -> Optional[float]:
    """Seconds to wait after a 429 — from the Retry-After header or the message text."""
    ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except (TypeError, ValueError):
            pass
    try:
        body = response.json()
        msg = body.get("message", "") if isinstance(body, dict) else ""
    except Exception:
        msg = response.text or ""
    m = re.search(r"(\d+)\s*second", msg or "")
    return float(m.group(1)) if m else None


def _parse_hp_call_time(raw):
    """
    Parse HotProspector's display call_time (e.g. "Apr 15, 2026 6:34 pm") into an
    ISO 8601 string so calls can be windowed by date preset. Returns None if it
    can't be parsed. strptime's %p is case-insensitive, so lowercase am/pm works.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


class HotProspectorIntegration:
    def __init__(self, api_uid, api_key):
        self.api_uid = api_uid
        self.api_key = api_key
        self.base_url = HOTPROSPECTOR_CONFIG["base_url"]
        self._semaphore = asyncio.Semaphore(HOTPROSPECTOR_CONFIG["max_concurrent_requests"])
        # Cumulative 429-backoff seconds spent by THIS instance (one group's sync),
        # capped at _HP_MAX_TOTAL_BACKOFF so a burst of rate limits can't run for minutes.
        self._backoff_used = 0.0

    # ── v2 Bearer-token management ───────────────────────────────────────────
    async def _token_lock(self) -> asyncio.Lock:
        """Per-api_uId lock so concurrent calls don't each hit /auth/token."""
        async with _token_locks_guard:
            lock = _token_locks.get(self.api_uid)
            if lock is None:
                lock = asyncio.Lock()
                _token_locks[self.api_uid] = lock
            return lock

    def _store_token_response(self, response) -> Optional[str]:
        """Parse an /auth/token or /auth/refresh response and cache the token."""
        if response.status_code != 200:
            logger.warning(f"HP auth failed: {response.status_code} {response.text[:200]}")
            return None
        try:
            body = response.json()
        except Exception:
            logger.warning("HP auth returned non-JSON body")
            return None
        if not isinstance(body, dict):
            logger.warning(f"HP auth unexpected response: {str(body)[:200]}")
            return None
        # Tolerate either {"success":..,"data":{access_token..}} or a flat body,
        # and don't hinge on the exact success-flag type (bool True / "true").
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        access = data.get("access_token")
        if not access:
            logger.warning(f"HP auth: no access_token in response: {str(body)[:200]}")
            return None
        expires_in = int(data.get("expires_in") or 21600)
        prev = _token_cache.get(self.api_uid) or {}
        _token_cache[self.api_uid] = {
            "access_token": access,
            # Keep the previous refresh token if the refresh response omits a new one.
            "refresh_token": data.get("refresh_token") or prev.get("refresh_token"),
            # 60s safety buffer so we never use a token that's about to expire.
            "expires_at": time.time() + max(expires_in - 60, 60),
        }
        return access

    async def _exchange_credentials(self, client: httpx.AsyncClient) -> Optional[str]:
        """Exchange api_uId/api_key for a fresh access + refresh token."""
        try:
            resp = await client.post(
                HOTPROSPECTOR_CONFIG["auth_token_url"],
                json={"api_uId": self.api_uid, "api_key": self.api_key},
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as e:
            logger.error(f"HP auth token network error: {e}")
            return None
        return self._store_token_response(resp)

    async def _refresh_access_token(self, client: httpx.AsyncClient, refresh_token: str) -> Optional[str]:
        """Use a refresh token to obtain a new access token."""
        try:
            resp = await client.post(
                HOTPROSPECTOR_CONFIG["auth_refresh_url"],
                json={"refresh_token": refresh_token},
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as e:
            logger.error(f"HP auth refresh network error: {e}")
            return None
        return self._store_token_response(resp)

    async def _get_access_token(self, force: bool = False) -> Optional[str]:
        """
        Return a valid v2 access token. Serves the cached token until it nears
        expiry; otherwise refreshes (preferred) or re-exchanges credentials.
        `force=True` always fetches a new token (used after a 401).
        """
        cached = _token_cache.get(self.api_uid)
        if (not force and cached and cached.get("access_token")
                and cached.get("expires_at", 0) > time.time()):
            return cached["access_token"]

        lock = await self._token_lock()
        async with lock:
            # Re-check inside the lock — another coroutine may have just refreshed.
            cached = _token_cache.get(self.api_uid)
            if (not force and cached and cached.get("access_token")
                    and cached.get("expires_at", 0) > time.time()):
                return cached["access_token"]

            async with httpx.AsyncClient(timeout=HOTPROSPECTOR_CONFIG["request_timeout"]) as client:
                refresh_token = (cached or {}).get("refresh_token")
                token = None
                if refresh_token:
                    token = await self._refresh_access_token(client, refresh_token)
                if not token:
                    token = await self._exchange_credentials(client)
                return token

    # ── Transport ────────────────────────────────────────────────────────────
    async def _post_and_parse(self, client, url, body, headers):
        """
        POST and parse the HotProspector envelope.
        Returns (success: bool, data_or_error: Any, status_code: Optional[int]).
        """
        try:
            response = await client.post(url, json=body, headers=headers)
        except httpx.RequestError as e:
            logger.error(f"Network error during Hot Prospector request: {str(e)}")
            return False, {"error": f"Network error: {str(e)}"}, None

        status = response.status_code
        if status != 200:
            # Surface 401 so the caller can refresh the token and retry; surface the
            # retry delay on 429 so the caller can back off the right amount.
            err = {"error": f"API request failed with status {status}", "status_code": status}
            if status == 429:
                err["retry_after"] = _retry_after_seconds(response)
            logger.error(f"Hot Prospector API error: {status} {response.text[:300]}")
            return False, err, status

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {str(e)}")
            return False, {"error": f"Invalid JSON response: {str(e)}"}, status

        # API-level error envelope: operations use {"response":"false","message":...};
        # auth-style payloads use {"success": false, "message": ...}.
        if isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict) and first.get("response") == "false":
                return False, {"error": first.get("message", "Unknown API error"), "status_code": 400}, status
        elif isinstance(data, dict):
            if data.get("response") == "false" or data.get("success") is False:
                return False, {"error": data.get("message", "Unknown API error"), "status_code": 400}, status

        return True, data, status

    async def _execute(self, payload: dict):
        """One request: proactive throttle, then v2 Bearer auth (401 refresh) + 429 backoff."""
        # Proactively space out same-Method calls to stay under the per-endpoint limit.
        await _throttle_method(payload.get("Method", ""))
        is_v1 = HOTPROSPECTOR_CONFIG.get("api_version") == "v1"

        async with httpx.AsyncClient(timeout=HOTPROSPECTOR_CONFIG["request_timeout"]) as client:
            if is_v1:
                # Legacy: credentials travel in the body, no Authorization header.
                url = HOTPROSPECTOR_CONFIG["legacy_base_url"]
                body = payload
                headers = {"Content-Type": "application/json"}
            else:
                # v2: credentials live in the Bearer header, not the body.
                url = HOTPROSPECTOR_CONFIG["base_url"]
                body = {k: v for k, v in payload.items() if k not in ("api_uId", "api_key")}
                token = await self._get_access_token()
                if not token:
                    return False, {"error": "HotProspector authentication failed", "status_code": 401}
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

            did_refresh = False
            rate_retries = 0
            while True:
                ok, data, status = await self._post_and_parse(client, url, body, headers)

                # Token expired/invalid mid-flight — force a new one and retry once.
                if status == 401 and not is_v1 and not did_refresh:
                    did_refresh = True
                    token = await self._get_access_token(force=True)
                    if not token:
                        return False, {"error": "HotProspector authentication failed", "status_code": 401}
                    headers["Authorization"] = f"Bearer {token}"
                    continue

                # Rate limited — back off the server-specified delay and retry, bounded
                # both per-call (retries) and per-instance (cumulative backoff budget).
                # Jitter (±_HP_JITTER_FRACTION) desynchronizes the HP_GROUPS_PER_TICK
                # parallel workers so they don't stampede HP's rate window together
                # on the retry.
                if status == 429 and rate_retries < _HP_MAX_429_RETRIES:
                    delay = data.get("retry_after") if isinstance(data, dict) else None
                    delay = min(float(delay) if delay else 60.0, _HP_MAX_429_WAIT)
                    jitter = delay * _HP_JITTER_FRACTION * (2 * random.random() - 1)
                    delay = max(1.0, delay + jitter)
                    if self._backoff_used + delay > _HP_MAX_TOTAL_BACKOFF:
                        logger.warning(
                            f"HP 429 on '{payload.get('Method')}' but backoff budget "
                            f"exhausted ({self._backoff_used:.0f}s used) — giving up to "
                            f"protect the function timeout"
                        )
                        return ok, data
                    rate_retries += 1
                    self._backoff_used += delay
                    logger.warning(
                        f"HP rate limited on '{payload.get('Method')}'; backing off "
                        f"{delay:.1f}s (retry {rate_retries}/{_HP_MAX_429_RETRIES})"
                    )
                    await asyncio.sleep(delay)
                    continue

                return ok, data

    async def _make_request(self, payload: dict, use_dedup: bool = True):
        """Make a request to the HotProspector API (v2 Bearer auth) with deduplication."""
        cache_key = json.dumps(payload, sort_keys=True)

        future = None
        if use_dedup:
            async with _cache_lock:
                if cache_key in _pending_requests:
                    logger.info(f"Returning cached pending request for {payload.get('Method')}")
                    return await _pending_requests[cache_key]
                future = asyncio.Future()
                _pending_requests[cache_key] = future

        result = (False, {"error": "request aborted"})
        try:
            async with self._semaphore:
                result = await self._execute(payload)
        except Exception as e:
            logger.error(f"Hot Prospector request failed: {str(e)}")
            result = (False, {"error": str(e)})
        finally:
            if use_dedup:
                if future is not None and not future.done():
                    future.set_result(result)
                async with _cache_lock:
                    _pending_requests.pop(cache_key, None)

        return result

    async def search_leads_by_ghl_location(
            self,
            ghl_location_id: str,
            search_field: str = "",
            search_text: str = "",
            limit: int = 100,
            offset: int = 0,
    ):
        """
        Search leads scoped to a GHL locationId.

        SearchByUserInput paginates: it serves one page (default 50, capped at
        max_limit=100) and reports the TRUE total under `total_count`. Callers must
        read total_count — NOT len(Results) — for an accurate lead count, otherwise
        every location with >page_size leads looks like exactly one page.

        Returns (True, {"leads": [...], "total_count": int, "total_pages": int}) on
        success, else (False, {"error": ...}).
        """
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "locationId": ghl_location_id,
            "searchField": search_field,
            "searchText": search_text,
            "limit": limit,
            "offset": offset,
            "Method": "SearchByUserInput"
        }

        success, result = await self._make_request(payload, use_dedup=False)
        if not success:
            return False, result

        body = result[0] if isinstance(result, list) and result else result
        if isinstance(body, dict) and body.get("response") == "true":
            leads = body.get("Results", []) or []
            total = int(body.get("total_count") or len(leads))
            logger.info(
                f"Fetched {len(leads)} of {total} leads from GHL location "
                f"{ghl_location_id} (offset={offset})"
            )
            return True, {
                "leads": leads,
                "total_count": total,
                "total_pages": int(body.get("total_pages") or 1),
            }

        return False, {"error": "Unexpected response format or no results"}

    async def fetch_all_leads_from_ghl_location(self, ghl_location_id: str, with_meta: bool = False):
        """
        Fetch ALL leads for a GHL location by paginating SearchByUserInput.

        History: this function used to return only the first page (100 leads).
        The docstring justified it as a rate-limit concession — but the caller
        (save_hotprospector_leads_to_collection) then deleted every stored
        lead and inserted only the returned page, silently truncating any
        location with more than 100 leads to exactly 100. That was a real UX
        bug: locations with 700+ leads only ever showed 100 in Sales-Hub, and
        pruning cost real data on every cron cycle.

        Now: page through everything up to a defensive cap (MAX_PAGES *
        PAGE_SIZE = 20k leads). If pagination gets interrupted mid-way
        (transient error, empty page), we abort the loop and flag the result
        as truncated so callers know not to prune existing leads.

        Returns:
            with_meta=False (default): (True, [lead, ...])  — backward compatible
            with_meta=True:  (True, {"leads": [...], "total_count": int, "truncated": bool})
            on failure:      (False, <error dict>)
        """
        logger.info(f"Fetching leads for GHL location {ghl_location_id}")

        PAGE_SIZE = 100          # HP's response page size (informational; API decides)
        MAX_PAGES = 200          # safety cap — 20k leads before we bail

        by_id: Dict = {}
        total_count = 0
        truncated = False
        first_call_failed = False

        for page_idx in range(MAX_PAGES):
            offset = page_idx * PAGE_SIZE
            success, data = await self.search_leads_by_ghl_location(
                ghl_location_id=ghl_location_id,
                search_field="",
                search_text="",
                offset=offset,
            )

            if not success:
                # First-page failure → this location is unreachable this run.
                # Later-page failure → return what we have so far, flagged truncated.
                if page_idx == 0:
                    logger.error(f"Failed to fetch leads for location {ghl_location_id}: {data}")
                    first_call_failed = True
                    break
                logger.warning(
                    f"Pagination interrupted at offset={offset} for {ghl_location_id}: {data}"
                )
                truncated = True
                break

            page_leads = data.get("leads", []) if isinstance(data, dict) else []
            total_count = int(data.get("total_count", total_count) or total_count or 0)

            # Merge by LeadId (dedup across pages defensively — HP occasionally
            # returns the same lead twice near page boundaries).
            new_this_page = 0
            for lead in page_leads:
                lead_id = lead.get("LeadId")
                if not lead_id or lead_id in by_id:
                    continue
                by_id[lead_id] = lead
                new_this_page += 1

            # Done when we've collected the full reported total, or when a page
            # comes back shorter than a full page (last page).
            if total_count and len(by_id) >= total_count:
                break
            if len(page_leads) < PAGE_SIZE:
                break
            # Also stop if a page added nothing new — protects against a loop
            # in a broken pagination implementation.
            if new_this_page == 0:
                logger.warning(
                    f"Pagination at offset={offset} added 0 new leads for {ghl_location_id}; stopping"
                )
                truncated = True
                break
        else:
            # Hit MAX_PAGES without finishing — flag truncated so we don't prune.
            truncated = True
            logger.warning(
                f"Hit MAX_PAGES={MAX_PAGES} for {ghl_location_id}; "
                f"got {len(by_id)}/{total_count or '?'} leads — flagging truncated"
            )

        if first_call_failed:
            # Preserve original failure shape so callers checking `success`
            # get an error dict, not an empty payload masquerading as success.
            return False, {"error": "Failed to fetch leads on first page"}

        if total_count and len(by_id) < total_count:
            truncated = True

        deduplicated_leads = list(by_id.values())
        logger.info(
            f"✅ Fetched {len(deduplicated_leads)} of {total_count or len(deduplicated_leads)} leads "
            f"for GHL location {ghl_location_id}{' (truncated)' if truncated else ''}"
        )

        if with_meta:
            return True, {
                "leads": deduplicated_leads,
                "total_count": total_count or len(deduplicated_leads),
                "truncated": truncated,
            }
        return True, deduplicated_leads

    async def _fetch_leads_for_char(self, ghl_location_id: str, char: str) -> Tuple[bool, List]:
        """Helper method to fetch leads for a single character"""
        try:
            success, data = await self.search_leads_by_ghl_location(
                ghl_location_id=ghl_location_id,
                search_field="email",
                search_text=char
            )
            leads = data.get("leads", []) if success and isinstance(data, dict) else []
            return success, leads
        except Exception as e:
            logger.error(f"Error fetching leads for char '{char}': {e}")
            return False, []

    async def get_member_users(self):
        """Get all member users (team members)"""
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "Method": "getMemberUsers"
        }

        success, result = await self._make_request(payload)
        if not success:
            return False, result

        # Response may be a dict or a single-element list wrapping the dict.
        body = result[0] if isinstance(result, list) and result else result
        if isinstance(body, dict):
            # Per HP docs the members live under "data"; older shapes used "message".
            members = body.get("data")
            if members is None:
                members = body.get("message", [])
            if not isinstance(members, list):
                members = []
            logger.info(f"Fetched {len(members)} team members from Hot Prospector")
            return True, members

        return False, {"error": "Unexpected response format"}

    async def get_member_dashboard_data(self, date_str: str = None):
        """
        Per-agent dashboard metrics for a single day via getMemberDashboardData
        (outbound/inbound counts, answered, answer rate, convos, appts, talk time, …).

        Args:
            date_str: 'Y-m-d' (omit for today; pass a past date for historical data).

        Returns:
            (True, {"date": <str>, "results": [<agent metrics dict>]}) on success,
            else (False, {"error": ...}).
        """
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "Method": "getMemberDashboardData",
        }
        if date_str:
            payload["date"] = date_str

        success, result = await self._make_request(payload, use_dedup=False)
        if not success:
            return False, result

        body = result[0] if isinstance(result, list) and result else result
        if isinstance(body, dict) and body.get("response") == "true":
            # Live API returns "Results" (capital R) even though the docs show "results".
            results = body.get("Results")
            if results is None:
                results = body.get("results", [])
            return True, {"date": body.get("date"), "results": results or []}
        return False, {"error": "Unexpected getMemberDashboardData response format"}

    async def fetch_lead_call_logs(self, lead_id: str):
        """
        Fetch call logs for a specific lead.

        Args:
            lead_id: The LeadId from HotProspector

        Returns:
            Tuple of (success: bool, call_logs: list)
        """
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "LeadId": str(lead_id),
            "Method": "FetchLeadCallLogs"
        }

        success, result = await self._make_request(payload, use_dedup=False)
        if not success:
            return False, result

        # Extract call logs from response
        if isinstance(result, list) and len(result) > 0:
            if result[0].get("response") == "true":
                call_logs = result[0].get("Results", [])
                logger.info(f"Fetched {len(call_logs)} call logs for lead {lead_id}")
                return True, call_logs

        return False, {"error": "No call logs found or unexpected response format"}

    async def fetch_user_call_logs(
            self,
            ghl_location_id: str = None,
            from_date: str = None,
            to_date: str = None,
            call_type: str = "",
            page_size: int = 500,
            max_records: int = 100000,
    ):
        """
        Bulk-fetch ALL call logs for a GHL location via FetchUserCallLog.

        One paginated endpoint (limit/offset) instead of one request per lead, so a
        location with thousands of leads costs a handful of calls rather than thousands.

        Args:
            ghl_location_id: GHL locationId to scope the logs to.
            from_date / to_date: optional Y-m-d filters (HP caps the range at 30 days).
            call_type: "inbound", "outbound", or "" for both.
            page_size: records per request (HP default is 100; we ask for more).
            max_records: hard safety cap so a misbehaving has_more can't loop forever.

        Returns:
            (True, {"call_logs": [...raw...], "inbound_count", "outbound_count",
                    "total_records"}) on success, else (False, {"error": ...}).
        """
        all_logs = []
        offset = 0
        inbound_count = 0
        outbound_count = 0
        total_records = 0

        while True:
            payload = {
                "api_uId": self.api_uid,
                "api_key": self.api_key,
                "Method": "FetchUserCallLog",
                "type": call_type or "",
                "limit": page_size,
                "offset": offset,
                "sort_by": "call_time",
                "sort_order": "DESC",
            }
            # HP does not reliably tag call logs with the GHL locationId, so callers
            # usually omit it and match calls to leads by phone/email instead.
            if ghl_location_id:
                payload["locationId"] = ghl_location_id
            if from_date:
                payload["from_date"] = from_date
            if to_date:
                payload["to_date"] = to_date

            success, result = await self._make_request(payload, use_dedup=False)
            if not success:
                # If we already pulled some pages, return what we have so far.
                if all_logs:
                    break
                return False, result

            # Response may be a dict or a single-element list wrapping the dict.
            body = result[0] if isinstance(result, list) and result else result
            if not isinstance(body, dict) or body.get("response") != "true":
                if all_logs:
                    break
                return False, {"error": "Unexpected FetchUserCallLog response format"}

            page = body.get("Results", []) or []
            all_logs.extend(page)

            # Aggregate counts come back on every page; keep the latest non-empty values.
            inbound_count = int(body.get("inbound_count") or inbound_count or 0)
            outbound_count = int(body.get("outbound_count") or outbound_count or 0)
            total_records = int(body.get("total_records") or total_records or 0)

            has_more = bool(body.get("has_more"))
            next_offset = body.get("next_offset")
            if not has_more or not page or len(all_logs) >= max_records:
                break
            offset = int(next_offset) if next_offset is not None else offset + page_size

        logger.info(
            f"FetchUserCallLog: {len(all_logs)} call logs for location {ghl_location_id} "
            f"(inbound={inbound_count}, outbound={outbound_count}, total={total_records})"
        )
        return True, {
            "call_logs": all_logs,
            "inbound_count": inbound_count,
            "outbound_count": outbound_count,
            "total_records": total_records or len(all_logs),
        }

    async def fetch_call_logs_for_location(
            self,
            ghl_location_id: str,
            lookback_days: int = 400,
            window_days: int = 30,
    ):
        """
        Fetch ALL of a location's call logs over the past `lookback_days` by walking
        <= `window_days` date windows.

        FetchUserCallLog caps from_date/to_date at a 30-day range, AND with no dates
        it returns only a small default (recent) window — so to get the real history
        we must page through explicit date windows. locationId filtering works fine
        once a date range is supplied.

        Returns (ok, [raw call dicts]).
        """
        end = date.today()
        start = end - timedelta(days=lookback_days)
        all_calls = []
        any_ok = False
        cur = start
        while cur <= end:
            win_end = min(cur + timedelta(days=window_days - 1), end)
            ok, payload = await self.fetch_user_call_logs(
                ghl_location_id,
                from_date=cur.isoformat(),
                to_date=win_end.isoformat(),
            )
            if ok and isinstance(payload, dict):
                any_ok = True
                all_calls.extend(payload.get("call_logs", []) or [])
            cur = win_end + timedelta(days=1)

        logger.info(
            f"Fetched {len(all_calls)} call logs for location {ghl_location_id} "
            f"over last {lookback_days}d ({window_days}d windows)"
        )
        return any_ok, all_calls

    def normalize_call_log(self, call_log: dict):
        """
        Normalize a call log object to a standard format.

        Args:
            call_log: Raw call log data from HotProspector API
        """
        return {
            "lead_id": call_log.get("leadId"),
            "lead_name": call_log.get("lead_Name"),
            "location_name": call_log.get("location_name"),
            "from_number": call_log.get("from_number"),
            "to_number": call_log.get("to_number"),
            "recording_url": call_log.get("recording"),
            "recording_id": call_log.get("recordingId"),
            "group": call_log.get("group"),
            "caller_name": call_log.get("caller_name"),
            "call_time": call_log.get("call_time"),
            "duration": int(call_log.get("duration", 0)),
            "city": call_log.get("city", ""),
            "state": call_log.get("state", ""),
            "transfer": call_log.get("transfer") == "Yes",
            "call_status": call_log.get("call_status"),
            "speed_to_lead": int(call_log.get("speed_to_lead", 0))
        }

    def normalize_user_call_log(self, call_log: dict, location_name: str = None):
        """
        Normalize a FetchUserCallLog row to the same shape the Sales-Hub UI consumes
        (see normalize_call_log).

        FetchUserCallLog differs from FetchLeadCallLogs: the call DIRECTION lives in
        `call_type` (the raw `call_status` is the outcome, e.g. "completed"), and there
        is no per-row location name. The frontend reads `call_status` as the direction,
        so we map it from `call_type` and stamp the GHL location name we already know.
        """
        return {
            "lead_id": call_log.get("leadId"),
            "lead_name": call_log.get("lead_Name"),
            "location_name": location_name or "",
            "from_number": call_log.get("from_number"),
            "to_number": call_log.get("to_number"),
            "recording_url": call_log.get("recording") or None,
            "recording_id": call_log.get("recordingId"),
            "group": call_log.get("group"),
            "caller_name": call_log.get("caller_name"),
            "call_time": call_log.get("call_time"),
            "call_time_iso": _parse_hp_call_time(call_log.get("call_time")),
            "duration": int(call_log.get("duration") or 0),
            "city": call_log.get("city", ""),
            "state": call_log.get("state", ""),
            "transfer": call_log.get("transfer") == "Yes",
            "call_status": (call_log.get("call_type") or "").lower(),
            "speed_to_lead": int(call_log.get("speed_to_lead") or 0),
        }

    def normalize_lead(self, lead: dict, ghl_location_id: str, ghl_location_name: str = None,
                       client_group_name: str = None):
        """
        Normalize a lead object to a standard format.

        Args:
            lead: Raw lead data from HotProspector API
            ghl_location_id: The GHL location ID
            ghl_location_name: The GHL location name (e.g., "Acme Corp - Main")
            client_group_name: The client group name from client_groups collection (e.g., "Acme Corporation")
        """
        custom_fields = lead.get("Lead_Custom_Fields") or {}

        # Parse GroupId as it might contain multiple IDs
        group_ids = []
        if lead.get("GroupId"):
            group_ids = [gid.strip() for gid in str(lead.get("GroupId")).split(",") if gid.strip()]

        # Parse tags
        tags = []
        if lead.get("Tags"):
            tags = [tag.strip() for tag in str(lead.get("Tags")).split(",") if tag.strip()]

        return {
            "id": lead.get("LeadId"),
            "ghl_location_id": ghl_location_id,
            "ghl_location_name": ghl_location_name or "Unknown Location",
            "client_name": client_group_name if client_group_name else "No Client Group",
            "group_ids": group_ids,
            "tags": tags,
            "first_name": lead.get("Firstname", ""),
            "last_name": lead.get("Lastname", ""),
            "email": lead.get("E-Mail", ""),
            "phone": lead.get("Phone", ""),
            "mobile": lead.get("Mobile", ""),
            "country_code": lead.get("CountryCode", ""),
            "zipcode": lead.get("Zipcode", ""),
            "city": lead.get("City", ""),
            "state": lead.get("State", ""),
            "address": lead.get("Address", ""),
            "company": lead.get("Company", ""),
            "website": lead.get("Website", ""),
            "custom_fields": custom_fields,
            "source": "HotProspector",
            "call_logs_count": 0,
            "call_logs": []
        }

    async def fetch_call_logs_for_leads_batch(self, lead_ids: List[str]):
        """
        Fetch call logs for multiple leads in parallel.

        Args:
            lead_ids: List of LeadId strings

        Returns:
            Dict mapping lead_id -> list of call logs
        """
        logger.info(f"Fetching call logs for {len(lead_ids)} leads")

        # Batch the requests to avoid overwhelming the API
        batch_size = HOTPROSPECTOR_CONFIG["max_concurrent_requests"]
        all_call_logs = {}

        for i in range(0, len(lead_ids), batch_size):
            batch = lead_ids[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(lead_ids) + batch_size - 1) // batch_size

            logger.info(f"Processing call logs batch {batch_num}/{total_batches}")

            tasks = [self.fetch_lead_call_logs(lead_id) for lead_id in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for lead_id, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning(f"Error fetching call logs for lead {lead_id}: {result}")
                    all_call_logs[lead_id] = []
                    continue

                success, call_logs = result
                if success:
                    normalized_logs = [self.normalize_call_log(log) for log in call_logs]
                    all_call_logs[lead_id] = normalized_logs
                else:
                    all_call_logs[lead_id] = []

        logger.info(f"✅ Fetched call logs for {len(all_call_logs)} leads")
        return all_call_logs


# ============================================
# UPDATED MongoDB Storage Functions
# Now supports call logs in cache
# ============================================

async def save_hotprospector_leads_to_collection(
        user_id: str,
        ghl_location_id: str,
        leads: list,
        mongo_client: AsyncIOMotorClient,
        *,
        prune_missing: bool = False,
):
    """
    Upsert HP leads (with nested call_logs) for a specific GHL location.

    History: this function used to `delete_many` every existing lead for the
    location and then bulk-insert whatever the caller passed. That silently
    wiped a location's cache on any bad HP day — a rate-limited fetch that
    returned 0 leads deleted every stored lead. Multiplied by the delete-then-
    insert running on every cron cycle, real user data was at constant risk.

    Now: upsert by (user_id, ghl_location_id, lead_data.id). Existing leads
    not in this batch are preserved by default. Two additional protections:

      - If `leads` is empty, do nothing — refuses to wipe on "we got nothing".
      - When an incoming lead has an empty `call_logs` list, we treat that as
        "caller didn't fetch calls this run" and preserve whatever call_logs
        are already stored on that doc, only refreshing scalar fields.
        The synthetic "Unmatched calls" pseudo-lead is unaffected because
        it's only created (upstream in hp_service) when unmatched_calls is
        non-empty — so its call_logs list is never empty at save time.

    Callers that KNOW they've fetched the full lead set (successful
    pagination, no rate-limit truncation) can pass `prune_missing=True` to
    drop leads that HP no longer returns. Default stays safe.
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        leads_collection = db["hotprospector_leads"]

        if not leads:
            # Empty input is almost always a fetch failure signal (rate
            # limit, transient API blip). Skipping the wipe is the whole
            # point of this rewrite.
            logger.warning(
                f"No leads to save for GHL location {ghl_location_id}; "
                f"skipping (avoids wiping cache on empty fetches)."
            )
            return

        batch_size = HOTPROSPECTOR_CONFIG["batch_size"]
        total_batches = (len(leads) + batch_size - 1) // batch_size
        logger.info(
            f"Upserting {len(leads)} leads (call logs when present) for "
            f"{ghl_location_id} in {total_batches} batches of {batch_size}"
        )

        seen_lead_ids: set = set()
        total_calls = 0
        ops: List[UpdateOne] = []
        now = datetime.now()

        for lead in leads:
            lead_id = lead.get("id")
            if not lead_id:
                # Can't safely upsert without a stable id; skip.
                continue
            seen_lead_ids.add(str(lead_id))
            total_calls += lead.get("call_logs_count", 0)

            filt = {
                "user_id": user_id,
                "ghl_location_id": ghl_location_id,
                "lead_data.id": lead_id,
            }
            match_keys = compute_match_keys(lead.get("email"), lead.get("phone"))
            incoming_calls = lead.get("call_logs") or []

            if incoming_calls:
                # Fresh call data — replace lead_data wholesale.
                ops.append(UpdateOne(
                    filt,
                    {
                        "$set": {
                            "user_id": user_id,
                            "ghl_location_id": ghl_location_id,
                            "lead_data": lead,
                            "match_keys": match_keys,
                            "updated_at": now,
                        },
                        "$setOnInsert": {"created_at": now},
                    },
                    upsert=True,
                ))
            else:
                # No fresh calls this run — preserve any existing
                # lead_data.call_logs / call_logs_count already on this doc.
                # Update all OTHER lead_data.* fields via dot-notation so a
                # renamed/updated scalar still lands.
                dotted_set = {
                    "user_id": user_id,
                    "ghl_location_id": ghl_location_id,
                    "match_keys": match_keys,
                    "updated_at": now,
                }
                for k, v in lead.items():
                    if k in ("call_logs", "call_logs_count"):
                        continue
                    dotted_set[f"lead_data.{k}"] = v
                # NOTE: `lead_data.id` is intentionally NOT in $setOnInsert —
                # it's already written by the `for k, v in lead.items()` loop
                # above (which does dotted_set["lead_data.id"] = lead["id"]).
                # MongoDB rejects an update where both $set and $setOnInsert
                # target the same path with "Updating the path X would create
                # a conflict at X", failing the whole bulk_write batch and
                # leaving the group in hp_refresh_status="error".
                ops.append(UpdateOne(
                    filt,
                    {
                        "$set": dotted_set,
                        "$setOnInsert": {
                            "created_at": now,
                            "lead_data.call_logs": [],
                            "lead_data.call_logs_count": 0,
                        },
                    },
                    upsert=True,
                ))

        # Batch the bulk writes so a location with tens of thousands of
        # leads doesn't build one giant payload.
        if ops:
            for i in range(0, len(ops), batch_size):
                chunk = ops[i:i + batch_size]
                await leads_collection.bulk_write(chunk, ordered=False)
                logger.info(
                    f"Upserted batch {(i // batch_size) + 1}/{total_batches} "
                    f"({len(chunk)} leads)"
                )

        # Optional prune. Callers only pass prune_missing=True when they're
        # confident the fetch was complete (successful pagination, no
        # rate-limit truncation) — otherwise stale leads stay put rather
        # than risk deleting real data.
        if prune_missing and seen_lead_ids:
            del_res = await leads_collection.delete_many({
                "user_id": user_id,
                "ghl_location_id": ghl_location_id,
                "lead_data.id": {"$nin": list(seen_lead_ids)},
            })
            if del_res.deleted_count:
                logger.info(
                    f"Pruned {del_res.deleted_count} stale leads for GHL location "
                    f"{ghl_location_id}"
                )

        # User document metadata (unchanged behaviour).
        users_collection = db["users"]
        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    f"integrations.hotprospector.ghl_locations.{ghl_location_id}": {
                        "total_leads": len(leads),
                        "total_calls": total_calls,
                        "last_fetched": now,
                    },
                    "updated_at": now,
                }
            },
            upsert=True,
        )

        logger.info(
            f"✅ Upserted {len(leads)} leads with {total_calls} call logs "
            f"for GHL location {ghl_location_id} for user: {user_id}"
        )

    except Exception as e:
        logger.error(f"Failed to save Hot Prospector leads for user {user_id}: {str(e)}")
        raise


async def get_hotprospector_leads_from_collection(
        user_id: str,
        ghl_location_id: str = None,
        mongo_client: AsyncIOMotorClient = None,
        skip: int = 0,
        limit: Optional[int] = None
):
    """
    Retrieve Hot Prospector leads WITH CALL LOGS from cache.

    KEY CHANGE: Call logs are now included in cached lead_data
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        leads_collection = db["hotprospector_leads"]

        # Build query
        query = {"user_id": user_id}
        if ghl_location_id:
            query["ghl_location_id"] = ghl_location_id

        # Check cache validity for specific location
        if ghl_location_id:
            users_collection = db["users"]
            user_doc = await users_collection.find_one(
                {"user_id": user_id},
                projection={
                    f"integrations.hotprospector.ghl_locations.{ghl_location_id}": 1
                }
            )

            if user_doc:
                location_meta = (user_doc.get("integrations", {})
                                 .get("hotprospector", {})
                                 .get("ghl_locations", {})
                                 .get(ghl_location_id, {}))

                last_fetched = location_meta.get("last_fetched")
                total_count = location_meta.get("total_leads", 0)
                # total_calls = location_meta.get("total_calls", 0)

                if last_fetched:
                    cache_age = (datetime.now() - last_fetched).total_seconds()
                    cache_valid = cache_age < HOTPROSPECTOR_CONFIG["leads_cache_duration"]

                    if not cache_valid:
                        logger.info(f"Cache expired for {ghl_location_id} (age: {cache_age:.0f}s)")
                        return None, 0

                    logger.info(
                        f"Cache valid for {ghl_location_id}: {total_count} leads, "
                        # f"{total_calls} calls (age: {cache_age:.0f}s)"
                    )

        # Fetch leads with call logs
        cursor = leads_collection.find(
            query,
            projection={"lead_data": 1, "_id": 0}
        ).skip(skip)

        if limit is not None and limit > 0:
            cursor = cursor.limit(limit)

        leads = []
        # total_calls = 0
        async for doc in cursor:
            lead_data = doc["lead_data"]
            # Call logs are already in lead_data!
            leads.append(lead_data)
            # total_calls += lead_data.get("call_logs_count", 0)

        # Get total count
        total_count = await leads_collection.count_documents(query)

        logger.info(
            # f"✅ Returned {len(leads)} cached leads with {total_calls} call logs "
            f"(total: {total_count})"
        )
        return leads, total_count

    except Exception as e:
        logger.error(f"Failed to retrieve Hot Prospector leads for user {user_id}: {str(e)}")
        return None, 0


async def save_hotprospector_credentials(
        user_id: str,
        api_uid: str,
        api_key: str,
        mongo_client: AsyncIOMotorClient
):
    """Save Hot Prospector API credentials to MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]

        credentials_doc = {
            "api_uid": api_uid,
            "api_key": api_key,
            "connected": True,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }

        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "integrations.hotprospector.credentials": credentials_doc,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )

        logger.info(f"Saved Hot Prospector credentials for user: {user_id}")

    except Exception as e:
        logger.error(f"Failed to save Hot Prospector credentials for user {user_id}: {str(e)}")
        raise


async def get_hotprospector_credentials(user_id: str, mongo_client: AsyncIOMotorClient):
    """Retrieve Hot Prospector credentials from MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]

        user_doc = await users_collection.find_one(
            {"user_id": user_id},
            projection={"integrations.hotprospector.credentials": 1}
        )

        if (user_doc and
                user_doc.get("integrations", {})
                        .get("hotprospector", {})
                        .get("credentials")):
            return user_doc["integrations"]["hotprospector"]["credentials"]

        return None

    except Exception as e:
        logger.error(f"Failed to retrieve Hot Prospector credentials for user {user_id}: {str(e)}")
        return None


async def get_client_group_mapping(user_id: str, mongo_client: AsyncIOMotorClient) -> Dict[str, str]:
    """
    Get a mapping of GHL location IDs to client group names.
    If multiple client groups share the same GHL location, concatenate their names.

    Returns:
        Dict mapping ghl_location_id -> "Group1, Group2, Group3"
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        client_groups_collection = db["client_groups"]

        # Find all client groups for this user that have a ghl_location_id
        client_groups = await client_groups_collection.find({
            "user_id": user_id,
            "ghl_location_id": {"$exists": True, "$ne": None}
        }).to_list(None)

        logger.info(f"Found {len(client_groups)} client groups for user {user_id}")

        # Create mapping: ghl_location_id -> list of client_group_names
        location_to_groups = {}
        for group in client_groups:
            ghl_location_id = group.get("ghl_location_id")
            client_name = group.get("name")  # This is the "Birdy Ai" field

            if ghl_location_id and client_name:
                if ghl_location_id not in location_to_groups:
                    location_to_groups[ghl_location_id] = []
                location_to_groups[ghl_location_id].append(client_name)
                logger.info(f"  Added mapping: {ghl_location_id} -> {client_name}")

        # Convert lists to comma-separated strings
        mapping = {}
        for location_id, group_names in location_to_groups.items():
            # Sort for consistent ordering
            group_names.sort()
            mapping[location_id] = ", ".join(group_names)
            logger.info(f"  Final mapping: {location_id} -> {mapping[location_id]}")

        logger.info(f"Created client group mapping for user {user_id}: {len(mapping)} unique locations mapped")
        logger.debug(f"Full mapping: {mapping}")
        return mapping

    except Exception as e:
        logger.error(f"Error getting client group mapping for user {user_id}: {str(e)}")
        return {}