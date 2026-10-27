"""
Daily ad-level Meta insights for the Media Buying Agent's ad history.

One row per (ad, day) with spend > 0, fetched from `/{act}/insights` at
level=ad, stored in `facebook_ad_insights` in the shape
ai.agents.snapshot.load_ad_days reads. Spend stays in the ad account's own
currency. Meta omits `actions` on a day with no leads, so a stored row with an
empty `actions` list is a real zero-lead day.
"""

import json
import logging
from datetime import date, datetime
from typing import Dict, List, Optional

import httpx
from pymongo import UpdateOne

from integrations.facebook_utils._api_helpers import api_get_with_backoff
from services.meta_campaign_scope import campaign_filter

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v25.0"
PAGE_LIMIT = 500
COLLECTION = "facebook_ad_insights"
SOURCE = "agent_history"
FIELDS = "ad_id,ad_name,adset_id,campaign_id,campaign_name,spend,actions,date_start"
# Meta error codes for "no permission": 200 (permissions error), 10 (app lacks permission).
PERMISSION_CODES = (200, 10)


class MetaPermissionError(Exception):
    """Meta refused the read: the token has no access to this ad account."""


async def fetch_ad_daily(
        ad_account_id: str,
        access_token: str,
        since: date,
        until: date,
        campaign_ids: Optional[List[str]] = None,
) -> Optional[List[Dict]]:
    """
    Raw Meta rows (one per ad per day with spend) over [since, until].

    Returns None on failure, distinct from [] (no spend in the window); callers
    must not treat a failure as an empty history. `campaign_ids` scopes a group
    on a shared account: None = whole account, [] = nothing (no request).
    Raises MetaPermissionError when Meta answers with code 200 or 10.
    """
    if campaign_ids is not None and len(campaign_ids) == 0:
        return []

    filters = [{"field": "spend", "operator": "GREATER_THAN", "value": 0}]
    scope = campaign_filter(campaign_ids)
    if scope:
        filters += json.loads(scope)
    params = {
        "access_token": access_token,
        "level": "ad",
        "time_increment": "1",
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()},
                                 separators=(",", ":")),
        "fields": FIELDS,
        "limit": PAGE_LIMIT,
        "filtering": json.dumps(filters),
    }
    url = f"{GRAPH}/{ad_account_id}/insights"

    rows: List[Dict] = []
    next_url = None
    page = 0
    ok = True
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            # params=None on later pages: `next` already carries token and cursor.
            ok, data = await api_get_with_backoff(
                client,
                url if page == 1 else next_url,
                params if page == 1 else None,
                return_error=True,
            )
            if not ok:
                err = (data or {}).get("error") or {}
                if err.get("code") in PERMISSION_CODES:
                    raise MetaPermissionError(err.get("message") or "Meta permission error")
                break
            if not data:
                break
            rows.extend(r for r in data.get("data", []) if r.get("ad_id") and r.get("date_start"))
            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break

    if not ok:
        # A later page failing leaves a partial window; report failure, not a short list.
        logger.warning("Ad daily fetch failed for %s", ad_account_id)
        return None
    return rows


async def store_ad_daily(db, *, user_id: str, client_group_id: str,
                         ad_account_id: str, rows: List[Dict]) -> int:
    """Upsert rows keyed (user, group, ad, day); returns the number written."""
    if not rows:
        return 0
    now = datetime.utcnow()
    ops = []
    for row in rows:
        key = {"user_id": user_id, "client_group_id": client_group_id,
               "ad_id": row["ad_id"], "date_start": row["date_start"]}
        ops.append(UpdateOne(key, {"$set": {
            "ad_account_id": ad_account_id,
            "adset_id": row.get("adset_id"),
            "adset_name": row.get("adset_name"),
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "ad_name": row.get("ad_name"),
            "date_stop": row["date_start"],
            "insight_data": {"spend": row["spend"], "actions": row.get("actions", [])},
            "source": SOURCE,
            "updated_at": now,
        }}, upsert=True))
    await db[COLLECTION].bulk_write(ops, ordered=False)
    return len(ops)
