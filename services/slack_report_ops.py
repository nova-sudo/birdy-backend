"""
services/slack_report_ops.py
----------------------------
What the Slack reports API does (routers/slack_reports.py) and what the MCP
tools do (ai/mcp/slack_reports_mcp.py): the same request models, the same
validation and the same writes, in one place. Problems raise ReportError with
the HTTP status and detail the router returns, so both callers refuse alike.
"""

import re
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

from services import report_blocks as rt
from services import slack_reports as sr
from services.report_events import describe_event

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{2,30}$")

SAMPLE_EVENT = {"alert_id": None, "alert_name": "Cost per lead above target",
                "alert_message": "Triggered for: Glow Clinic", "account_name": "Glow Clinic",
                "metric_name": "Cost per booked call"}


class ReportError(Exception):
    def __init__(self, status_code: int, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class Trigger(BaseModel):
    type: Literal["schedule", "event"]
    frequency: Optional[Literal["daily", "weekly"]] = None
    day: Optional[str] = None
    time: Optional[str] = None
    timezone: Optional[str] = None
    event: Optional[str] = None
    alert_id: Optional[str] = None


class ReportBody(BaseModel):
    name: Optional[str] = Field(default=None, max_length=80)
    enabled: Optional[bool] = None
    trigger: Optional[Trigger] = None
    channel_id: Optional[str] = None
    channel_name: Optional[str] = Field(default=None, max_length=120)
    template: Optional[dict] = None   # {frequency: data window, blocks}; no blocks = keep / standard


def clean_trigger(t: Trigger) -> dict:
    if t.type == "schedule":
        if t.frequency not in rt.FREQUENCIES:
            raise ReportError(400, "Choose every day or weekly")
        if not t.time or not _TIME_RE.match(t.time):
            raise ReportError(400, "Time must be HH:MM")
        day = None
        if t.frequency == "weekly":
            day = (t.day or "").strip().title()
            if day not in rt.WEEKDAYS:
                raise ReportError(400, "Choose a weekday")
        if t.timezone:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(t.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ReportError(400, "Unknown timezone")
        return {"type": "schedule", "frequency": t.frequency, "day": day, "time": t.time,
                "timezone": t.timezone or None}
    if t.event not in sr.EVENTS:
        raise ReportError(400, f"event must be one of {', '.join(sr.EVENTS)}")
    return {"type": "event", "event": t.event,
            "alert_id": (t.alert_id or None) if t.event == "alert_fired" else None}


def standard_blocks(trigger: dict | None) -> list:
    """The standard brief. An event report leads with what happened instead of
    the brief's title, or its message would read like any other day's brief."""
    blocks = rt.default_template()["blocks"]
    if (trigger or {}).get("type") != "event":
        return blocks
    lead = [{"type": "header", "text": {"type": "plain_text", "text": "{{event}}"}}]
    if trigger.get("event") == "alert_fired":
        lead.append({"type": "section", "text": {"type": "mrkdwn", "text": "{{alert_message}}"}})
    return lead + blocks[1:]


async def clean_template(db, user_id: str, raw: dict, blocks: list | None = None,
                         trigger: dict | None = None) -> dict:
    """A validated template. Without blocks of its own it keeps `blocks` (the
    report's current content), or starts from the standard brief."""
    if not raw.get("blocks"):
        raw = {**raw, "blocks": blocks or standard_blocks(trigger)}
    names = {g["id"]: g.get("name") or "" async for g in db["client_groups"].find(
        {"user_id": user_id}, {"_id": 0, "id": 1, "name": 1}) if g.get("id")}
    try:
        return sr.template_of(rt.validate(raw, names))
    except rt.TemplateError as e:
        raise ReportError(400, {"errors": e.errors})


def present(report: dict) -> dict:
    out = {k: v for k, v in report.items() if k != "_id"}
    out["summary"] = rt.describe(report.get("template") or {})
    return out


async def get_report(db, user_id: str, report_id: str) -> dict:
    report = await db[sr.REPORTS].find_one({"id": report_id, "user_id": user_id}, {"_id": 0})
    if not report:
        raise ReportError(404, "Report not found")
    return report


async def _write_brief(db, report: dict) -> None:
    """The default report is the brief: keep the brief the screens read in step."""
    brief = {**sr.brief_of(report), "updated_at": datetime.utcnow()}
    await db["users"].update_one(
        {"user_id": report["user_id"], "integrations.slack_bot": {"$exists": True}},
        {"$set": {"integrations.slack_bot.brief": brief}},
    )


async def list_reports(db, user_id: str) -> dict:
    if not await db[sr.REPORTS].find_one({"user_id": user_id, "is_default": True}, {"_id": 1}):
        await sr.sync_default_from_brief(db, user_id)
    reports = await db[sr.REPORTS].find({"user_id": user_id}, {"_id": 0}).to_list(sr.MAX_REPORTS + 5)
    reports.sort(key=lambda r: (not r.get("is_default"), r.get("created_at") or datetime.min))
    return {"reports": [present(r) for r in reports], "max": sr.MAX_REPORTS}


async def create_report(db, user_id: str, body: ReportBody) -> dict:
    if not (body.name or "").strip():
        raise ReportError(400, "Give the report a name")
    if body.trigger is None:
        raise ReportError(400, "Choose when the report sends")
    if body.channel_id and not _CHANNEL_RE.match(body.channel_id):
        raise ReportError(400, "Unknown Slack channel")
    if await db[sr.REPORTS].count_documents({"user_id": user_id}) >= sr.MAX_REPORTS:
        raise ReportError(400, f"You can have up to {sr.MAX_REPORTS} reports")
    trigger = clean_trigger(body.trigger)
    template = await clean_template(db, user_id, body.template or {"frequency": "daily"}, trigger=trigger)
    report = sr.new_report(user_id, name=body.name.strip(), trigger=trigger, template=template,
                           channel_id=body.channel_id, channel_name=body.channel_name,
                           enabled=True if body.enabled is None else body.enabled)
    await db[sr.REPORTS].insert_one(dict(report))
    return {"report": present(report)}


async def update_report(db, user_id: str, report_id: str, body: ReportBody) -> dict:
    report = await get_report(db, user_id, report_id)
    changes: dict = {}
    if body.name is not None:
        if not body.name.strip():
            raise ReportError(400, "Give the report a name")
        changes["name"] = body.name.strip()
    if body.enabled is not None:
        changes["enabled"] = body.enabled
    if body.channel_id is not None:
        if body.channel_id and not _CHANNEL_RE.match(body.channel_id):
            raise ReportError(400, "Unknown Slack channel")
        changes["channel_id"] = body.channel_id or None
        changes["channel_name"] = body.channel_name if body.channel_id else None
    if body.trigger is not None:
        trigger = clean_trigger(body.trigger)
        if report.get("is_default") and trigger["type"] != "schedule":
            raise ReportError(400, "The daily/weekly brief always runs on a schedule")
        changes["trigger"] = trigger
    if body.template is not None:
        changes["template"] = await clean_template(db, user_id, body.template,
                                                   (report.get("template") or {}).get("blocks"))
    if not changes:
        return {"report": present(report)}

    changes["updated_at"] = datetime.utcnow()
    await db[sr.REPORTS].update_one({"id": report_id, "user_id": user_id}, {"$set": changes})
    report = {**report, **changes}
    if report.get("is_default") and ("trigger" in changes or "template" in changes):
        await _write_brief(db, report)
    return {"report": present(report)}


async def delete_report(db, user_id: str, report_id: str) -> dict:
    report = await get_report(db, user_id, report_id)
    if report.get("is_default"):
        raise ReportError(400, "The daily/weekly brief can't be deleted — turn it off instead")
    await db[sr.REPORTS].delete_one({"id": report_id, "user_id": user_id})
    await db[sr.RUNS].delete_many({"report_id": report_id})
    return {"ok": True}


def _test_args(report: dict) -> dict:
    """Event reports get sample event details. Not recorded as a run."""
    trigger = report.get("trigger") or {}
    event = describe_event(trigger["event"], SAMPLE_EVENT) if trigger.get("type") == "event" else None
    now = datetime.now(timezone.utc)
    return {"now_utc": now, "key": now.date().isoformat(), "event": event}


async def render_test(db, report: dict) -> dict:
    """The message a test send would post (see sr.render_report)."""
    return await sr.render_report(db, report, **_test_args(report))


async def send_test(db, report: dict) -> dict:
    status, error = await sr.send_report(db, report, record=False, **_test_args(report))
    return {"status": status, "error": error}
