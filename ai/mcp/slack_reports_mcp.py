"""
ai/mcp/slack_reports_mcp.py
----------------------------
Tools for the owner's Slack reports (Hub -> Reports): list them (read), create, change or delete one (write), and
post a test of one to Slack (live, two-step). They use the same request models, validation and writes as the
dashboard routes (services/slack_report_ops.py), and the test send renders with the same function the real send
posts with, so the preview has the same figures and layout as what lands in Slack.
"""

from datetime import datetime, timezone

from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import slack_report_ops as ops
from services import slack_reports as sr

NOT_CONNECTED = "Connect Slack in Birdy first (Settings → Integrations)."
NO_CHANNEL = "This report has no Slack channel and no default Birdy channel is set. Pick a channel for it in Birdy first."

_TRIGGER_HELP = (
    'trigger is {"type": "schedule", "frequency": "daily" | "weekly", "day": "Monday".."Sunday" (weekly only), '
    '"time": "HH:MM" (24h), "timezone": optional IANA name such as "Europe/London"} or '
    '{"type": "event", "event": "alert_fired" | "metric_created" | "account_created", "alert_id": optional, only for alert_fired}.'
)
_TEMPLATE_HELP = (
    'template is {"frequency": "daily" | "weekly" (the data window), "blocks": [Slack blocks with Birdy placeholders such as {{spend}}]}. '
    "Leave out blocks to keep the report's current content (or start from the standard brief)."
)


def _db():
    return get_db(get_shared_mongo_client())


def _body(**fields) -> ops.ReportBody:
    """The router's own request model, built from the tool's arguments; a bad value is a clear tool error."""
    try:
        return ops.ReportBody(**{k: v for k, v in fields.items() if v is not None})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors())
        raise ToolError(f"Invalid report: {problems}")


def _detail(e: ops.ReportError) -> str:
    d = e.detail
    if isinstance(d, dict) and d.get("errors"):
        return "The report content isn't valid: " + "; ".join(str(x) for x in d["errors"])
    return str(d)


async def _find(db, user_id: str, report_id: str) -> dict:
    try:
        return await ops.get_report(db, user_id, report_id)
    except ops.ReportError as e:
        raise ToolError(_detail(e))


async def _slack_connected(db, user_id: str) -> bool:
    user = await db["users"].find_one({"user_id": user_id}, {"integrations.slack_bot.bot_token_encrypted": 1}) or {}
    return bool(((user.get("integrations") or {}).get("slack_bot") or {}).get("bot_token_encrypted"))


async def _channel_label(db, user_id: str, report: dict, channel_id: str) -> str:
    if report.get("channel_id") and report.get("channel_name"):
        return report["channel_name"]
    if not report.get("channel_id"):
        user = await db["users"].find_one({"user_id": user_id}, {"integrations.slack_bot.notify_channel_name": 1}) or {}
        name = ((user.get("integrations") or {}).get("slack_bot") or {}).get("notify_channel_name")
        if name:
            return name
    return channel_id


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@mcp.tool(**READ)
async def list_slack_reports() -> dict:
    """The owner's Slack reports, the default daily/weekly brief first: each one's id, name, whether it is on, when it sends (a schedule or an event), where it is sent (`sends_to`: the report's own channel, or the account's default Birdy channel when channel_id is empty), its content template and a one-line summary of each block. Also returns `max`, how many reports an account can have.

    Use it to find a report's id before update_slack_report, delete_slack_report or send_test_slack_report, or to answer "what do I get in Slack and when?".
    """
    db, user_id = _db(), _current_user_id()
    out = await ops.list_reports(db, user_id)
    # A report with no channel of its own goes to the account's default Birdy channel; its channel_id
    # is empty, which read as "sent nowhere".
    user = await db["users"].find_one({"user_id": user_id}, {"integrations.slack_bot.notify_channel_id": 1,
                                                             "integrations.slack_bot.notify_channel_name": 1}) or {}
    bot = (user.get("integrations") or {}).get("slack_bot") or {}
    for report in out["reports"]:
        own = report.get("channel_id")
        report["sends_to"] = {"channel_id": own or bot.get("notify_channel_id"),
                              "channel_name": report.get("channel_name") if own else bot.get("notify_channel_name"),
                              "default_channel": not own}
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@mcp.tool(**WRITE)
async def create_slack_report(name: str, trigger: dict, channel_id: str | None = None, channel_name: str | None = None,
                              enabled: bool = True, template: dict | None = None) -> dict:
    """Create a Slack report: a message Birdy posts to Slack on a schedule or when something happens. Returns the new report. Nothing is posted by creating it; use send_test_slack_report to see it in Slack.

    Same rules as Hub -> Reports: a name (80 characters at most), a trigger, and an optional channel. Without channel_id the report goes to the account's default Birdy channel. An account can have a limited number of reports (see `max` in list_slack_reports).

    Args:
        name: What the owner calls the report.
        trigger: When it sends. {trigger_help}
        channel_id: Slack channel id (starts with C, G or D). Leave out to use the default Birdy channel.
        channel_name: The channel's name, for display.
        enabled: Whether it sends (default true).
        template: What it says. {template_help}
    """
    body = _body(name=name, trigger=trigger, channel_id=channel_id, channel_name=channel_name, enabled=enabled,
                 template=template)
    try:
        return await ops.create_report(_db(), _current_user_id(), body)
    except ops.ReportError as e:
        raise ToolError(_detail(e))


create_slack_report.description = create_slack_report.description.format(trigger_help=_TRIGGER_HELP,
                                                                         template_help=_TEMPLATE_HELP)


@mcp.tool(**WRITE)
async def update_slack_report(report_id: str, name: str | None = None, enabled: bool | None = None,
                              trigger: dict | None = None, channel_id: str | None = None,
                              channel_name: str | None = None, template: dict | None = None) -> dict:
    """Change a Slack report. Only the fields you pass change; everything else stays as it was. Returns the updated report. Nothing is posted.

    The default daily/weekly brief can be renamed, switched off, re-timed or re-written, but always runs on a schedule. Pass channel_id as an empty string to send to the default Birdy channel again.

    Args:
        report_id: The report's id (from list_slack_reports).
        name: New name (80 characters at most).
        enabled: Turn the report on or off.
        trigger: New timing. {trigger_help}
        channel_id: New Slack channel id (starts with C, G or D), or "" to clear it.
        channel_name: The new channel's name, for display (used with channel_id).
        template: New content. {template_help}
    """
    body = _body(name=name, enabled=enabled, trigger=trigger, channel_id=channel_id, channel_name=channel_name,
                 template=template)
    try:
        return await ops.update_report(_db(), _current_user_id(), report_id, body)
    except ops.ReportError as e:
        raise ToolError(_detail(e))


update_slack_report.description = update_slack_report.description.format(trigger_help=_TRIGGER_HELP,
                                                                         template_help=_TEMPLATE_HELP)


@mcp.tool(**WRITE)
async def delete_slack_report(report_id: str) -> dict:
    """Delete a Slack report and its send history. Returns {"ok": true}. The default daily/weekly brief can't be deleted; turn it off with update_slack_report instead.

    Args:
        report_id: The report's id (from list_slack_reports).
    """
    db = _db()
    user_id = _current_user_id()
    try:
        return await ops.delete_report(db, user_id, report_id)
    except ops.ReportError as e:
        raise ToolError(_detail(e))


# ---------------------------------------------------------------------------
# Live (two steps)
# ---------------------------------------------------------------------------

@mcp.tool(**LIVE)
async def send_test_slack_report(report_id: str, confirm_code: str | None = None) -> dict:
    """Post a Slack report to its channel right now as a test, with today's real figures (event reports get sample event details). It is not recorded as a run and doesn't change what the next scheduled send compares against.

    Two steps. Called without confirm_code it posts nothing and returns a preview (the report's name, the Slack channel and the message text with today's figures; a one-line Birdy take, if the report has one, is written only when it's sent) and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same report_id. If Slack isn't connected, the report has no channel to send to or has nothing to send, you get an error and no code.

    Args:
        report_id: The report's id (from list_slack_reports).
        confirm_code: The code from the preview, once the user has agreed.
    """
    user_id = _current_user_id()
    db = _db()
    report = await _find(db, user_id, report_id)
    if not confirm_code:
        # Refuse now if it can't work, so the user is never asked to confirm something that will fail.
        if not await _slack_connected(db, user_id):
            raise ToolError(NOT_CONNECTED)

    async def preview():
        built = await ops.render_test(db, report)
        if "skipped" in built:
            raise ToolError(NO_CHANNEL if "no channel" in built["skipped"] else f"Nothing to send: {built['skipped']}.")
        return {"report_id": report_id, "name": report.get("name"),
                "channel": await _channel_label(db, user_id, report, built["channel"]),
                "channel_id": built["channel"], "message": built["fallback"]}

    async def execute():
        if not await _slack_connected(db, user_id):
            raise ToolError(NOT_CONNECTED)
        res = await ops.send_test(db, report, with_channel=True)
        channel = res.pop("channel")
        if res["status"] != sr.SENT:
            return {"ok": False, **res, "error": res["error"] or "The report was not sent."}
        label = await _channel_label(db, user_id, report, channel or "")
        return {"ok": True, **res, "summary": f"Posted the report to #{label}."}

    # What the preview showed: a channel or template edited after it needs a fresh yes.
    state = {k: report.get(k) for k in ("name", "channel_id", "template", "trigger")}
    return await two_step(db, user_id, "send_test_slack_report", {"report_id": report_id}, confirm_code, preview, execute,
                          state=state)
