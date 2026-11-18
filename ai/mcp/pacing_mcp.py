"""
ai/mcp/pacing_mcp.py
--------------------
get_budget_pacing: how far each client is through this month's budget (targets.monthly_spend) against how far
through the month we are. Reads the cached this_month spend (the same numbers the dashboard shows); no Meta call.
"""

import calendar
from datetime import date, datetime, timedelta, timezone

from ai.mcp.access import READ
from ai.mcp.cards import card
from ai.mcp.server import current_user_id as _current_user_id, mcp
from ai.tools.derived_metrics import _safe_float
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

PRESET = "this_month"
OVER = 10  # points ahead of the month's share: "over"
UNDER = 15  # points behind: "under"


def pace_row(spend: float, budget: float | None, today: date) -> dict:
    """One client's pacing. Day N of the month counts today as half spent (the cache has part of today)."""
    days = calendar.monthrange(today.year, today.month)[1]
    elapsed = today.day - 0.5
    expected_pct = round(elapsed / days * 100, 1)
    row = {"spend": round(spend, 2), "budget": budget, "used_pct": None, "expected_pct": expected_pct,
           "projected": round(spend / elapsed * days, 2), "status": "no_budget", "runs_out_on": None}
    if not budget:
        return row
    used = spend / budget * 100
    row["used_pct"] = round(used, 1)
    row["status"] = "over" if used - expected_pct > OVER else "under" if expected_pct - used > UNDER else "on_pace"
    if spend > 0 and used < 100:
        day = elapsed * budget / spend  # day of the month the budget is used up at this pace
        if day < days - 1:  # a day or less before month end is on pace, not running out
            row["runs_out_on"] = (today.replace(day=1) + timedelta(days=int(day))).isoformat()
    elif used >= 100:
        row["runs_out_on"] = "already"
    return row


@mcp.tool(**READ, meta=card("pacing"))
async def get_budget_pacing() -> dict:
    """This month's budget pacing per client: month-to-date spend against the client's monthly budget (its monthly_spend target), next to how far through the month we are. Use it for "are we on budget?", "who will overspend?" and month-end planning.

    Each client has spend (month to date, the client's ad account currency), budget (null when no monthly_spend
    target is set: set one with set_client_target), used_pct, expected_pct (share of the month gone, the same for
    everyone), projected (month-end spend at the current daily rate), status ("over" more than 10 points ahead,
    "under" more than 15 points behind, "on_pace", or "no_budget"), and runs_out_on (the date the budget is used up
    at this pace if that is before month end, "already" when it is used up, else null). Clients with a budget come
    first, most ahead of pace first. Spend is from the cached this-month numbers, refreshed through the day.
    """
    db = get_db(get_shared_mongo_client())
    today = datetime.now(timezone.utc).date()
    groups = await db["client_groups"].find(
        {"user_id": _current_user_id(), "client_status": {"$ne": "Inactive"}},
        {"id": 1, "name": 1, "targets.monthly_spend": 1, "facebook_cache.currency": 1, "ad_account_currency": 1,
         f"facebook_cache.presets.{PRESET}.metrics.insights.spend": 1, f"facebook_cache.{PRESET}.metrics.insights.spend": 1,
         "_id": 0},
    ).to_list(None)
    clients = []
    for g in groups:
        fb = g.get("facebook_cache") or {}
        bucket = (fb.get("presets") or {}).get(PRESET) or fb.get(PRESET) or {}
        spend = _safe_float(((bucket.get("metrics") or {}).get("insights") or {}).get("spend")) or 0.0
        budget = _safe_float((g.get("targets") or {}).get("monthly_spend")) or None
        clients.append({"client": {"id": g.get("id"), "name": g.get("name")},
                        "currency": fb.get("currency") or g.get("ad_account_currency"), **pace_row(spend, budget, today)})
    clients.sort(key=lambda c: (c["budget"] is None, -((c["used_pct"] or 0) - c["expected_pct"]), -c["spend"]))
    days = calendar.monthrange(today.year, today.month)[1]
    with_budget = [c for c in clients if c["budget"]]
    return {
        "month": today.strftime("%Y-%m"), "day": today.day, "days_in_month": days,
        "expected_pct": clients[0]["expected_pct"] if clients else round((today.day - 0.5) / days * 100, 1),
        "clients": clients,
        "summary": {s: sum(1 for c in with_budget if c["status"] == s) for s in ("over", "on_pace", "under")}
        | {"no_budget": len(clients) - len(with_budget)},
    }
