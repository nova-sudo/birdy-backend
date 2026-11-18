"""
ai/mcp/watch_mcp.py
-------------------
check_my_accounts: the "watch my data" tool behind the daily check-in and weekly review prompts.
The logic lives in services/account_check.py.
"""

import asyncio

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ
from ai.mcp.cards import card
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import alert_service
from services.account_check import build_account_check
from services.alert_history import fired_alerts
from core.utils import mongo_to_dict

EVAL_TIMEOUT_SECONDS = 20


@mcp.tool(**READ, meta=card("accounts"))
async def check_my_accounts(days: int = 7) -> dict:
    """One verdict per client: which accounts need the owner's attention right now, and why. Use it first for "how are my clients doing?", the daily check-in and the weekly review, then dig into the flagged ones with get_client, compare_periods and get_agent_cards.

    Compares the last N days (ending yesterday) with the N days before. Each client has: spend, leads and cpl for
    the period; prior (the same three for the earlier period); change_pct (percent change, null when there is
    nothing to compare); target_cpl (the owner's cost-per-lead goal, null if unset); alerts_fired (distinct
    alerts that fired in the period, win alerts left out); win_alerts_fired (distinct win alerts that fired:
    good news, never a reason for attention); open_agent_cards (agent suggestions waiting for a decision); attention
    ("high", "medium" or "ok"); and reasons, plain-English lines saying why. Money is in that client's ad account
    currency. Clients are sorted high first, then medium, then ok, biggest spend first. summary counts each level.

    High means CPL more than 20% over target, spend with zero leads above 1.2x the target CPL (reason "Spent X with no leads"), an alert fired, or spend stopped. Medium means CPL up 25% or more,
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


@mcp.tool(**READ, meta=card("alert"))
async def get_fired_alerts(days: int = 7) -> dict:
    """The alerts that fired in the last N days (1-90, default 7), newest first. Use it for "what went off?" and before explain_alert.

    The alert job runs hourly and logs a row per pass while an alert stays triggered, so rows are
    collapsed into one firing: consecutive rows of the same alert (and client) no more than 90 minutes apart are
    one firing. Each firing has alert_id, alert_name, condition_display, group_id and client_name (null for
    account-wide alerts), first_triggered_at, last_seen_at, count (how many passes it stayed triggered), message
    and current_value.

    Args:
        days: Window length in days, 1-90.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 90:
        raise ToolError("days must be a whole number between 1 and 90.")
    db = get_db(get_shared_mongo_client())
    return {"days": days, "fired": await fired_alerts(db, _current_user_id(), days)}


@mcp.tool(**READ, meta=card("alert"))
async def explain_alert(alert_id: str) -> dict:
    """Explain one alert: why it fired or didn't. Use after get_fired_alerts or get_alerts.

    Returns alert (its settings), condition_display, last_eval_result (the stored result of the last check,
    including per_client_results), recent_notifications (the last 10 firing rows), and now: a fresh evaluation
    run just now, labelled "now". If that fresh check fails or takes over 20 seconds, now is {"error": ...} and
    everything else is still returned. This changes nothing: no state is saved and no notification is created.

    Args:
        alert_id: The alert's id.
    """
    user_id = _current_user_id()
    client = get_shared_mongo_client()
    db = get_db(client)
    alert = await db["alerts"].find_one({"id": alert_id, "user_id": user_id})
    if not alert:
        raise ToolError(f"Alert '{alert_id}' not found.")
    notes = await db["alert_notifications"].find(
        {"alert_id": alert_id, "user_id": user_id}, {"_id": 0}).sort("triggered_at", -1).limit(10).to_list(None)
    try:
        now = await asyncio.wait_for(alert_service.evaluate_alert(dict(alert), client), timeout=EVAL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        now = {"error": f"Fresh evaluation timed out after {EVAL_TIMEOUT_SECONDS}s."}
    except Exception as e:
        now = {"error": f"Fresh evaluation failed: {e}"}
    d = mongo_to_dict(alert)
    return {
        "alert": d,
        "condition_display": alert_service.format_condition_display(d.get("condition", {})),
        "last_eval_result": d.get("last_eval_result"),
        "recent_notifications": [mongo_to_dict(n) for n in notes],
        "now": now,
    }
