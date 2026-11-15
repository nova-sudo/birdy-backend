"""
ai/mcp/attribution_mcp.py
--------------------------
Which ads produce the closes. Wraps ai/tools/attribution_tools.py; user_id always comes from the token.
"""

from typing import Literal

from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import attribution_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client


@mcp.tool(**READ)
async def get_closes_by_ad(level: Literal["ad", "adset", "campaign"] = "ad", start_date: str | None = None,
                           end_date: str | None = None, group_ids: list[str] | None = None, limit: int = 15) -> dict:
    """Which campaigns, ad sets or ads produce the closes (won GoHighLevel opportunities), with revenue, leads, close rate, spend and cost per close, best first.

    Use it for "which ad is generating the closes or sales", "best ad by revenue" and "cost per close by ad". It
    counts leads that arrived in the window, by the ad that brought them in, and their opportunity's current status.

    Args:
        level: ad (default), adset or campaign.
        start_date: Start of the lead-arrival window, YYYY-MM-DD. Defaults to 30 days before end_date.
        end_date: End of the window, YYYY-MM-DD. Defaults to today.
        group_ids: Optional client group ids (from get_client_groups). Omit for all clients.
        limit: Rows to return (1-15, default 15).
    """
    return await attribution_tools.get_closes_by_ad(
        get_db(get_shared_mongo_client()), _current_user_id(), mongo_client=get_shared_mongo_client(),
        level=level, start_date=start_date, end_date=end_date, group_ids=group_ids, limit=limit)
