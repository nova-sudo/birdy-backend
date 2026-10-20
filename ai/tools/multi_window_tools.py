"""
ai/tools/multi_window_tools.py
-------------------------------
Deterministic multi-window metrics comparison (e.g. "last 30/60/90 days").

Built after a production incident: BirdyAI was asked for spend, leads, won
opportunities, revenue, and cost-per-won-opportunity across the last 30, 60,
and 90 days. No cached preset covers 60 or 90 days (only 13 fixed presets
exist — see core/constants.py), so answering this correctly required the
model to compute 3 date ranges itself and chain up to 6+ live tool calls
(get_meta_insights_live + get_ghl_opp_stats_windowed, once per window). That
exceeded the per-turn tool-call budget, and rather than disclosing the gap,
the model fabricated numbers — including a duplicated 60d/90d figure and an
invented "campaigns were paused" explanation — when the user pushed back.

This tool removes the model from the date-math and multi-call-chaining path
entirely: given a list of day-window sizes, it fetches each data source ONCE
per group (daily-granularity Meta insights over the widest window, all GHL
opportunities once) and buckets every requested window from that single pull
in code — mirroring the "fetch once, derive every window" pattern already
used elsewhere in this codebase (services/ghl_service.py,
services/hp_service.py, ai/tools/ghl_tools.py::get_ghl_opp_stats_monthly).
Derived metrics (cost per won opportunity, ROAS, profit, CPL) are computed
here too, not left to the model's own arithmetic.

All windows end "today" and are cumulative by construction (a 90-day window
fully contains the 30-day window), so spend/leads/won/revenue are
mathematically guaranteed non-decreasing as the window widens. That
invariant is verified before returning — a violation can only come from a
real bug, and this is exactly the class of error that went unnoticed and
got fabricated over in the incident this tool exists to prevent.
"""

import asyncio
import logging
from datetime import date, timedelta

import httpx

from ai.tools.registry import registry
from ai.tools.derived_metrics import _safe_div
from ai.config import MAX_RESULT_ITEMS
from core.utils import get_result_value
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_campaign_scope import campaign_filter

logger = logging.getLogger(__name__)

META_API = "https://graph.facebook.com/v25.0"

NO_CAMPAIGNS_NOTE = (
    "No campaigns are selected for this client, so there is no Meta data to "
    "report. Tell the user to pick campaigns in client settings."
)

MAX_WINDOWS = 6  # bounds worst-case cost; 30/60/90/etc. never needs more than this


# ── Meta: fetch once at daily granularity, bucket per window in code ───────

async def _fetch_daily_insights(
    ad_account_id: str, access_token: str, since: str, until: str,
    campaign_ids: list[str] | None = None,
) -> dict:
    """
    ONE live call: account-level insights broken out per calendar day via
    Meta's time_increment=1, covering [since, until]. Mirrors
    ai/tools/meta_live_tools.py::_fetch_monthly_insights_live, generalized
    from a fixed calendar year to an arbitrary date range.
    """
    time_range = f'{{"since":"{since}","until":"{until}"}}'
    params = {
        "fields": "spend,impressions,clicks,reach,actions,results",
        "time_range": time_range,
        "time_increment": "1",
        "access_token": access_token,
        "limit": 500,
    }
    f = campaign_filter(campaign_ids, "campaign.id")
    if f is not None:
        params["filtering"] = f
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.get(f"{META_API}/{ad_account_id}/insights", params=params)
            if resp.status_code != 200:
                body = resp.text[:300]
                if resp.status_code == 429 or '"code":17' in body or '"code":4' in body:
                    return {"error": "Meta API rate limit reached. Try again in a few minutes."}
                if '"code":190' in body or '"code":200' in body:
                    return {"error": "Meta token expired or missing permissions. Please reconnect Meta."}
                return {"error": f"Meta API error ({resp.status_code}): {body[:200]}"}
            data = resp.json()
    except httpx.TimeoutException:
        return {"error": "Meta API request timed out."}
    except Exception as e:
        logger.error(f"Meta daily fetch error for {ad_account_id}: {e}", exc_info=True)
        return {"error": f"Meta API error: {str(e)[:200]}"}

    days = []
    for row in data.get("data", []):
        days.append({
            "date_start": row.get("date_start"),
            "spend": float(row.get("spend", 0) or 0),
            "impressions": int(row.get("impressions", 0) or 0),
            "clicks": int(row.get("clicks", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "results": get_result_value([row], "lead"),
        })
    return {"days": days}


def _bucket_meta_window(daily_rows: list[dict], window_start: str) -> dict:
    """Sum every daily row with date_start >= window_start. Pure, no I/O."""
    spend = impressions = clicks = 0.0
    reach = results = 0
    for row in daily_rows:
        if (row.get("date_start") or "") >= window_start:
            spend += row["spend"]
            impressions += row["impressions"]
            clicks += row["clicks"]
            reach = max(reach, row["reach"])  # reach doesn't sum across days (dedup'd audience)
            results += row["results"]
    return {
        "spend": round(spend, 2),
        "impressions": impressions,
        "clicks": clicks,
        "reach": reach,
        "leads": results,
    }


# ── GHL: fetch all opps once, bucket per window via the existing pure fn ───

async def _fetch_all_opps(user_id: str, mongo_client, location_id: str) -> tuple[bool, list, str | None]:
    from integrations.gohighlevel import ghl_integration, get_subaccount_tokens

    tokens = await get_subaccount_tokens(user_id, mongo_client)
    access_token = (tokens or {}).get(location_id, {}).get("access_token")
    if not access_token:
        return False, [], f"No GHL access token for location {location_id}"

    ok, opps = await ghl_integration.fetch_all_opportunities(location_id, access_token)
    if not ok:
        return False, [], "GHL API fetch failed — the tool could not load opportunities"
    return True, opps, None


# ── Derived metrics + monotonicity guard ────────────────────────────────────

def _derive(meta_bucket: dict, opp_stats: dict) -> dict:
    spend = meta_bucket["spend"]
    leads = meta_bucket["leads"]
    won = opp_stats.get("won", 0)
    revenue = opp_stats.get("won_revenue", 0.0)
    return {
        "cpl": _safe_div(spend, leads),
        "cost_per_won_opportunity": _safe_div(spend, won),
        "roas": _safe_div(revenue, spend),
        "profit": round(revenue - spend, 2),
    }


def _check_monotonic(windows: list[dict], group_name: str) -> str | None:
    """
    Windows are cumulative (all end today, only start date widens), so every
    metric below MUST be non-decreasing as `days` increases. A violation can
    only come from a real bucketing bug — surface it instead of letting a
    caller (model or human) discover the inconsistency the hard way, which is
    exactly what happened in the incident this tool exists to prevent.
    """
    fields = ("spend", "leads", "won_opps", "revenue")
    for i in range(1, len(windows)):
        prev, cur = windows[i - 1], windows[i]
        for f in fields:
            if cur[f] < prev[f] - 1e-6:  # small epsilon for float rounding
                return (
                    f"Data inconsistency for {group_name}: {f} for the "
                    f"{cur['days']}-day window ({cur[f]}) is LESS than the "
                    f"{prev['days']}-day window ({prev[f]}). Since both windows "
                    f"end today, the wider window can never be smaller — this "
                    f"indicates a real bug in the underlying data, not a "
                    f"reporting choice. Do not paper over this or invent an "
                    f"explanation; tell the user the numbers look inconsistent "
                    f"and that it's been flagged for investigation."
                )
    return None


# ── Tool executor ────────────────────────────────────────────────────────────

async def get_metrics_by_day_windows(db, user_id, windows_days, group_ids=None, mongo_client=None):
    """
    Deterministically fetch spend, leads, GHL opportunity stats, revenue, and
    derived metrics for MULTIPLE day-count windows ending today (e.g. last
    30/60/90 days), one data pull per source per group regardless of how many
    windows are requested.
    """
    if not isinstance(windows_days, list) or not windows_days:
        return {"error": "windows_days must be a non-empty list of positive integers, e.g. [30, 60, 90]"}
    try:
        windows_days = sorted({int(d) for d in windows_days if int(d) > 0})
    except (TypeError, ValueError):
        return {"error": "windows_days must contain only positive integers"}
    if not windows_days:
        return {"error": "windows_days must contain at least one positive integer"}
    if len(windows_days) > MAX_WINDOWS:
        return {"error": f"Too many windows requested ({len(windows_days)}); max is {MAX_WINDOWS}"}

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}
    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1,
            "ghl_location_id": 1, "meta_campaign_ids": 1, "_id": 0,
        },
    ).to_list(None)

    if not groups:
        return {"error": "No matching client groups found."}

    today = date.today()
    today_iso = today.isoformat()
    max_days = max(windows_days)
    since_iso = (today - timedelta(days=max_days)).isoformat()

    meta_token = None
    if any(g.get("meta_ad_account_id") for g in groups):
        token_doc = await get_facebook_token(user_id, db.client)
        meta_token = (token_doc or {}).get("access_token")

    async def _process_group(g):
        group_result = {
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "currency": g.get("ad_account_currency"),
            "meta_connected": bool(g.get("meta_ad_account_id")),
            "ghl_connected": bool(g.get("ghl_location_id")),
        }

        # -- Meta: one daily fetch covering the widest window --
        daily_rows = []
        campaign_ids = g.get("meta_campaign_ids")
        if g.get("meta_ad_account_id") and campaign_ids is not None and len(campaign_ids) == 0:
            group_result["meta_note"] = NO_CAMPAIGNS_NOTE
        elif g.get("meta_ad_account_id") and meta_token:
            meta_data = await _fetch_daily_insights(
                g["meta_ad_account_id"], meta_token, since_iso, today_iso, campaign_ids
            )
            if "error" in meta_data:
                group_result["meta_error"] = meta_data["error"]
            else:
                daily_rows = meta_data["days"]
        elif g.get("meta_ad_account_id") and not meta_token:
            group_result["meta_error"] = "No Meta token found. Please connect your Meta account first."

        # -- GHL: fetch every opportunity once --
        opps = []
        if g.get("ghl_location_id") and mongo_client is not None:
            ok, fetched_opps, err = await _fetch_all_opps(user_id, mongo_client, g["ghl_location_id"])
            if not ok:
                group_result["ghl_error"] = err
            else:
                opps = fetched_opps
        elif g.get("ghl_location_id") and mongo_client is None:
            group_result["ghl_error"] = "GHL data unavailable in this context."

        from integrations.gohighlevel import compute_opp_stats

        windows = []
        for days in windows_days:  # ascending — required for the monotonicity check below
            window_start = (today - timedelta(days=days)).isoformat()
            meta_bucket = _bucket_meta_window(daily_rows, window_start)
            opp_stats = compute_opp_stats(opps, window_start, today_iso) if opps or g.get("ghl_location_id") else {}
            won = opp_stats.get("won", 0)
            revenue = round(opp_stats.get("won_revenue", 0.0), 2)

            row = {
                "days": days,
                "start_date": window_start,
                "end_date": today_iso,
                "spend": meta_bucket["spend"],
                "impressions": meta_bucket["impressions"],
                "clicks": meta_bucket["clicks"],
                "leads": meta_bucket["leads"],
                "won_opps": won,
                "lost_opps": opp_stats.get("lost", 0),
                "open_opps": opp_stats.get("open", 0),
                "abandoned_opps": opp_stats.get("abandoned", 0),
                "total_opportunities": opp_stats.get("total_opportunities", 0),
                "revenue": revenue,
                **_derive(meta_bucket, opp_stats),
            }
            windows.append(row)

        warning = _check_monotonic(windows, g.get("name", g.get("id")))
        if warning:
            group_result["data_warning"] = warning
            logger.error(f"Non-monotonic multi-window result for group {g.get('id')}: {warning}")

        group_result["windows"] = windows
        return group_result

    results = await asyncio.gather(*(_process_group(g) for g in groups))
    return {"groups": results[:MAX_RESULT_ITEMS], "total_groups": len(results), "windows_days": windows_days}


# ── Day-by-day series (for charts) ──────────────────────────────────────────

MAX_SERIES_DAYS = 90


def _daily_series(daily_rows: list[dict], since_iso: str, until_iso: str) -> list[dict]:
    """One row per calendar day in [since, until], zero-filled, with CPL and
    CTR derived here rather than by the model. Pure, no I/O."""
    by_day: dict[str, dict] = {}
    for row in daily_rows:
        d = row.get("date_start")
        if not d:
            continue
        acc = by_day.setdefault(d, {"spend": 0.0, "impressions": 0, "clicks": 0, "leads": 0})
        acc["spend"] += row.get("spend", 0) or 0
        acc["impressions"] += row.get("impressions", 0) or 0
        acc["clicks"] += row.get("clicks", 0) or 0
        acc["leads"] += row.get("results", row.get("leads", 0)) or 0
    out = []
    day = date.fromisoformat(since_iso)
    end = date.fromisoformat(until_iso)
    while day <= end:
        iso = day.isoformat()
        acc = by_day.get(iso, {"spend": 0.0, "impressions": 0, "clicks": 0, "leads": 0})
        spend = round(acc["spend"], 2)
        out.append({
            "date": iso,
            "spend": spend,
            "leads": acc["leads"],
            "clicks": acc["clicks"],
            "impressions": acc["impressions"],
            "cpl": _safe_div(spend, acc["leads"]),
            "ctr": round(acc["clicks"] / acc["impressions"] * 100, 2) if acc["impressions"] else None,
        })
        day += timedelta(days=1)
    return out


def _totals(days: list[dict]) -> dict:
    spend = round(sum(d["spend"] for d in days), 2)
    leads = sum(d["leads"] for d in days)
    clicks = sum(d["clicks"] for d in days)
    impressions = sum(d["impressions"] for d in days)
    return {
        "from": days[0]["date"] if days else None,
        "to": days[-1]["date"] if days else None,
        "spend": spend, "leads": leads, "clicks": clicks, "impressions": impressions,
        "cpl": _safe_div(spend, leads),
        "ctr": round(clicks / impressions * 100, 2) if impressions else None,
    }


def _pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return round((new - old) / old * 100, 1)


def _summarize(days: list[dict], previous_days: list[dict]) -> dict:
    """Totals the model would otherwise add up itself — and get wrong: summing
    14 daily rows in its head is where KPI cards went off by 10-15%. Compared
    with the same number of days just before, so "last 7 days" always comes
    with "vs the previous 7 days" and never with a slice of its own range."""
    total, previous = _totals(days), _totals(previous_days)
    return {
        "total": total,
        "previous_period": previous,
        "change_pct": {k: _pct_change(total[k], previous[k]) for k in ("spend", "leads", "cpl", "ctr")},
    }


def _split_series(rows: list[dict], prev_since_iso: str, since_iso: str, until_iso: str):
    """The requested range and the equal range before it, from one fetch."""
    full = _daily_series(rows, prev_since_iso, until_iso)
    i = next(n for n, d in enumerate(full) if d["date"] == since_iso)
    return full[i:], full[:i]


async def get_daily_metrics(db, user_id, days=30, group_ids=None, mongo_client=None, **kwargs):
    """
    Meta spend, leads, clicks, impressions, CPL and CTR per calendar day for the
    last `days` days, per client and summed across them. The one tool that
    returns a time series, so the chat can chart a trend from real numbers
    instead of window totals.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"error": "days must be a whole number of days, e.g. 30"}
    if days < 2 or days > MAX_SERIES_DAYS:
        return {"error": f"days must be between 2 and {MAX_SERIES_DAYS}"}

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}
    groups = await db["client_groups"].find(
        query,
        {"id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1, "meta_campaign_ids": 1, "_id": 0},
    ).to_list(None)
    if not groups:
        return {"error": "No matching client groups found."}

    today = date.today()
    until_iso = (today - timedelta(days=1)).isoformat()  # today is still filling in
    since_iso = (today - timedelta(days=days)).isoformat()
    prev_since_iso = (today - timedelta(days=2 * days)).isoformat()  # for summary.previous_period

    meta_token = None
    if any(g.get("meta_ad_account_id") for g in groups):
        token_doc = await get_facebook_token(user_id, db.client)
        meta_token = (token_doc or {}).get("access_token")

    async def _one(g):
        res = {"group_id": g.get("id"), "group_name": g.get("name"), "currency": g.get("ad_account_currency")}
        campaign_ids = g.get("meta_campaign_ids")
        if not g.get("meta_ad_account_id"):
            res["meta_error"] = "Meta is not connected for this client."
            return res, []
        if campaign_ids is not None and len(campaign_ids) == 0:
            res["meta_note"] = NO_CAMPAIGNS_NOTE
            return res, []
        if not meta_token:
            res["meta_error"] = "No Meta token found. Please connect your Meta account first."
            return res, []
        data = await _fetch_daily_insights(g["meta_ad_account_id"], meta_token, prev_since_iso, until_iso, campaign_ids)
        if "error" in data:
            res["meta_error"] = data["error"]
            return res, []
        res["days"], previous = _split_series(data["days"], prev_since_iso, since_iso, until_iso)
        res["summary"] = _summarize(res["days"], previous)
        return res, data["days"]

    results = await asyncio.gather(*(_one(g) for g in groups))
    per_group = [r for r, _ in results]
    all_rows = [row for _, rows in results for row in rows]
    currencies = {r.get("currency") for r in per_group if r.get("days")}
    out = {
        "since": since_iso,
        "until": until_iso,
        "groups": per_group[:MAX_RESULT_ITEMS],
        "total_groups": len(per_group),
    }
    # A combined series only when every client with data shares one currency.
    if len([r for r in per_group if r.get("days")]) > 1 and len(currencies) == 1:
        combined, previous = _split_series(all_rows, prev_since_iso, since_iso, until_iso)
        out["combined"] = {"currency": next(iter(currencies)), "days": combined, "summary": _summarize(combined, previous)}
    return out


# ── Registration ─────────────────────────────────────────────────────────────

def register_multi_window_tools():
    registry.register(
        name="get_metrics_by_day_windows",
        description=(
            "Fetch spend, leads, GHL opportunity stats (won/lost/open), revenue, and derived metrics "
            "(cost per won opportunity, ROAS, profit, CPL) across MULTIPLE day-count windows ending today "
            "— e.g. the last 30, 60, and 90 days — in a single deterministic call. "
            "ALWAYS use this instead of calling get_meta_insights_live / get_ghl_opp_stats_windowed "
            "multiple times when the user asks to compare 2+ day-count periods for the same client(s) "
            "(e.g. 'last 30/60/90 days', 'compare the last 7, 14, and 30 days'). "
            "All date math and all derived-metric arithmetic (cost per won opp, ROAS, profit) is computed "
            "here — do not recompute them yourself, just report the values returned. "
            "All windows end today and are cumulative (a 90-day window fully contains the 30-day window), "
            "so spend/leads/won_opps/revenue are always non-decreasing as the window widens. If a result "
            "includes a 'data_warning' field, tell the user the numbers look inconsistent and that it's "
            "been flagged — do NOT invent an explanation for it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "windows_days": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Day-window sizes to compare, e.g. [30, 60, 90] for 'last 30/60/90 days'. Max 6 windows.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to specific group IDs (resolve names via get_client_groups first). Omit for all groups.",
                },
            },
            "required": ["windows_days"],
        },
        executor=get_metrics_by_day_windows,
    )
    registry.register(
        name="get_daily_metrics",
        description=(
            "Meta ad spend, leads, clicks, impressions, CPL and CTR per calendar day for the last N days "
            "(up to 90, ending yesterday), per client plus a combined series across clients. Use this for "
            "any trend or over-time chart (e.g. 'ad spend vs CPL over the last 30 days'), then draw it with "
            "render_ui. Each series comes with a `summary`: `total` for the N days, `previous_period` for the N days "
            "before them, and `change_pct` between the two. So 'last 7 days vs the previous 7' is days=7: use "
            "summary.total, summary.previous_period and summary.change_pct, and label the delta 'vs previous 7 days'. "
            "Use those totals; don't add up the daily rows yourself. "
            "A day with spend but no leads has cpl null: leave it null, don't fill it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days back, 2-90. Default 30."},
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to specific group IDs (resolve names via get_client_groups first). Omit for all groups.",
                },
            },
            "required": [],
        },
        executor=get_daily_metrics,
    )
