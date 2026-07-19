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

logger = logging.getLogger(__name__)

META_API = "https://graph.facebook.com/v25.0"

MAX_WINDOWS = 6  # bounds worst-case cost; 30/60/90/etc. never needs more than this


# ── Meta: fetch once at daily granularity, bucket per window in code ───────

async def _fetch_daily_insights(ad_account_id: str, access_token: str, since: str, until: str) -> dict:
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
            "ghl_location_id": 1, "_id": 0,
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
        if g.get("meta_ad_account_id") and meta_token:
            meta_data = await _fetch_daily_insights(g["meta_ad_account_id"], meta_token, since_iso, today_iso)
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
