"""
ai/agents/meta_state.py
-----------------------
Cache of Meta ad / ad set state (status, budgets, created_time) plus the
date an ad was paused, which Meta does not expose. One paged Graph call per
object type per refresh.
"""

import logging
from datetime import date, datetime

import httpx

from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v25.0"
AD_STATE = "meta_ad_state"
ADSET_STATE = "meta_adset_state"
_AD_FIELDS = "id,name,adset_id,campaign_id,effective_status,created_time"
_ADSET_FIELDS = "id,name,campaign_id,effective_status,daily_budget,lifetime_budget,campaign{effective_status}"


class MetaTokenError(Exception):
    """The user's Meta token is missing, expired or rejected."""


def _is_active(status) -> bool:
    return str(status or "").strip().upper() == "ACTIVE"


def _budget(v) -> float:
    """Graph returns budgets as minor-unit strings (pence/cents)."""
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return 0.0


def _parse_time(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.strptime(str(v), "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
    except ValueError:
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None


async def _fetch_all(client: httpx.AsyncClient, path: str, fields: str, token: str) -> list[dict]:
    out: list[dict] = []
    url, params = f"{_GRAPH}/{path}", {"fields": fields, "limit": 200, "access_token": token}
    while url:
        resp = await client.get(url, params=params)
        if resp.status_code == 401:
            raise MetaTokenError("Meta rejected the access token")
        try:
            body = resp.json()
        except Exception:
            body = {}
        if resp.status_code != 200:
            err = body.get("error") or {}
            if err.get("code") in (190, 102):  # expired/invalid token, session invalid
                raise MetaTokenError(err.get("message") or "Meta token invalid")
            raise RuntimeError(f"Meta Graph error on {path}: {err.get('message') or resp.status_code}")
        out.extend(body.get("data") or [])
        url, params = (body.get("paging") or {}).get("next"), None
    return out


async def refresh_meta_state(db, mongo_client, user_id: str, client_group_id: str, ad_account_id: str,
                             *, today: date) -> dict:
    token_data = await get_facebook_token(user_id, mongo_client)
    token = (token_data or {}).get("access_token")
    if not token:
        raise MetaTokenError("No valid Facebook token found")

    acct = ad_account_id if str(ad_account_id).startswith("act_") else f"act_{ad_account_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        ads = await _fetch_all(client, f"{acct}/ads", _AD_FIELDS, token)
        adsets = await _fetch_all(client, f"{acct}/adsets", _ADSET_FIELDS, token)

    now = datetime.utcnow()
    for ad in ads:
        key = {"client_group_id": client_group_id, "ad_id": ad["id"]}
        prev = await db[AD_STATE].find_one(key) or {}
        status = ad.get("effective_status") or ""
        paused_at = prev.get("paused_at")
        if _is_active(status):
            paused_at = None
        elif prev and _is_active(prev.get("effective_status")):
            paused_at = today.isoformat()  # first transition to non-active
        await db[AD_STATE].update_one(key, {"$set": {
            "user_id": user_id,
            "ad_account_id": ad_account_id,
            "adset_id": ad.get("adset_id"),
            "campaign_id": ad.get("campaign_id"),
            "name": ad.get("name") or "",
            "effective_status": status,
            "created_time": _parse_time(ad.get("created_time")),
            "paused_at": paused_at,
            "updated_at": now,
        }}, upsert=True)

    for s in adsets:
        await db[ADSET_STATE].update_one(
            {"client_group_id": client_group_id, "adset_id": s["id"]},
            {"$set": {
                "user_id": user_id,
                "ad_account_id": ad_account_id,
                "campaign_id": s.get("campaign_id"),
                "name": s.get("name") or "",
                "effective_status": s.get("effective_status") or "",
                "campaign_status": (s.get("campaign") or {}).get("effective_status") or "",
                "daily_budget": _budget(s.get("daily_budget")),
                "lifetime_budget": _budget(s.get("lifetime_budget")),
                "updated_at": now,
            }},
            upsert=True,
        )
    return {"ads": len(ads), "adsets": len(adsets)}


async def backfill_paused_at(db, client_group_id: str) -> int:
    """Paused ads with no paused_at: the day after their last day with spend > 0. Returns count set."""
    set_count = 0
    q = {"client_group_id": client_group_id, "paused_at": None,
         "effective_status": {"$nin": ["ACTIVE", ""]}}
    async for ad in db[AD_STATE].find(q):
        last = None
        async for row in db["facebook_ad_insights"].find(
                {"client_group_id": client_group_id, "ad_id": ad["ad_id"]}):
            try:
                spend = float((row.get("insight_data") or {}).get("spend") or 0)
            except (TypeError, ValueError):
                continue
            day = str(row.get("date_start") or "")[:10]
            if spend > 0 and day and (last is None or day > last):
                last = day
        if last:
            paused = date.fromisoformat(last).toordinal() + 1
            await db[AD_STATE].update_one(
                {"_id": ad["_id"]}, {"$set": {"paused_at": date.fromordinal(paused).isoformat()}})
            set_count += 1
    return set_count
