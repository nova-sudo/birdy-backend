"""
Meta Graph reads for tracked creative-test ads: per-ad daily insights (batched),
account daily totals for baselines, and ad effective_status.

Callers pass the access token. Errors raise MetaReadError; `retryable` marks the
rate-limit codes (4, 17, 613) so the caller can back off and retry later.
"""

import json
from datetime import date
from typing import Dict, List, Optional

import httpx

GRAPH = "https://graph.facebook.com/v25.0"
TRACK_FIELDS = "ad_id,spend,impressions,clicks,inline_link_clicks,actions,date_start"
ACCOUNT_FIELDS = "spend,impressions,inline_link_clicks,actions,date_start"
BATCH = 50
PAGE_LIMIT = 500
RETRYABLE_CODES = (4, 17, 613)


class MetaReadError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES


def _error_from(resp: httpx.Response) -> MetaReadError:
    try:
        body = resp.json()
    except Exception:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    code = err.get("code")
    if resp.status_code == 429 and code is None:
        code = 17
    return MetaReadError(err.get("message") or f"Meta HTTP {resp.status_code}",
                         code if isinstance(code, int) else None)


async def _get(client: httpx.AsyncClient, url: str, params: Optional[dict]) -> dict:
    try:
        resp = await client.get(url, params=params) if params is not None else await client.get(url)
    except httpx.HTTPError as e:
        raise MetaReadError(f"Meta request failed: {e}") from e
    if resp.status_code != 200:
        raise _error_from(resp)
    return resp.json()


async def _insights(client, ad_account_id: str, params: dict) -> List[Dict]:
    rows: List[Dict] = []
    url: Optional[str] = f"{GRAPH}/{ad_account_id}/insights"
    first = True
    while url:
        # params=None on later pages: `next` already carries token and cursor.
        data = await _get(client, url, params if first else None)
        first = False
        rows.extend(data.get("data", []))
        url = (data.get("paging") or {}).get("next")
    return rows


def _range(since: date, until: date) -> str:
    return json.dumps({"since": since.isoformat(), "until": until.isoformat()},
                      separators=(",", ":"))


async def fetch_tracked_ad_days(ad_account_id: str, token: str, ad_ids: List[str],
                                since: date, until: date) -> List[Dict]:
    """Daily ad-level rows for the given ads, one request per <= 50 ids."""
    rows: List[Dict] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for i in range(0, len(ad_ids), BATCH):
            chunk = ad_ids[i:i + BATCH]
            params = {
                "access_token": token,
                "level": "ad",
                "time_increment": "1",
                "time_range": _range(since, until),
                "fields": TRACK_FIELDS,
                "limit": PAGE_LIMIT,
                "filtering": json.dumps(
                    [{"field": "ad.id", "operator": "IN", "value": chunk}]),
            }
            rows.extend(await _insights(client, ad_account_id, params))
    return rows


async def fetch_account_daily(ad_account_id: str, token: str,
                              since: date, until: date) -> List[Dict]:
    """Account-level daily totals (for baselines)."""
    params = {
        "access_token": token,
        "level": "account",
        "time_increment": "1",
        "time_range": _range(since, until),
        "fields": ACCOUNT_FIELDS,
        "limit": PAGE_LIMIT,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await _insights(client, ad_account_id, params)


async def fetch_ad_status(ad_id: str, token: str) -> Optional[str]:
    """effective_status of an ad; None when Meta says it no longer exists (code 100)."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            data = await _get(client, f"{GRAPH}/{ad_id}",
                              {"access_token": token, "fields": "effective_status"})
        except MetaReadError as e:
            if e.code == 100:
                return None
            raise
    return data.get("effective_status")


# --- wizard reads ----------------------------------------------------------

ADSET_FIELDS = ("id,name,status,effective_status,campaign{id,name,objective},"
                "optimization_goal,promoted_object,destination_type")

_ACCEPTED_OBJECTIVES = {
    "lead_form": ("OUTCOME_LEADS",),
    "website": ("OUTCOME_LEADS", "OUTCOME_TRAFFIC", "OUTCOME_SALES", "OUTCOME_ENGAGEMENT"),
    "whatsapp": ("OUTCOME_ENGAGEMENT", "OUTCOME_SALES"),
}


_DESTINATION_TYPE = {"lead_form": "ON_AD", "whatsapp": "WHATSAPP"}


async def _list(url: str, params: dict) -> List[Dict]:
    rows: List[Dict] = []
    first = True
    async with httpx.AsyncClient(timeout=30.0) as client:
        while url:
            # params=None on later pages: `next` already carries token and cursor.
            data = await _get(client, url, params if first else None)
            first = False
            rows.extend(data.get("data", []))
            url = (data.get("paging") or {}).get("next")
    return rows


async def list_adsets(ad_account_id: str, token: str) -> List[Dict]:
    return await _list(f"{GRAPH}/{ad_account_id}/adsets",
                       {"access_token": token, "fields": ADSET_FIELDS, "limit": PAGE_LIMIT})


async def list_promote_pages(ad_account_id: str, token: str) -> List[Dict]:
    return await _list(f"{GRAPH}/{ad_account_id}/promote_pages",
                       {"access_token": token, "fields": "id,name", "limit": PAGE_LIMIT})


async def list_lead_forms(page_id: str, token: str) -> List[Dict]:
    """leadgen_forms needs a Page access token: fetch it with the user token first."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            page = await _get(client, f"{GRAPH}/{page_id}",
                              {"access_token": token, "fields": "access_token"})
        page_token = page.get("access_token")
        if not page_token:
            raise MetaReadError("Meta gave no Page access token for this Page")
        return await _list(f"{GRAPH}/{page_id}/leadgen_forms",
                           {"access_token": page_token, "fields": "id,name,status",
                            "limit": PAGE_LIMIT})
    except MetaReadError as e:
        if e.code == 190:
            # A Page-token problem is not a dead user connection: do not surface 190.
            raise MetaReadError(f"Could not read this Page's lead forms: {e}") from e
        raise


def adset_compatible(adset: dict, destination: str):
    """(ok, reason). The campaign objective decides which destinations an ad set can serve."""
    accepted = _ACCEPTED_OBJECTIVES.get(destination)
    if accepted is None:
        return False, f"Unknown destination '{destination}'"
    objective = (adset.get("campaign") or {}).get("objective")
    if objective in accepted:
        needed = _DESTINATION_TYPE.get(destination)
        dtype = adset.get("destination_type")
        if needed and dtype != needed:
            return False, (f"{destination.replace('_', ' ')} ads need an ad set with destination "
                           f"{needed} (this one is {dtype or 'unknown'})")
        return True, None
    shown = " or ".join(accepted)
    return False, f"{destination.replace('_', ' ')} ads need a campaign with objective {shown} (this one is {objective or 'unknown'})"
