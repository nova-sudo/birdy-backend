"""
ai/mcp/unified_leads_mcp.py
----------------------------
The unified leads tools (get_unified_leads, get_unified_lead_stats), registered
onto the shared FastMCP server (ai/mcp/server.py) served over the Model
Context Protocol, mounted into the main FastAPI app at /mcp (see main.py).

Both tools call ai/tools/unified_leads_tools.py, so the chat and MCP paths
cannot drift apart.

Leads matched across GHL, Meta, and HotProspector using normalized
email/phone match_keys.
"""

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.access import READ
from ai.tools import unified_leads_tools
from ai.mcp.cards import card
from ai.mcp.server import mcp, current_user_id as _current_user_id


_GET_UNIFIED_LEADS_DESCRIPTION = (
    "Get leads with cross-source matching info. Each lead shows which sources (GHL/Meta/HP) "
    "have a record of it, plus Meta campaign/ad names and GHL opportunity status/tags. "
    "Supports filters: source, matched_only, opportunity_status, has_tag, date range."
)


@mcp.tool(description=_GET_UNIFIED_LEADS_DESCRIPTION, **READ, meta=card("leads"))
async def get_unified_leads(
    group_ids: list[str] | None = None,
    source: str | None = None,
    matched_only: bool = False,
    opportunity_status: str | None = None,
    has_tag: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Args:
        group_ids: Filter to specific client group IDs. Omit for all groups.
        source: Filter to leads present in a specific source: 'ghl', 'meta', or 'hp'.
        matched_only: If true, only return leads matched across 2+ sources.
        opportunity_status: Filter by GHL opp status: 'won', 'lost', 'open', 'abandoned'.
        has_tag: Filter to leads with this GHL tag.
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        limit: Max leads (default 50, max 200)
    """
    return await unified_leads_tools.get_unified_leads(
        get_db(get_shared_mongo_client()), _current_user_id(), group_ids=group_ids, source=source,
        matched_only=matched_only, opportunity_status=opportunity_status, has_tag=has_tag,
        start_date=start_date, end_date=end_date, limit=limit)


_GET_UNIFIED_LEAD_STATS_DESCRIPTION = (
    "Get cross-source lead stats: totals per source (GHL/Meta/HP) and overlap counts "
    "(GHL-only, GHL+Meta matched, GHL+HP matched, all three). Use for questions like "
    "'how many of my Meta leads show up in GHL' or 'what's my lead match rate'."
)


@mcp.tool(description=_GET_UNIFIED_LEAD_STATS_DESCRIPTION, **READ)
async def get_unified_lead_stats(
    group_ids: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Args:
        group_ids: Filter to specific client group IDs. Omit for all groups.
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
    """
    return await unified_leads_tools.get_unified_lead_stats(
        get_db(get_shared_mongo_client()), _current_user_id(), group_ids=group_ids,
        start_date=start_date, end_date=end_date)
