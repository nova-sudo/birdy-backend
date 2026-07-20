import logging
from datetime import datetime, timedelta

from ai.tools.registry import registry
from core.constants import METRIC_LABELS, OPERATOR_LABELS
from core.utils import mongo_to_dict
from services.alert_service import format_condition_display, evaluate_alert

logger = logging.getLogger(__name__)


# ── Executors ──────────────────────────────────────────────────────────────

async def get_alerts(db, user_id, status=None, **_):
    """List all alerts for the user, optionally filtered by status."""
    query = {"user_id": user_id}
    if status:
        query["status"] = status

    docs = await db["alerts"].find(query).sort("created_at", -1).to_list(None)

    alerts = []
    counts = {"total": 0, "active": 0, "triggered": 0, "paused": 0}

    for a in docs:
        d = mongo_to_dict(a)
        d["condition_display"] = format_condition_display(d.get("condition", {}))
        d["metric_label"] = METRIC_LABELS.get(d.get("condition", {}).get("metric", ""), "Unknown")
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


async def create_alert(
    db, user_id, mongo_client=None,
    name="", metric="", operator="", value=0,
    period="day", type="warning", frequency="daily",
    target_group_ids=None,
    description="",
    **_,
):
    """Create a new alert with a metric threshold condition."""
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

    # Resolve group names — alerts only ever target active clients
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
        "notification_channels": ["in_app"],
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

    # Auto-evaluate immediately
    eval_msg = ""
    if mongo_client:
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

    return {
        "success": True,
        "alert_id": alert_id,
        "name": name,
        "condition_display": format_condition_display(alert_doc["condition"]),
        "target_groups": group_names or ["all groups"],
        "message": f"Alert '{name}' created successfully.{eval_msg}",
    }


async def update_alert(
    db, user_id, alert_id,
    name=None, status=None, type=None, frequency=None,
    metric=None, operator=None, value=None, period=None,
    target_group_ids=None, description=None,
    snooze_hours=None,
    **_,
):
    """Update an existing alert. Only provided fields are changed."""
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

    # Partial condition update — merge with existing
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

    # Snooze
    if snooze_hours is not None:
        try:
            hours = int(snooze_hours)
        except (TypeError, ValueError):
            return {"error": f"Invalid snooze_hours '{snooze_hours}'. Must be an integer."}
        update_fields["status"] = "paused"
        update_fields["snoozed_until"] = datetime.utcnow() + timedelta(hours=hours)

    await db["alerts"].update_one({"id": alert_id}, {"$set": update_fields})

    return {
        "success": True,
        "alert_id": alert_id,
        "updated_fields": [k for k in update_fields if k != "updated_at"],
        "message": f"Alert '{alert.get('name')}' updated.",
    }


# ── Registration ───────────────────────────────────────────────────────────

_VALID_METRICS = ", ".join(METRIC_LABELS.keys())
_VALID_OPERATORS = "gt (>), lt (<), eq (=), neq (≠), pct_drop (% decrease), pct_rise (% increase)"
_VALID_PERIODS = "today, day (yesterday), week (last 7 days), month (last 30 days)"
_VALID_TYPES = "win, warning"
_VALID_FREQUENCIES = "realtime, hourly, daily, weekly"


def register_alert_tools():
    registry.register(
        name="get_alerts",
        description="List the user's alerts with their conditions, status, and trigger history.",
        parameters={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Filter by status: active, paused, or triggered. Omit for all.",
                },
            },
            "required": [],
        },
        executor=get_alerts,
    )

    registry.register(
        name="create_alert",
        description=(
            "Create a new alert that monitors a metric and triggers when a condition is met. "
            "The alert is auto-evaluated immediately after creation. "
            "Meta metrics (spend, impressions, clicks, reach, ctr, cpc, cpm, meta_leads, meta_conversion, cpl, cost_per_result, frequency) "
            "track Facebook/Meta Ads data. GHL metrics (ghl_leads, ghl_conversion) track GoHighLevel data. "
            "Call Center metrics track HotProspector data: per-client (hp_total_calls, hp_inbound, hp_outbound, hp_transfers, "
            "hp_leads_with_calls, hp_answered_calls, hp_talk_time, hp_connect_rate, hp_answer_rate) and account-wide per-agent "
            "(hp_agent_outbound, hp_agent_inbound, hp_agent_answered, hp_agent_convos, hp_agent_appts, hp_agent_talk_min, hp_agent_answer_rate). "
            "For GHL tags, use 'tag:TAG_NAME' as the metric (e.g., 'tag:Hot Lead', 'tag:booked consult hp'). "
            f"Valid operators: {_VALID_OPERATORS}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Alert name (e.g., 'High Spend Warning')."},
                "metric": {"type": "string", "description": f"Metric to monitor. Valid: {_VALID_METRICS}."},
                "operator": {"type": "string", "description": f"Comparison operator. Valid: {_VALID_OPERATORS}."},
                "value": {"type": "number", "description": "Threshold value."},
                "period": {"type": "string", "description": f"Time window for evaluation. Valid: {_VALID_PERIODS}. Default: day."},
                "type": {"type": "string", "description": f"Alert type. Valid: {_VALID_TYPES}. Default: warning."},
                "frequency": {"type": "string", "description": f"Check frequency. Valid: {_VALID_FREQUENCIES}. Default: daily."},
                "target_group_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Client group IDs to monitor. Omit for all groups.",
                },
                "description": {"type": "string", "description": "Optional description."},
            },
            "required": ["name", "metric", "operator", "value"],
        },
        executor=create_alert,
    )

    registry.register(
        name="update_alert",
        description=(
            "Update an existing alert. Can change name, type, frequency, status (active/paused), condition, "
            "target groups, or snooze it for a number of hours. Only provide fields you want to change."
        ),
        parameters={
            "type": "object",
            "properties": {
                "alert_id": {"type": "string", "description": "The alert ID to update (required)."},
                "name": {"type": "string", "description": "New alert name."},
                "status": {"type": "string", "description": "New status: active or paused."},
                "type": {"type": "string", "description": f"New type. Valid: {_VALID_TYPES}."},
                "frequency": {"type": "string", "description": f"New frequency. Valid: {_VALID_FREQUENCIES}."},
                "metric": {"type": "string", "description": f"New metric. Valid: {_VALID_METRICS}."},
                "operator": {"type": "string", "description": f"New operator. Valid: {_VALID_OPERATORS}."},
                "value": {"type": "number", "description": "New threshold value."},
                "period": {"type": "string", "description": f"New period. Valid: {_VALID_PERIODS}."},
                "target_group_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "New target group IDs.",
                },
                "description": {"type": "string", "description": "New description."},
                "snooze_hours": {"type": "integer", "description": "Snooze alert for this many hours."},
            },
            "required": ["alert_id"],
        },
        executor=update_alert,
    )
