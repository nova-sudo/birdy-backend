"""
ai/mcp/summary_mcp.py
----------------------
The account summary tool (get_account_summary), registered onto the shared
FastMCP server (ai/mcp/server.py) served over the Model Context Protocol,
mounted into the main FastAPI app at /mcp (see main.py). This is a real MCP
server — reachable by external MCP clients (Claude Desktop, Claude Code,
etc.), not just Birdy's own orchestrator.

Business logic here is a straight port of ai/tools/summary_tools.py — see
that module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.
"""

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.tools.derived_metrics import enrich, _safe_float
from ai.config import MAX_RESULT_ITEMS
from ai.mcp.server import mcp, current_user_id as _current_user_id

# Valid presets for the schema description
_PRESET_LIST = (
    "maximum, today, yesterday, this_week_mon_today, last_7d, last_14d, "
    "last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year"
)

_GET_ACCOUNT_SUMMARY_DESCRIPTION = (
    "Get a high-level dashboard summary across all or selected client groups. "
    "Returns aggregated spend, leads, CPL, conversion rate, frequency, and GHL contact counts. "
    "Use this for overview questions like 'how am I doing overall' or 'total spend this month'."
)


@mcp.tool(description=_GET_ACCOUNT_SUMMARY_DESCRIPTION)
async def get_account_summary(preset: str = "last_7d", group_ids: list[str] | None = None) -> dict:
    """
    High-level dashboard summary across all or selected client groups.
    Reads pre-aggregated metrics from facebook_cache and gohighlevel_cache.

    Args:
        preset: Date range preset. Valid values: maximum, today, yesterday, this_week_mon_today,
            last_7d, last_14d, last_30d, this_month, last_month, this_quarter, last_quarter,
            this_year, last_year. Default: last_7d.
        group_ids: Filter to specific group IDs. Omit for all groups.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            f"facebook_cache.presets.{preset}.metrics": 1,
            f"facebook_cache.{preset}.metrics": 1,
            "facebook_cache.currency": 1,
            "facebook_cache.total_leads": 1,
            "gohighlevel_cache.metrics": 1,
            "_id": 0,
        },
    ).to_list(None)

    per_group = []
    totals = {
        "spend": 0, "impressions": 0, "clicks": 0, "reach": 0,
        "results": 0, "total_leads": 0, "ghl_contacts": 0,
    }

    for g in groups:
        fb = g.get("facebook_cache", {})
        # Split shape first, legacy bucket second.
        preset_data = (fb.get("presets") or {}).get(preset) or fb.get(preset) or {}
        insights = preset_data.get("metrics", {}).get("insights", {})
        ghl = g.get("gohighlevel_cache", {}).get("metrics", {})

        row = {
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "currency": fb.get("currency"),
            "spend": _safe_float(insights.get("spend")) or 0,
            "impressions": _safe_float(insights.get("impressions")) or 0,
            "clicks": _safe_float(insights.get("clicks")) or 0,
            "reach": _safe_float(insights.get("reach")) or 0,
            "results": _safe_float(insights.get("results")) or 0,
            "total_leads": _safe_float(insights.get("total_leads")) or 0,
            "cpm": _safe_float(insights.get("cpm")),
            "cpc": _safe_float(insights.get("cpc")),
            "ctr": _safe_float(insights.get("ctr")),
            "ghl_contacts": ghl.get("total_contacts", 0),
            "ghl_top_tags": dict(
                sorted(ghl.get("tag_breakdown", {}).items(), key=lambda x: x[1], reverse=True)[:5]
            ) if ghl.get("tag_breakdown") else {},
        }
        enrich(row)
        per_group.append(row)

        for k in ("spend", "impressions", "clicks", "reach", "results", "total_leads"):
            totals[k] += row[k]
        totals["ghl_contacts"] += row["ghl_contacts"]

    overall = enrich({**totals})

    return {
        "overall": overall,
        "groups": per_group[:MAX_RESULT_ITEMS],
        "preset": preset,
        "total_groups": len(per_group),
    }
