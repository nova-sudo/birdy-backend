"""
ai/mcp/hp_mcp.py
-----------------
HotProspector call-center stats per client. Wraps ai/tools/hp_tools.py; user_id always comes from the token.
"""

from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import hp_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client


@mcp.tool(**READ)
async def get_call_center_stats(preset: str = "last_7d", group_ids: list[str] | None = None) -> dict:
    """HotProspector call-center stats per client for a date window: total, inbound and outbound calls, transfers, answered calls, talk time, leads called, plus connect_rate and leads_called_rate.

    Use it for lead-quality questions. A cheap CPL with a low connect_rate means unreachable or low-intent leads; a
    low leads_called_rate is a call-center coverage gap (operations), not a media problem. Clients without
    HotProspector data are left out.

    Args:
        preset: today, yesterday, last_7d (default), last_14d, last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year or maximum.
        group_ids: Optional client group ids (from get_client_groups). Omit for all clients.
    """
    return await hp_tools.get_call_center_stats(
        get_db(get_shared_mongo_client()), _current_user_id(), preset=preset, group_ids=group_ids)
