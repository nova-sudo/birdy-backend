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
from ai.mcp.access import READ
from ai.mcp.cards import card
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import summary_tools

# Valid presets for the schema description
_PRESET_LIST = (
    "maximum, today, yesterday, this_week_mon_today, last_7d, last_14d, "
    "last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year"
)

_GET_ACCOUNT_SUMMARY_DESCRIPTION = (
    "Get a high-level dashboard summary across all or selected client groups. "
    "Returns aggregated spend, leads, CPL, conversion rate, frequency, and GHL contact counts. "
    "results is Meta's lead count and cpl is spend / results, the same CPL every other tool gives; "
    "total_leads is the leads Birdy collected, which can differ by a few. "
    "Use this for overview questions like 'how am I doing overall' or 'total spend this month'."
)


@mcp.tool(description=_GET_ACCOUNT_SUMMARY_DESCRIPTION, **READ, meta=card("summary"))
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
    return await summary_tools.get_account_summary(db, user_id, preset=preset, group_ids=group_ids)
