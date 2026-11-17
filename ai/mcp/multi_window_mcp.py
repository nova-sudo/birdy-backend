"""
ai/mcp/multi_window_mcp.py
---------------------------
Day-by-day and multi-window metrics across the owner's clients. Wraps ai/tools/multi_window_tools.py, the same
code Ask Birdy uses; user_id always comes from the verified token.
"""

from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import multi_window_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client


@mcp.tool(**READ)
async def get_daily_metrics(days: int = 30, group_ids: list[str] | None = None) -> dict:
    """Meta ad spend, leads, clicks, impressions, CPL and CTR per calendar day for the last N days, per client plus a combined series.

    Use it for any trend or over-time question ("spend vs CPL over the last 30 days"). Each series carries a
    summary: the N-day total, the previous N days, and the percentage change. Use those totals instead of adding
    up the daily rows. A day with spend but no leads has cpl null; leave it null.

    Args:
        days: How many days back, 2-90 (default 30). The series ends yesterday.
        group_ids: Optional client group ids (from get_client_groups). Omit for all clients.
    """
    return await multi_window_tools.get_daily_metrics(
        get_db(get_shared_mongo_client()), _current_user_id(), days=days, group_ids=group_ids,
        mongo_client=get_shared_mongo_client())


@mcp.tool(**READ)
async def get_metrics_by_day_windows(windows_days: list[int], group_ids: list[str] | None = None) -> dict:
    """Spend, leads, GHL opportunity stats (won/lost/open), revenue, ROAS, profit and CPL across several day windows ending today, in one call.

    Use it instead of calling other tools repeatedly when the owner compares two or more periods ("last 30/60/90
    days"). Windows are cumulative (90 days contains the 30), and all arithmetic is done for you: report the values
    as returned. If a result has a data_warning, tell the owner the numbers look inconsistent; do not invent a reason.

    Args:
        windows_days: Window sizes in days, e.g. [30, 60, 90]. At most 6.
        group_ids: Optional client group ids (from get_client_groups). Omit for all clients.
    """
    return await multi_window_tools.get_metrics_by_day_windows(
        get_db(get_shared_mongo_client()), _current_user_id(), windows_days=windows_days, group_ids=group_ids,
        mongo_client=get_shared_mongo_client())
