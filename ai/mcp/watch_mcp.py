"""
ai/mcp/watch_mcp.py
-------------------
check_my_accounts: the "watch my data" tool behind the daily check-in and weekly review prompts.
The logic lives in services/account_check.py.
"""

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services.account_check import build_account_check


@mcp.tool(**READ)
async def check_my_accounts(days: int = 7) -> dict:
    """One verdict per client: which accounts need the owner's attention right now, and why. Use it first for "how are my clients doing?", the daily check-in and the weekly review, then dig into the flagged ones with get_client, compare_periods and get_agent_cards.

    Compares the last N days (ending yesterday) with the N days before. Each client has: spend, leads and cpl for
    the period; prior (the same three for the earlier period); change_pct (percent change, null when there is
    nothing to compare); target_cpl (the owner's cost-per-lead goal, null if unset); alerts_fired (distinct
    alerts that fired in the period); open_agent_cards (agent suggestions waiting for a decision); attention
    ("high", "medium" or "ok"); and reasons, plain-English lines saying why. Money is in that client's ad account
    currency. Clients are sorted high first, then medium, then ok, biggest spend first. summary counts each level.

    High means CPL more than 20% over target, an alert fired, or spend stopped. Medium means CPL up 25% or more,
    leads down 25% or more (only when the prior period had 5+ leads), or open agent cards. A client with no Meta
    ad account is "ok" with that reason. A client whose data couldn't be loaded is "medium" with the reason.

    This does no analysis or writing: the numbers and rules are fixed. Write the narrative yourself, leading with
    the high clients. Never invent a cause the data doesn't show.

    Args:
        days: Period length in days, 1-90 (default 7).
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 90:
        raise ToolError("days must be a whole number between 1 and 90.")
    client = get_shared_mongo_client()
    return await build_account_check(get_db(client), client, _current_user_id(), days)
