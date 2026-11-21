"""
ai/tools/hp_tools.py
--------------------
HotProspector (call center) chat tool. Reads the per-preset call stats that
services/hp_service.py caches onto each client_group under
`hotprospector_call_cache.{preset}` (same cache-first pattern as the Meta tools
in meta_tools.py — no live HotProspector call here).

Exposes `get_call_center_stats`, which closes the lead-quality funnel past GHL:
it lets the agent see whether an ad's leads are actually reachable (connect rate)
and worked (coverage), so a cheap CPL with a low connect rate is exposed as a
zombie-lead source rather than a win.
"""

from datetime import date, timedelta

from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from core.constants import META_CACHE_PRESETS

MAX_TREND_DAYS = 90
_SUMMED = ("calls", "inbound", "outbound", "answered", "transfers")


async def get_call_center_stats(db, user_id, preset="last_7d", group_ids=None, **_):
    """Per-client HotProspector call stats for a date-window preset, read from the
    `hotprospector_call_cache` on client_groups. Adds derived connect_rate
    (answered / total calls) and leads_called_rate (leads worked / total leads)."""
    if preset not in META_CACHE_PRESETS:
        # An unknown window used to come back empty, which read as "no data" (e.g. "last_week").
        return {"error": f"Unknown preset {preset!r}. Use one of: {', '.join(META_CACHE_PRESETS)}. "
                         "To compare a period with the one before it (this week against last week), "
                         "use get_call_trend instead."}
    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {"id": 1, "name": 1, f"hotprospector_call_cache.{preset}": 1},
    ).to_list(None)

    results = []
    for g in groups:
        stats = (g.get("hotprospector_call_cache") or {}).get(preset)
        if not stats:
            # No HotProspector data cached for this client/preset — skip rather
            # than emit misleading zeros.
            continue

        total_calls = stats.get("total_calls", 0) or 0
        answered = stats.get("answered_calls", 0) or 0
        total_leads = stats.get("total_leads", 0) or 0
        leads_with_calls = stats.get("leads_with_calls", 0) or 0

        # The cache holds one lead count per client, all time, under every preset. Dividing a window's
        # called leads by it gave a coverage rate that meant nothing (47 called this week / 915 ever),
        # so the rate is only given where both sides cover the same period.
        all_time = preset == "maximum"
        results.append({
            "client_group_id": g.get("id"),
            "client_group_name": g.get("name"),
            "preset": preset,
            "total_calls": total_calls,
            "inbound_count": stats.get("inbound_count", 0),
            "outbound_count": stats.get("outbound_count", 0),
            "transfers": stats.get("transfers", 0),
            "answered_calls": answered,
            "total_talk_min": stats.get("total_talk_min", 0),
            "total_leads_all_time": total_leads,
            "leads_with_calls": leads_with_calls,
            # % of placed calls with talk time > 0.
            "connect_rate": round(answered / total_calls * 100, 1) if total_calls else 0.0,
            # % of leads that got >= 1 call — call-center coverage. Low coverage is
            # an operations gap, not a lead-quality problem.
            "leads_called_rate": (round(leads_with_calls / total_leads * 100, 1) if total_leads else 0.0)
                                 if all_time else None,
        })

    results.sort(key=lambda r: r["total_calls"], reverse=True)  # the cut keeps the busiest clients
    return {"call_center": results[:MAX_RESULT_ITEMS], "total": len(results),
            "shown": min(len(results), MAX_RESULT_ITEMS), "preset": preset,
            "note": "total_leads_all_time is every lead the client has in HotProspector, not the leads of "
                    "this period; leads_called_rate is given for preset=maximum only."}


def _window_totals(rows, since: str, until: str) -> dict:
    out = {k: 0 for k in _SUMMED}
    talk = 0.0
    for r in rows:
        day = r.get("date") or ""
        if since <= day <= until:
            for k in _SUMMED:
                out[k] += r.get(k) or 0
            talk += r.get("talk_time") or r.get("talk_min") or 0
    out["talk_min"] = round(talk, 1)
    out["connect_rate"] = round(out["answered"] / out["calls"] * 100, 1) if out["calls"] else None
    return out


def _change(now, before):
    if not before or now is None:
        return None
    return round((now - before) / before * 100, 1)


MAX_TREND_PERIODS = 12
TREND_CLIENTS = 10


async def get_call_trend(db, user_id, days=7, periods=2, group_ids=None, **_):
    """Call-centre activity in consecutive periods of `days` full days, newest first: the last
    `days` days, the `days` before them, and so on for `periods` periods (7 x 4 = the last four
    weeks, week by week). Totals per period, plus the busiest clients' calls per period. Read from
    the per-day call rows on client_groups (`hp_daily_calls`), the rows the Sales Coach charts draw.
    Kept small on purpose: a tool result is cut at a few thousand characters."""
    try:
        days, periods = int(days), int(periods)
    except (TypeError, ValueError):
        return {"error": "days and periods must be whole numbers, e.g. days=7, periods=4"}
    if days < 1 or days > MAX_TREND_DAYS:
        return {"error": f"days must be between 1 and {MAX_TREND_DAYS}"}
    if periods < 2 or periods > MAX_TREND_PERIODS:
        return {"error": f"periods must be between 2 and {MAX_TREND_PERIODS}"}
    if days * periods > 366:
        return {"error": "That reaches back more than a year. Use fewer or shorter periods."}

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}
    groups = await db["client_groups"].find(query, {"id": 1, "name": 1, "hp_daily_calls": 1, "_id": 0}).to_list(None)

    today = date.today()
    # windows[0] is the newest: the `days` full days ending yesterday (today is still filling in).
    windows = [((today - timedelta(days=days * (n + 1))).isoformat(),
                (today - timedelta(days=days * n + 1)).isoformat()) for n in range(periods)]

    clients, all_rows = [], []
    for g in groups:
        rows = g.get("hp_daily_calls")
        if not isinstance(rows, list) or not rows:
            continue  # no call centre data for this client: left out, not shown as zeros
        per = [_window_totals(rows, since, until) for since, until in windows]
        all_rows.extend(rows)
        clients.append({
            "client_group_id": g.get("id"), "client_group_name": g.get("name"),
            "calls": [p["calls"] for p in per], "answered": [p["answered"] for p in per],
            "calls_change_pct": _change(per[0]["calls"], per[1]["calls"]),
        })
    if not clients:
        return {"error": "No call centre data is stored for these clients yet."}

    totals = [{"since": since, "until": until, **_window_totals(all_rows, since, until)} for since, until in windows]
    clients.sort(key=lambda c: c["calls"][0], reverse=True)
    return {
        "days_per_period": days,
        "periods": totals,                                   # newest first
        "change_pct_latest_vs_previous": {k: _change(totals[0][k], totals[1][k]) for k in (*_SUMMED, "talk_min")},
        "clients": clients[:TREND_CLIENTS], "total_clients": len(clients), "shown_clients": min(len(clients), TREND_CLIENTS),
        "note": "Periods and each client's calls/answered lists are newest first. Full days only; today is "
                "left out. Clients are the busiest in the newest period; pass group_ids for others. These "
                "are calls only: closed deals (won) and open opportunities come from get_ghl_opportunity_stats "
                "or get_ghl_opp_stats_windowed, and lead counts are not here.",
    }


# ── Schema ──────────────────────────────────────────────────────────────────

_CALL_CENTER_PARAMS = {
    "type": "object",
    "properties": {
        "preset": {
            "type": "string",
            "description": (
                "Date-window preset. One of: today, yesterday, last_7d, last_14d, "
                "last_30d, this_month, last_month, this_quarter, last_quarter, "
                "this_year, last_year, maximum. Default last_7d."
            ),
        },
        "group_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional client group ids to filter to (from get_client_groups).",
        },
    },
    "required": [],
}


def register_hp_tools():
    registry.register(
        name="get_call_center_stats",
        description=(
            "Get HotProspector call-center stats per client for a date window: total / "
            "inbound / outbound calls, transfers, answered calls, talk time, leads called, "
            "plus connect_rate (answered / total calls — lead reachability) and "
            "leads_called_rate (coverage). Use for lead-quality analysis: an ad with a "
            "cheap CPL but a low connect_rate is producing unreachable / low-intent leads. "
            "If leads_called_rate is low, that's a call-center coverage (operations) gap, "
            "not a media problem. Pass a preset and optional group_ids."
        ),
        parameters=_CALL_CENTER_PARAMS,
        executor=get_call_center_stats,
    )
    registry.register(
        name="get_call_trend",
        description=(
            "Call-centre activity in consecutive periods, newest first: calls, answered calls, connect "
            "rate, transfers and talk time per period, plus the busiest clients' calls per period. "
            "This week against last week is days=7, periods=2; the last month week by week is days=7, "
            "periods=4; month against month is days=30, periods=2. Use this for any 'trend', 'per week', "
            "'why did X drop / rise' or 'compared with last week' question about calls."
        ),
        parameters={
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": f"Length of each period in days, 1 to {MAX_TREND_DAYS}. Default 7."},
                "periods": {"type": "integer", "description": f"How many consecutive periods, 2 to {MAX_TREND_PERIODS}. Default 2."},
                "group_ids": {"type": "array", "items": {"type": "string"},
                              "description": "Optional client group ids to filter to (from get_client_groups)."},
            },
            "required": [],
        },
        executor=get_call_trend,
    )
