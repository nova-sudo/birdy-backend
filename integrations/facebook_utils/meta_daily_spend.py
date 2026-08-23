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

    [{"date": "2026-08-16", "spend": 718.52, "impressions": 41233, "clicks": 812,
      "currency": "GBP", "source_currency": "USD", "fx_rate": 0.743}, ...]

sorted by date, one entry per day the account spent anything.

`impressions` and `clicks` are present only when Meta reported them. Rows
written before they were requested carry neither, and the Impressions chart
falls back to scaling the spend curve onto the period's real total for those —
labelled as an estimate rather than passed off as measured.

`spend` is denominated in `currency`, matching what `facebook_cache` holds, so
the chart and the preset totals above it are in the same money. Rows used to be
written in the ad account's own currency while `facebook_cache` was converted
(services/meta_service.py), which quietly added USD to GBP totals under a single
symbol. Every row now carries its denomination, the currency Meta reported, and
the rate applied, so a row's provenance is legible and a later backfill can tell
converted rows from untouched ones.

A row is only stamped as converted once the conversion actually succeeded: if
the rate lookup fails the row keeps its source currency and says so, which the
chart can exclude, rather than being silently mislabelled.

One caveat worth knowing: the rate is the one current at write time, applied to
the whole window. `facebook_cache` does the same, so the two agree — but neither
reconstructs the rate that held on each historical day.
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


async def _resolve_currencies(
        db,
        group_id: str,
        user_currency: Optional[str],
        ad_account_currency: Optional[str],
) -> tuple:
    """Work out (target, source) currencies for one group's spend rows.

    Callers that already hold both (the refresh manager reads them off the job)
    pass them in. Everyone else — the backfill script, ad-hoc runs — gets them
    looked up off the group, so the signature stays optional and no caller is
    forced to thread currency through just to cache spend.
    """
    if user_currency and ad_account_currency:
        return user_currency, ad_account_currency

    group = await db["client_groups"].find_one(
        {"id": group_id}, {"user_id": 1, "ad_account_currency": 1}
    ) or {}

    source = ad_account_currency or group.get("ad_account_currency")
    target = user_currency
    if not target and group.get("user_id"):
        try:
            from utils.currency_exchange import CurrencyService
            target = await CurrencyService.get_user_currency(group["user_id"])
        except (ValueError, RuntimeError) as e:
            # No default currency set, or the lookup failed. Leaving target
            # unset means _convert_rows stamps the source currency and skips
            # conversion, which is the honest outcome.
            logger.warning(
                "Daily spend for %s: could not resolve user currency (%s)", group_id, e
            )
    return target, source


def _convert_rows(rows: List[Dict], source: Optional[str], target: Optional[str]) -> List[Dict]:
    """Denominate every row, converting when source and target differ.

    Returns rows unchanged when the source currency is unknown — stamping a
    denomination we cannot justify would be worse than stamping none.
    """
    if not source:
        logger.warning("Daily spend: unknown ad account currency, rows left unstamped")
        return rows

    target = target or source
    rate = 1.0
    if target != source:
        try:
            from utils.currency_exchange import CurrencyService
            rate = CurrencyService.get_rate(source, target)
        except ValueError as e:
            # Fall back to the source currency rather than writing a converted
            # figure we could not compute. The row stays truthful and the chart
            # can see it is not in the account's display currency.
            logger.warning(
                "Daily spend: no %s->%s rate (%s); rows stay in %s", source, target, e, source
            )
            target = source
            rate = 1.0

    if rate == 1.0:
        return [
            {**r, "currency": target, "source_currency": source, "fx_rate": 1.0}
            for r in rows
        ]

    from utils.currency_exchange import CurrencyService
    return [
        {
            **r,
            # Convert through the same call meta_service uses, so the daily
            # rows and the preset totals round identically.
            "spend": CurrencyService.convert(r["spend"], source, target),
            "currency": target,
            "source_currency": source,
            "fx_rate": rate,
        }
        for r in rows
    ]


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
        # impressions and clicks ride along on the same row Meta already
        # returns, at no extra request. Without them the Impressions chart had
        # nothing measured to draw: it fell back to scaling the spend curve
        # onto the period's real impression total, so the shape was inferred
        # from spend while the headline was a measurement. That fallback stays
        # for dates before this shipped, but it should not be the only path.
        "fields": "spend,impressions,clicks,date_start",
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
                if not day:
                    continue
                entry = {"date": day, "spend": _num(row.get("spend"))}
                # Only carried when Meta actually reported them. A missing
                # figure is a gap, not a day that served to nobody, and the
                # chart distinguishes the two — it plots only the days whose
                # row has an impression count, rather than drawing a zero.
                if row.get("impressions") is not None:
                    entry["impressions"] = int(_num(row.get("impressions")))
                if row.get("clicks") is not None:
                    entry["clicks"] = int(_num(row.get("clicks")))
                rows.append(entry)

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
        user_currency: Optional[str] = None,
        ad_account_currency: Optional[str] = None,
) -> int:
    """
    Refresh one group's daily spend history and store it on the group.

    Rewrites the whole window rather than appending: Meta restates recent days
    as attribution settles, so the last few rows are not final when first seen.

    Rows are denominated in `user_currency` so they match `facebook_cache`.
    Both currencies are optional: pass them if you already hold them (the
    refresh manager does), otherwise they are read off the group.

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

    target, source = await _resolve_currencies(
        db, group_id, user_currency, ad_account_currency
    )
    rows = _convert_rows(rows, source, target)

    await db["client_groups"].update_one(
        {"id": group_id},
        {"$set": {
            "meta_daily_spend": rows,
            "meta_daily_spend_updated_at": datetime.utcnow().isoformat(),
        }},
    )

    total = sum(r["spend"] for r in rows)
    denom = rows[0].get("currency") if rows else (target or source or "?")
    logger.info(
        "Cached daily spend for %s: %d days, %.2f %s total",
        group_id, len(rows), total, denom,
    )
    return len(rows)
