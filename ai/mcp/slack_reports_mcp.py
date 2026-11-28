"""
ai/mcp/slack_reports_mcp.py
----------------------------
Tools for the owner's Slack reports (Hub -> Reports): list them (read), create, change or delete one (write), and
post a test of one to Slack (live, two-step). They use the same request models, validation and writes as the
dashboard routes (services/slack_report_ops.py), and the test send renders with the same function the real send
posts with, so the preview has the same figures and layout as what lands in Slack.

Every report belongs to one agent (its `owner`). Called from an agent's chat run, the tools are scoped to the
calling agent, read from the run's signed token (server.current_agent), never from an argument: Marketing Guru and
Sales Coach see and change only their own reports, and what they create is theirs. Birdy changes only hers but
lists everyone's, labelled, so she can send the owner to the right agent. An outside assistant (no agent in its
token) is the owner and sees and changes every report, as before.

What an agent's report shows must be in its area (services/report_owner.py): Birdy makes client-level
reports only, campaign / ad set / ad / creative level is Marketing Guru's, calls are Sales Coach's, and
client-level totals may sit beside either's own figures. Outside its area create and update refuse,
saying which agent to ask, before anything is saved or previewed. The owner's builder is not held to it.
"""

from datetime import datetime, timezone

from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_agent as _current_agent, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import report_owner
from services import report_proposals
from services.report_drafts import preview_payload
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
    "Leave out blocks to keep the report's current content (or start from the standard brief). "
    "Numbers come ONLY from {{placeholders}} and birdy_data, filled with live figures at every send: never type a figure "
    "into the template, not even one you just read with a tool (it would be posted unchanged every day). Tables are a "
    "data_table block with birdy_data, never a Markdown pipe table; charts are a data_visualization block with birdy_data. "
    "Call list_report_metrics for this account's metric names and the block shapes. Examples: "
    '{"type": "section", "fields": [{"type": "mrkdwn", "text": "*Leads*\\n{{leads}} {{leads show=change}}"}]}; '
    '{"type": "data_table", "caption": "Clients", "birdy_data": {"metrics": ["spend", "leads", "cpl"], "sort": "spend", "total_row": true}}; '
    'yesterday vs the day before per client: {"type": "data_visualization", "title": "Leads: yesterday vs day before", '
    '"birdy_data": {"chart": "bar", "by": "client", "metrics": ["leads"], "compare": "previous"}}; '
    'a calls / sales-team table (Sales Coach): {"type": "data_table", "caption": "Sales team", "birdy_data": {"metrics": '
    '["booked", "closed", "speed_to_lead", "speed_within_target", "hp_total_calls"], "sort": "booked"}}. '
    "If the tool refuses the template, its error lists what is wrong and the valid metric names: fix it and call again "
    "yourself before saying anything to the owner."
)


_NAMES, _ASK = report_owner.NAMES, report_owner.ASK


def _db():
    return get_db(get_shared_mongo_client())


def _caller() -> str | None:
    """The calling agent when it owns reports, else None (an outside assistant: the owner, unscoped)."""
    agent = _current_agent()
    return agent if agent in sr.OWNERS else None


def _not_yours(caller: str, owner: str) -> ToolError:
    """Refused, with where to go instead: the report's own agent changes it."""
    ask = _ASK[owner] if caller == "birdy" or owner == "birdy" else "ask_birdy"
    return ToolError(f"That is {_NAMES[owner]}'s report, so only {_NAMES[owner]} can change or send it. "
                     f"Ask it with {ask}; don't send or change one of your own reports instead.")


def _body(**fields) -> ops.ReportBody:
    """The router's own request model, built from the tool's arguments; a bad value is a clear tool error."""
    try:
        return ops.ReportBody(**{k: v for k, v in fields.items() if v is not None})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in e.errors())
        raise ToolError(f"Invalid report: {problems}")


def _detail(e: ops.ReportError) -> str:
    return report_proposals.error_text(e)


def _check_area(caller: str | None, name: str | None, template: dict | None) -> None:
    """Refuse an agent a report outside its area (services/report_owner.py), before anything is saved or
    previewed, so the owner is never asked to confirm a report that would be refused. An outside
    assistant (no agent) is the owner and picks any content, as in the report builder."""
    if caller:
        why = report_owner.refusal(caller, report_owner.report_area(template, name))
        if why:
            raise ToolError(why)


async def _find(db, user_id: str, report_id: str) -> dict:
    """The report, when the calling agent may change it (its own, or any for an outside assistant)."""
    try:
        report = await ops.get_report(db, user_id, report_id)
    except ops.ReportError as e:
        raise ToolError(_detail(e))
    caller = _caller()
    if caller and sr.owner_of(report) != caller:
        raise _not_yours(caller, sr.owner_of(report))
    return report


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
    """The owner's Slack reports, the default daily/weekly brief first: each one's id, name, `owner` (the agent whose report it is: birdy, media_buyer or sales_coach) and `owner_name`, whether it is on, when it sends (a schedule or an event), where it is sent (`sends_to`: the report's own channel, or the account's default Birdy channel when channel_id is empty), its content template and a one-line summary of each block. Also returns `max`, how many reports an account can have (every agent's count toward it).

    In an agent's own chat you see your own reports (Birdy sees every agent's, labelled by owner, and changes only her own). Use it to find a report's id before update_slack_report, delete_slack_report or send_test_slack_report, or to answer "what do I get in Slack and when?".
    """
    db, user_id = _db(), _current_user_id()
    caller = _caller()
    out = await ops.list_reports(db, user_id, owner=caller if caller and caller != "birdy" else None)
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
        report["owner_name"] = _NAMES[report["owner"]]
    return out


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@mcp.tool(**WRITE)
async def create_slack_report(name: str, trigger: dict, channel_id: str | None = None, channel_name: str | None = None,
                              enabled: bool = True, template: dict | None = None,
                              confirm_code: str | None = None) -> dict:
    """Create a Slack report: a message Birdy posts to Slack on a schedule or when something happens. Nothing is posted by creating it; use send_test_slack_report to see it in Slack. In an agent's own chat the report is that agent's (its `owner`) and appears on its Reports tab.

    Two steps. Called without confirm_code it saves nothing and returns a preview (the report's name, when and where it sends, and the message text with today's figures) and a one-time confirm_code. Show the preview to the user and only call again, with the same arguments plus confirm_code, after they say yes; that call returns the new report. In an agent's chat in Birdy there is no code: the call proposes the report, with a live preview, for the owner to approve below your answer.

    It must be in the calling agent's area: Agency Ops (Birdy) makes client-level reports only (a client's or the agency's spend, leads, CPL, opportunities, deals won); anything at campaign, ad set, ad or creative level is Marketing Guru's; anything about calls (booked, closed, speed to lead, call stats, recordings, setters) is Sales Coach's. Marketing Guru and Sales Coach may add client-level totals to their own. Outside your area the tool refuses and says which agent to ask.

    Same rules as Hub -> Reports: a name (80 characters at most), a trigger, and an optional channel. Without channel_id the report goes to the account's default Birdy channel. An account can have a limited number of reports (see `max` in list_slack_reports).

    Args:
        name: What the owner calls the report.
        trigger: When it sends. {trigger_help}
        channel_id: Slack channel id (starts with C, G or D). Leave out to use the default Birdy channel.
        channel_name: The channel's name, for display.
        enabled: Whether it sends (default true).
        template: What it says. {template_help}
        confirm_code: The code from the preview, once the user has agreed.
    """
    caller = _caller()
    fields = dict(name=name, trigger=trigger, channel_id=channel_id, channel_name=channel_name, enabled=enabled,
                  template=template)
    _body(**fields, owner=caller)
    _check_area(caller, name, template)
    db, user_id = _db(), _current_user_id()
    if caller:
        # An agent's chat in Birdy (the orchestrator proposes it itself, ai/orchestrator.py): never saved
        # here, only proposed for the owner's approval.
        try:
            proposal = await report_proposals.build(db, user_id, caller, fields)
        except ops.ReportError as e:
            raise ToolError(_detail(e))
        return {"proposed": True, "proposal": proposal, "note": report_proposals.PROPOSED_NOTE}
    owner = sr.DEFAULT_OWNER
    try:
        # Every check now, so the user is never asked to confirm a report that would be refused.
        report = await ops.prepare_report(db, user_id, report_proposals.body_of(owner, fields))
    except ops.ReportError as e:
        raise ToolError(_detail(e))

    async def preview():
        shown = await preview_payload(db, user_id, report["template"])
        return {"name": report["name"], "owner": owner, "owner_name": _NAMES[owner],
                "schedule": ops.schedule_label(report["trigger"]),
                "sends_to": await ops.sends_to(db, user_id, report.get("channel_id"), report.get("channel_name")),
                "message": shown["text"], "sample_figures": bool(shown.get("sample"))}

    async def execute():
        try:
            return await ops.create_report(db, user_id, report_proposals.body_of(owner, fields))
        except ops.ReportError as e:
            raise ToolError(_detail(e))

    return await two_step(db, user_id, "create_slack_report", fields, confirm_code, preview, execute)


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
    db, user_id = _db(), _current_user_id()
    report = await _find(db, user_id, report_id)
    if name is not None or template is not None:
        # What the report would say after the change: a template without blocks keeps the current ones.
        new = template if template and (template.get("blocks") or template.get("body")) else None
        _check_area(_caller(), name if name is not None else report.get("name"),
                    new or {"blocks": (report.get("template") or {}).get("blocks")})
    try:
        return await ops.update_report(db, user_id, report_id, body)
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
    await _find(db, user_id, report_id)
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
