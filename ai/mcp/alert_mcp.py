"""
ai/mcp/alert_mcp.py
--------------------
The alert tools (get_alerts, create_alert, update_alert), registered onto the
shared FastMCP server (ai/mcp/server.py) served over the Model Context
Protocol, mounted into the main FastAPI app at /mcp (see main.py). This is a
real MCP server — reachable by external MCP clients (Claude Desktop, Claude
Code, etc.), not just Birdy's own orchestrator.

Business logic here is a straight port of ai/tools/alert_tools.py — see that
module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.
"""

import logging
from typing import Literal
from datetime import datetime, timedelta

from core.constants import METRIC_LABELS, OPERATOR_LABELS
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from core.utils import mongo_to_dict
from fastmcp.exceptions import ToolError
from services.alert_service import format_condition_display, evaluate_alert, normalize_channels
from services import slack_reports
from services.slack_bot_service import get_slack_bot_status
from ai.mcp.access import READ, WRITE
from ai.mcp.server import mcp, current_user_id as _current_user_id

logger = logging.getLogger(__name__)

_VALID_METRICS = ", ".join(METRIC_LABELS.keys())
_VALID_OPERATORS = "gt (>), lt (<), eq (=), neq (≠), pct_drop (% decrease), pct_rise (% increase)"
_VALID_PERIODS = "today, day (yesterday), week (last 7 days), month (last 30 days)"
_VALID_TYPES = "win, warning"
_VALID_FREQUENCIES = "realtime, hourly, daily, weekly"
SLACK_WARNING = "Slack isn't connected, so this alert will only show in Birdy until you connect it."
SLACK_RECONNECT = "Slack needs reconnecting in Birdy (Settings), so this alert will only show in Birdy until then."
SLACK_NEEDS_REPORT = (
    "Slack is connected, but alerts reach Slack through a Slack report, and none posts this alert yet. To post it, "
    'call create_slack_report with trigger {{"type": "event", "event": "alert_fired", "alert_id": "{alert_id}"}} '
    "(it goes to the account's Birdy channel unless you pass channel_id). Until then it only shows in Birdy."
)


async def _slack_note(db, user_id: str, alert_id: str) -> str | None:
    """Why a "slack" alert won't reach Slack, or None when it will.

    The alert's "slack" channel posts nothing by itself: a Slack report on the alert_fired event does the
    posting, through the Slack bot install (users.integrations.slack_bot). The old webhook field is unused."""
    bot = await get_slack_bot_status(db, user_id)
    if not bot:
        return SLACK_WARNING
    if bot.get("needs_reconnect"):
        return SLACK_RECONNECT
    report = await db[slack_reports.REPORTS].find_one({
        "user_id": user_id, "enabled": True, "trigger.type": "event", "trigger.event": "alert_fired",
        "$or": [{"trigger.alert_id": alert_id}, {"trigger.alert_id": None}, {"trigger.alert_id": {"$exists": False}}],
    }, {"_id": 1})
    return None if report else SLACK_NEEDS_REPORT.format(alert_id=alert_id)


@mcp.tool(**READ)
async def get_alerts(status: str | None = None) -> dict:
    """List the user's alerts with their conditions, status, and trigger history.

    Args:
        status: Filter by status: active, paused, or triggered. Omit for all.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    query = {"user_id": user_id}
    if status:
        query["status"] = status

    docs = await db["alerts"].find(query).sort("created_at", -1).to_list(None)

    alerts = []
    counts = {"total": 0, "active": 0, "triggered": 0, "paused": 0}

    for a in docs:
        d = mongo_to_dict(a)
        d["condition_display"] = format_condition_display(d.get("condition", {}))
        d["metric_label"] = METRIC_LABELS.get(
            d.get("condition", {}).get("metric", ""), "Unknown").replace(" ($)", "")
        alerts.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "description": d.get("description", ""),
            "condition_display": d["condition_display"],
            "metric_label": d["metric_label"],
            "condition": d.get("condition"),
            "target_group_names": d.get("target_group_names", []),
            "status": d.get("status"),
            "last_triggered_at": d.get("last_triggered_at"),
            "trigger_count": d.get("trigger_count", 0),
            "snoozed_until": d.get("snoozed_until"),
        })
        s = d.get("status", "active")
        counts["total"] += 1
        if s in counts:
            counts[s] += 1

    return {"alerts": alerts, "counts": counts}


_CREATE_ALERT_DESCRIPTION = (
    "Create a new alert that monitors a metric and triggers when a condition is met. "
    "The alert is auto-evaluated immediately after creation. "
    "Meta metrics (spend, impressions, clicks, reach, ctr, cpc, cpm, meta_leads, meta_conversion, cpl, cost_per_result, frequency) "
    "track Facebook/Meta Ads data. GHL metrics (ghl_leads, ghl_conversion) track GoHighLevel data. "
    "Call Center metrics track HotProspector data: per-client (hp_total_calls, hp_inbound, hp_outbound, hp_transfers, "
    "hp_leads_with_calls, hp_answered_calls, hp_talk_time, hp_connect_rate, hp_answer_rate) and account-wide per-agent "
    "(hp_agent_outbound, hp_agent_inbound, hp_agent_answered, hp_agent_convos, hp_agent_appts, hp_agent_talk_min, hp_agent_answer_rate). "
    "For GHL tags, use 'tag:TAG_NAME' as the metric (e.g., 'tag:Hot Lead', 'tag:booked consult hp'). "
    f"Valid metrics: {_VALID_METRICS}. Valid operators: {_VALID_OPERATORS}. "
    f"Valid periods: {_VALID_PERIODS}. Valid types: {_VALID_TYPES}. Valid frequencies: {_VALID_FREQUENCIES}. "
    "notification_channels chooses where it notifies: in_app (default), slack, email. "
    "Slack posting is done by a Slack report on the alert_fired event; if slack is chosen and no such report "
    "posts this alert (or Slack isn't connected), the alert is still created and the result's warning says what to do."
)


@mcp.tool(description=_CREATE_ALERT_DESCRIPTION, **WRITE)
async def create_alert(
    name: str,
    metric: str,
    operator: str,
    value: float,
    period: str = "day",
    type: str = "warning",
    frequency: str = "daily",
    target_group_ids: list[str] | None = None,
    description: str = "",
    notification_channels: list[Literal["in_app", "slack", "email"]] | None = None,
) -> dict:
    """Create an alert. notification_channels picks where it notifies: "in_app" (default), "slack", "email".
    A Slack report on the alert_fired event does the Slack posting; if none posts this alert, or Slack isn't
    connected, the alert is still created and the result's warning says what to do."""
    user_id = _current_user_id()
    try:
        channels = normalize_channels(notification_channels) if notification_channels is not None else ["in_app"]
    except ValueError as e:
        raise ToolError(str(e))
    mongo_client = get_shared_mongo_client()
    db = get_db(mongo_client)

    if not metric.startswith("tag:") and not metric.startswith("custom:") and metric not in METRIC_LABELS:
        return {"error": f"Invalid metric '{metric}'. Valid: {', '.join(METRIC_LABELS.keys())}, 'tag:TAG_NAME' for GHL tags, or 'custom:METRIC_ID' for custom metrics."}
    if operator not in OPERATOR_LABELS:
        return {"error": f"Invalid operator '{operator}'. Valid: {', '.join(OPERATOR_LABELS.keys())}"}

    try:
        value = float(value)
    except (TypeError, ValueError):
        return {"error": f"Invalid value '{value}'. Must be a number."}

    valid_periods = ("today", "day", "week", "month")
    if period not in valid_periods:
        return {"error": f"Invalid period '{period}'. Valid: {', '.join(valid_periods)}"}

    alert_id = f"alert_{user_id}_{int(datetime.utcnow().timestamp() * 1000)}"

    # Alerts only ever target active clients
    group_names = []
    if target_group_ids:
        groups = await db["client_groups"].find(
            {"id": {"$in": target_group_ids}, "user_id": user_id, "client_status": {"$ne": "Inactive"}},
            {"name": 1, "id": 1},
        ).to_list(None)
        target_group_ids = [g["id"] for g in groups]
        group_names = [g["name"] for g in groups]

    alert_doc = {
        "id": alert_id,
        "user_id": user_id,
        "name": name,
        "description": description or "",
        "type": type or "warning",
        "condition": {
            "metric": metric,
            "operator": operator,
            "value": value,
            "period": period,
        },
        "target_group_ids": target_group_ids or [],
        "target_group_names": group_names,
        "notification_channels": channels,
        "frequency": frequency or "daily",
        "status": "active",
        "last_triggered_at": None,
        "last_evaluated_at": None,
        "trigger_count": 0,
        "snoozed_until": None,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    await db["alerts"].insert_one(alert_doc)

    eval_msg = ""
    try:
        eval_result = await evaluate_alert(alert_doc, mongo_client)
        update = {
            "last_evaluated_at": datetime.utcnow(),
            "last_eval_result": eval_result,
            "current_value": eval_result.get("current_value", 0.0),
            "progress_pct": eval_result.get("progress_pct", 0.0),
            "updated_at": datetime.utcnow(),
        }
        if eval_result.get("triggered"):
            update["status"] = "triggered"
            update["last_triggered_at"] = datetime.utcnow()
        await db["alerts"].update_one({"id": alert_id}, {"$set": update})
        eval_msg = f" Initial evaluation: {eval_result.get('message', '')}"
    except Exception as e:
        logger.warning(f"Auto-evaluation failed for {alert_id}: {e}")

    result = {
        "success": True,
        "alert_id": alert_id,
        "name": name,
        "condition_display": format_condition_display(alert_doc["condition"]),
        "target_groups": group_names or ["all groups"],
        "notification_channels": channels,
        "message": f"Alert '{name}' created successfully.{eval_msg}",
    }
    if "slack" in channels and (note := await _slack_note(db, user_id, alert_id)):
        result["warning"] = note
    return result


@mcp.tool(**WRITE)
async def update_alert(
    alert_id: str,
    name: str | None = None,
    status: str | None = None,
    type: str | None = None,
    frequency: str | None = None,
    metric: str | None = None,
    operator: str | None = None,
    value: float | None = None,
    period: str | None = None,
    target_group_ids: list[str] | None = None,
    description: str | None = None,
    snooze_hours: int | None = None,
    notification_channels: list[Literal["in_app", "slack", "email"]] | None = None,
) -> dict:
    """Update an existing alert. Only provided fields are changed.

    Can change name, type, frequency, status (active/paused), condition,
    target groups, or snooze it for a number of hours.

    Args:
        alert_id: The alert ID to update (required).
        name: New alert name.
        status: New status: active or paused.
        type: New type. Valid: win, warning.
        frequency: New frequency. Valid: realtime, hourly, daily, weekly.
        metric: New metric.
        operator: New operator. Valid: gt, lt, eq, neq, pct_drop, pct_rise.
        value: New threshold value.
        period: New period. Valid: today, day, week, month.
        target_group_ids: New target group IDs.
        description: New description.
        snooze_hours: Snooze alert for this many hours.
        notification_channels: Replace where it notifies: in_app, slack, email. Omit to leave unchanged.
    """
    user_id = _current_user_id()
    mongo_client = get_shared_mongo_client()
    db = get_db(mongo_client)
    channels = None
    if notification_channels is not None:
        try:
            channels = normalize_channels(notification_channels)
        except ValueError as e:
            raise ToolError(str(e))

    alert = await db["alerts"].find_one({"id": alert_id, "user_id": user_id})
    if not alert:
        return {"error": f"Alert '{alert_id}' not found."}

    update_fields = {"updated_at": datetime.utcnow()}

    if name is not None:
        update_fields["name"] = name
    if description is not None:
        update_fields["description"] = description
    if status is not None:
        update_fields["status"] = status
    if type is not None:
        update_fields["type"] = type
    if frequency is not None:
        update_fields["frequency"] = frequency
    if channels is not None:
        update_fields["notification_channels"] = channels

    if any(v is not None for v in (metric, operator, value, period)):
        condition = alert.get("condition", {})
        if metric is not None:
            if metric not in METRIC_LABELS:
                return {"error": f"Invalid metric '{metric}'. Valid: {', '.join(METRIC_LABELS.keys())}"}
            condition["metric"] = metric
        if operator is not None:
            if operator not in OPERATOR_LABELS:
                return {"error": f"Invalid operator '{operator}'. Valid: {', '.join(OPERATOR_LABELS.keys())}"}
            condition["operator"] = operator
        if value is not None:
            try:
                condition["value"] = float(value)
            except (TypeError, ValueError):
                return {"error": f"Invalid value '{value}'. Must be a number."}
        if period is not None:
            condition["period"] = period
        update_fields["condition"] = condition

    # Target groups — alerts only ever target active clients
    if target_group_ids is not None:
        groups = await db["client_groups"].find(
            {"id": {"$in": target_group_ids}, "user_id": user_id, "client_status": {"$ne": "Inactive"}},
            {"name": 1, "id": 1},
        ).to_list(None)
        update_fields["target_group_ids"] = [g["id"] for g in groups]
        update_fields["target_group_names"] = [g["name"] for g in groups]

    if snooze_hours is not None:
        try:
            hours = int(snooze_hours)
        except (TypeError, ValueError):
            return {"error": f"Invalid snooze_hours '{snooze_hours}'. Must be an integer."}
        update_fields["status"] = "paused"
        update_fields["snoozed_until"] = datetime.utcnow() + timedelta(hours=hours)

    await db["alerts"].update_one({"id": alert_id}, {"$set": update_fields})

    result = {
        "success": True,
        "alert_id": alert_id,
        "updated_fields": [k for k in update_fields if k != "updated_at"],
        "message": f"Alert '{alert.get('name')}' updated.",
    }
    if channels and "slack" in channels and (note := await _slack_note(db, user_id, alert_id)):
        result["warning"] = note
    return result
