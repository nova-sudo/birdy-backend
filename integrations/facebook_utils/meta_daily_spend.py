"""
Account-level daily ad spend.

The dashboard's Ad spend chart had no daily figures to draw. Meta returns spend
already totalled for a date preset, so the curve was the preset total spread
across days *in proportion to that day's lead count* — which assumes CPL held
steady, and silently inherits any gap in lead capture. When lead ingestion was
running at 37%, the all-time curve overstated a day's spend by ~3.5x: £2,554
drawn against £718 actually spent.

Meta will give real daily rows for `time_increment=1`, so this asks for them.

Scope is deliberately narrow. The campaign / adset / ad insight collections
also carry per-day rows and are fetched the same way, but they are read by the
AI media-buying tools and by /api/facebook-* endpoints, cost three API calls
per group per cycle, and grow by one row per ad per day. This module answers
only the chart's question — how much did this account spend on this day — as a
single call per account and one small row per account per day.

Stored on the client group as `meta_daily_spend`:

    [{"date": "2026-08-16", "spend": 718.52}, ...]

sorted by date, one entry per day the account spent anything.
"""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

import httpx

from integrations.facebook_utils._api_helpers import api_get_with_backoff

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com/v25.0"

# Meta caps time_increment=1 responses; 500 days per page is comfortably more
# than any window we ask for.
PAGE_LIMIT = 500


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def fetch_account_daily_spend(
        ad_account_id: str,
        access_token: str,
        since: str,
        until: str,
) -> Optional[List[Dict]]:
    """
    One row per day for one ad account, over [since, until] (yyyy-mm-dd).

    Returns None on failure — distinct from [], which legitimately means "the
    account spent nothing in this window". Callers must not treat a failure as
    an empty history and overwrite good data with it.
    """
    rows: List[Dict] = []
    url = f"{GRAPH}/{ad_account_id}/insights"
    params = {
        "access_token": access_token,
        # No `level` — account-level totals are what the portfolio chart sums.
        "time_increment": "1",
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "fields": "spend,date_start",
        "limit": PAGE_LIMIT,
    }

    page = 0
    next_url = None
    ok = False

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            # params=None on later pages: Meta embeds the token and cursor in
            # the `next` URL, and httpx builds URL(url, params=...) which
            # REPLACES the query string — an empty dict would strip both.
            ok, data = await api_get_with_backoff(
                client,
                url if page == 1 else next_url,
                params if page == 1 else None,
            )
            if not ok or not data:
                break

            for row in data.get("data", []):
                day = row.get("date_start")
                if day:
                    rows.append({"date": day, "spend": _num(row.get("spend"))})

            next_url = (data.get("paging") or {}).get("next")
            if not next_url:
                break

    if not ok and not rows:
        logger.warning("Daily spend fetch failed for %s", ad_account_id)
        return None

    rows.sort(key=lambda r: r["date"])
    return rows


async def cache_account_daily_spend(
        group_id: str,
        ad_account_id: str,
        access_token: str,
        mongo_client,
        days: int = 400,
) -> int:
    """
    Refresh one group's daily spend history and store it on the group.

    Rewrites the whole window rather than appending: Meta restates recent days
    as attribution settles, so the last few rows are not final when first seen.

    Returns the number of days stored, or 0 if nothing was written. A failed
    fetch leaves the existing cache untouched.
    """
    until = date.today()
    since = until - timedelta(days=days)

    rows = await fetch_account_daily_spend(
        ad_account_id, access_token, since.isoformat(), until.isoformat()
    )
    if rows is None:
        return 0

    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    await db["client_groups"].update_one(
        {"id": group_id},
        {"$set": {
            "meta_daily_spend": rows,
            "meta_daily_spend_updated_at": datetime.utcnow().isoformat(),
        }},
    )

    total = sum(r["spend"] for r in rows)
    logger.info(
        "Cached daily spend for %s: %d days, %.2f total", group_id, len(rows), total
    )
    return len(rows)
