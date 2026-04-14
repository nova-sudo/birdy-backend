"""Slack notification service — sends messages to user-provided Incoming Webhooks."""

import logging
import re
from datetime import datetime

import httpx

from core.constants import METRIC_LABELS, OPERATOR_LABELS

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_REGEX = re.compile(
    r"^https://hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+$"
)


def is_valid_webhook_url(url: str) -> bool:
    """Validate that a URL looks like a Slack Incoming Webhook."""
    return bool(url and SLACK_WEBHOOK_REGEX.match(url.strip()))


async def post_to_webhook(webhook_url: str, payload: dict) -> tuple[bool, str]:
    """Post a payload to a Slack webhook. Returns (success, error_message)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=payload)
            if resp.status_code == 200 and resp.text == "ok":
                return True, ""
            return False, f"Slack returned {resp.status_code}: {resp.text[:200]}"
    except httpx.TimeoutException:
        return False, "Slack webhook request timed out"
    except Exception as e:
        return False, f"Slack error: {str(e)[:200]}"


async def send_test_message(webhook_url: str) -> tuple[bool, str]:
    """Send a confirmation message when a user first connects Slack."""
    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "🐦 Birdy connected to Slack"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "You'll receive alert notifications in this channel when your alerts trigger.",
                },
            },
        ],
        "text": "🐦 Birdy connected to Slack",
    }
    return await post_to_webhook(webhook_url, payload)


def _format_condition(alert: dict) -> str:
    """Render the alert condition in human-readable form."""
    cond = alert.get("condition", {}) or {}
    metric = cond.get("metric", "")
    operator = cond.get("operator", "")
    value = cond.get("value", 0)
    period = cond.get("period", "week")

    metric_label = METRIC_LABELS.get(metric, metric)
    if metric.startswith("tag:"):
        metric_label = f"Tag: {metric[4:]}"
    op_label = OPERATOR_LABELS.get(operator, operator)
    return f"{metric_label} {op_label} {value} (over {period})"


async def send_alert_triggered(webhook_url: str, alert: dict, eval_result: dict) -> tuple[bool, str]:
    """Send a formatted alert notification to Slack when an alert triggers."""
    name = alert.get("name", "Unnamed Alert")
    description = alert.get("description") or ""
    groups = alert.get("target_group_names") or []
    groups_text = ", ".join(groups) if groups else "All groups"
    alert_type = alert.get("type", "warning")

    icon = "🏆" if alert_type == "win" else "🚨"
    color = "#10b981" if alert_type == "win" else "#ef4444"

    current_value = eval_result.get("current_value", 0)
    message = eval_result.get("message", "")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{icon} {name}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Condition:*\n{_format_condition(alert)}"},
                {"type": "mrkdwn", "text": f"*Current value:*\n{current_value}"},
                {"type": "mrkdwn", "text": f"*Groups:*\n{groups_text}"},
                {"type": "mrkdwn", "text": f"*Triggered at:*\n{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"},
            ],
        },
    ]

    if message:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"> {message}"},
        })

    if description:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": description}],
        })

    payload = {
        "attachments": [{
            "color": color,
            "blocks": blocks,
            "fallback": f"{icon} {name}: {message or _format_condition(alert)}",
        }],
        "text": f"{icon} {name}: {message or _format_condition(alert)}",
    }

    return await post_to_webhook(webhook_url, payload)


async def get_user_webhook(user_id: str, mongo_client) -> str | None:
    """Fetch a user's stored Slack webhook URL, if any."""
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    user = await db["users"].find_one(
        {"user_id": user_id},
        {"integrations.slack.webhook_url": 1, "_id": 0},
    )
    if not user:
        return None
    return user.get("integrations", {}).get("slack", {}).get("webhook_url")
