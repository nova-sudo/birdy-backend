"""
ai/mcp/compare_mcp.py
----------------------
The compare_periods tool, registered onto the shared FastMCP server
(ai/mcp/server.py) served over the Model Context Protocol, mounted into the
main FastAPI app at /mcp (see main.py). This is a real MCP server —
reachable by external MCP clients (Claude Desktop, Claude Code, etc.), not
just Birdy's own orchestrator.

Business logic here is a straight port of ai/tools/compare_tools.py — see
that module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.
"""

import math

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.tools.derived_metrics import enrich, _safe_float
from ai.config import MAX_RESULT_ITEMS
from ai.mcp.access import READ
from ai.mcp.cards import card
from ai.mcp.server import mcp, current_user_id as _current_user_id

_PRESET_LIST = (
    "maximum, today, yesterday, this_week_mon_today, last_7d, last_14d, "
    "last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year"
)

_COMPARE_METRICS = [
    "spend", "impressions", "clicks", "reach", "results",
    "total_leads", "cpm", "cpc", "ctr",
]

_COMPARE_PERIODS_DESCRIPTION = (
    "Compare two date presets side-by-side with absolute and percentage change for each metric. "
    "preset_a is the baseline (older period), preset_b is the comparison (current period). "
    "Use when the user says 'vs', 'compared to', 'change from', etc. "
    "cpl is spend / results (Meta's lead count), the same CPL every other tool gives; total_leads is the "
    "leads Birdy collected, which can differ by a few. "
    "Each client and the overall total also have cpl_drivers, which splits the CPL change into what moved it: "
    "cpm (cost per 1,000 impressions), ctr (clicks per impression) and lead_rate (leads per click). Each step "
    "has from, to and effect (its share of the CPL change, in money; the effects add up to the change), and "
    "biggest names the step with the largest effect. cpl_drivers is null when either period has no leads, "
    "clicks or impressions. Use it to answer 'why did CPL change?'. "
    f"Valid presets: {_PRESET_LIST}."
)


_DRIVERS = (("cpm", "CPM"), ("ctr", "CTR"), ("lead_rate", "Lead rate"))


def _cpl_drivers(a: dict, b: dict) -> dict | None:
    """Split the CPL change from a to b into CPM, CTR and lead-rate steps.

    CPL = spend/impressions x impressions/clicks x clicks/leads, so ln(CPL_b/CPL_a) is the sum of the three
    factors' log ratios; each step gets that share of the money change (they add up exactly). A higher CTR or
    lead rate lowers CPL, so those effects have the opposite sign to their own change.
    """
    def parts(r):
        # Meta's results, the same lead count the CPL itself is built on (see derived_metrics.enrich).
        leads_key = "results" if r.get("results") is not None else "total_leads"
        imp, clk, leads, spend = (_safe_float(r.get(k)) or 0 for k in ("impressions", "clicks", leads_key, "spend"))
        if min(imp, clk, leads, spend) <= 0:
            return None
        return {"cpl": spend / leads, "cpm": spend / imp * 1000, "ctr": clk / imp * 100, "lead_rate": leads / clk * 100}

    pa, pb = parts(a), parts(b)
    if not pa or not pb:
        return None
    total = pb["cpl"] - pa["cpl"]
    logs = {"cpm": math.log(pb["cpm"] / pa["cpm"]), "ctr": math.log(pa["ctr"] / pb["ctr"]),
            "lead_rate": math.log(pa["lead_rate"] / pb["lead_rate"])}
    whole = sum(logs.values())
    steps = [{"key": k, "label": label, "from": round(pa[k], 2), "to": round(pb[k], 2),
              "effect": round(total * logs[k] / whole, 2) if abs(whole) > 1e-9 else 0.0} for k, label in _DRIVERS]
    return {"from": round(pa["cpl"], 2), "to": round(pb["cpl"], 2), "steps": steps,
            "biggest": max(steps, key=lambda st: abs(st["effect"]))["key"] if abs(total) > 0.005 else None}


def _compute_changes(a: dict, b: dict) -> dict:
    """Compute delta and pct_change for each metric between two enriched dicts."""
    changes = {}
    all_keys = _COMPARE_METRICS + ["cpl", "conversion_rate", "frequency", "cost_per_result"]
    for key in all_keys:
        val_a = _safe_float(a.get(key))
        val_b = _safe_float(b.get(key))

        delta = None
        pct = None
        if val_a is not None and val_b is not None:
            delta = round(val_b - val_a, 4)
            if val_a != 0:
                pct = round((delta / val_a) * 100, 2)

        changes[key] = {"value_a": val_a, "value_b": val_b, "delta": delta, "pct_change": pct}
    return changes


@mcp.tool(description=_COMPARE_PERIODS_DESCRIPTION, **READ, meta=card("scorecard"))
async def compare_periods(preset_a: str, preset_b: str, group_ids: list[str] | None = None) -> dict:
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            # Split shape and legacy bucket — preferred in that order below.
            f"facebook_cache.presets.{preset_a}.metrics": 1,
            f"facebook_cache.presets.{preset_b}.metrics": 1,
            f"facebook_cache.{preset_a}.metrics": 1,
            f"facebook_cache.{preset_b}.metrics": 1,
            "facebook_cache.currency": 1,
            "_id": 0,
        },
    ).to_list(None)

    per_group = []
    totals_a = {"spend": 0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0, "total_leads": 0}
    totals_b = {**totals_a}

    for g in groups:
        fb = g.get("facebook_cache", {})
        _split = fb.get("presets") or {}

        def _insights(preset):
            bucket = _split.get(preset) or fb.get(preset) or {}
            return (bucket.get("metrics") or {}).get("insights") or {}

        ins_a = _insights(preset_a)
        ins_b = _insights(preset_b)

        row_a = {k: _safe_float(ins_a.get(k)) or 0 for k in _COMPARE_METRICS}
        row_b = {k: _safe_float(ins_b.get(k)) or 0 for k in _COMPARE_METRICS}

        enrich(row_a)
        enrich(row_b)

        per_group.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "currency": fb.get("currency"),
            "period_a": row_a,
            "period_b": row_b,
            "changes": _compute_changes(row_a, row_b),
            "cpl_drivers": _cpl_drivers(row_a, row_b),
        })

        for k in totals_a:
            totals_a[k] += row_a.get(k, 0) or 0
            totals_b[k] += row_b.get(k, 0) or 0

    overall_a = enrich({**totals_a})
    overall_b = enrich({**totals_b})

    return {
        "overall": {
            "period_a": overall_a,
            "period_b": overall_b,
            "changes": _compute_changes(overall_a, overall_b),
            "cpl_drivers": _cpl_drivers(overall_a, overall_b),
        },
        "groups": per_group[:MAX_RESULT_ITEMS],
        "preset_a": preset_a,
        "preset_b": preset_b,
        "total_groups": len(per_group),
    }
