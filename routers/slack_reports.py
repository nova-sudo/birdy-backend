"""
routers/slack_reports.py
------------------------
The account's Slack reports (services/slack_reports.py), for Hub → Reports.

    GET    /api/slack-reports             every report, the default (the brief) first
    POST   /api/slack-reports             create one
    PATCH  /api/slack-reports/{id}        change name, trigger, channel, on/off or content
    DELETE /api/slack-reports/{id}        delete one (not the default — turn it off instead)
    POST   /api/slack-reports/{id}/test   post it now, with sample event details

Content is a template the report copilot designs (routers/report_designer.py):
the UI seeds the copilot's draft with a report's template and saves the result
back here. Editing the default report writes the brief too, so onboarding and
Settings keep showing what will be sent.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import report_blocks as rt
from services import slack_reports as sr
from services.report_events import describe_event

logger = logging.getLogger(__name__)

router = APIRouter()

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_CHANNEL_RE = re.compile(r"^[CGD][A-Z0-9]{2,30}$")

SAMPLE_EVENT = {"alert_id": None, "alert_name": "Cost per lead above target",
                "alert_message": "Triggered for: Glow Clinic", "account_name": "Glow Clinic",
                "metric_name": "Cost per booked call"}


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


def _clean_trigger(t: Trigger) -> dict:
    if t.type == "schedule":
        if t.frequency not in rt.FREQUENCIES:
            raise HTTPException(400, "Choose every day or weekly")
        if not t.time or not _TIME_RE.match(t.time):
            raise HTTPException(400, "Time must be HH:MM")
        day = None
        if t.frequency == "weekly":
            day = (t.day or "").strip().title()
            if day not in rt.WEEKDAYS:
                raise HTTPException(400, "Choose a weekday")
        if t.timezone:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(t.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise HTTPException(400, "Unknown timezone")
        return {"type": "schedule", "frequency": t.frequency, "day": day, "time": t.time,
                "timezone": t.timezone or None}
    if t.event not in sr.EVENTS:
        raise HTTPException(400, f"event must be one of {', '.join(sr.EVENTS)}")
    return {"type": "event", "event": t.event,
            "alert_id": (t.alert_id or None) if t.event == "alert_fired" else None}


def _standard_blocks(trigger: dict | None) -> list:
    """The standard brief. An event report leads with what happened instead of
    the brief's title, or its message would read like any other day's brief."""
    blocks = rt.default_template()["blocks"]
    if (trigger or {}).get("type") != "event":
        return blocks
    lead = [{"type": "header", "text": {"type": "plain_text", "text": "{{event}}"}}]
    if trigger.get("event") == "alert_fired":
        lead.append({"type": "section", "text": {"type": "mrkdwn", "text": "{{alert_message}}"}})
    return lead + blocks[1:]


async def _clean_template(db, user_id: str, raw: dict, blocks: list | None = None,
                          trigger: dict | None = None) -> dict:
    """A validated template. Without blocks of its own it keeps `blocks` (the
    report's current content), or starts from the standard brief."""
    if not raw.get("blocks"):
        raw = {**raw, "blocks": blocks or _standard_blocks(trigger)}
    names = {g["id"]: g.get("name") or "" async for g in db["client_groups"].find(
        {"user_id": user_id}, {"_id": 0, "id": 1, "name": 1}) if g.get("id")}
    try:
        return sr.template_of(rt.validate(raw, names))
    except rt.TemplateError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors})


def _out(report: dict) -> dict:
    out = {k: v for k, v in report.items() if k != "_id"}
    out["summary"] = rt.describe(report.get("template") or {})
    return out


async def _get(db, user_id: str, report_id: str) -> dict:
    report = await db[sr.REPORTS].find_one({"id": report_id, "user_id": user_id}, {"_id": 0})
    if not report:
        raise HTTPException(404, "Report not found")
    return report


async def _write_brief(db, report: dict) -> None:
    """The default report is the brief: keep the brief the screens read in step."""
    brief = {**sr.brief_of(report), "updated_at": datetime.utcnow()}
    await db["users"].update_one(
        {"user_id": report["user_id"], "integrations.slack_bot": {"$exists": True}},
        {"$set": {"integrations.slack_bot.brief": brief}},
    )


@router.get("/api/slack-reports")
async def list_reports(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        if not await db[sr.REPORTS].find_one({"user_id": current_user, "is_default": True}, {"_id": 1}):
            await sr.sync_default_from_brief(db, current_user)
        reports = await db[sr.REPORTS].find({"user_id": current_user}, {"_id": 0}).to_list(sr.MAX_REPORTS + 5)
        reports.sort(key=lambda r: (not r.get("is_default"), r.get("created_at") or datetime.min))
        return {"reports": [_out(r) for r in reports], "max": sr.MAX_REPORTS}


@router.post("/api/slack-reports")
async def create_report(body: ReportBody, current_user: str = Depends(get_current_user)):
    if not (body.name or "").strip():
        raise HTTPException(400, "Give the report a name")
    if body.trigger is None:
        raise HTTPException(400, "Choose when the report sends")
    if body.channel_id and not _CHANNEL_RE.match(body.channel_id):
        raise HTTPException(400, "Unknown Slack channel")
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        if await db[sr.REPORTS].count_documents({"user_id": current_user}) >= sr.MAX_REPORTS:
            raise HTTPException(400, f"You can have up to {sr.MAX_REPORTS} reports")
        trigger = _clean_trigger(body.trigger)
        template = await _clean_template(db, current_user, body.template or {"frequency": "daily"}, trigger=trigger)
        report = sr.new_report(current_user, name=body.name.strip(), trigger=trigger, template=template,
                               channel_id=body.channel_id, channel_name=body.channel_name,
                               enabled=True if body.enabled is None else body.enabled)
        await db[sr.REPORTS].insert_one(dict(report))
        return {"report": _out(report)}


@router.patch("/api/slack-reports/{report_id}")
async def update_report(report_id: str, body: ReportBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        report = await _get(db, current_user, report_id)
        changes: dict = {}
        if body.name is not None:
            if not body.name.strip():
                raise HTTPException(400, "Give the report a name")
            changes["name"] = body.name.strip()
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.channel_id is not None:
            if body.channel_id and not _CHANNEL_RE.match(body.channel_id):
                raise HTTPException(400, "Unknown Slack channel")
            changes["channel_id"] = body.channel_id or None
            changes["channel_name"] = body.channel_name if body.channel_id else None
        if body.trigger is not None:
            trigger = _clean_trigger(body.trigger)
            if report.get("is_default") and trigger["type"] != "schedule":
                raise HTTPException(400, "The daily/weekly brief always runs on a schedule")
            changes["trigger"] = trigger
        if body.template is not None:
            changes["template"] = await _clean_template(db, current_user, body.template,
                                                        (report.get("template") or {}).get("blocks"))
        if not changes:
            return {"report": _out(report)}

        changes["updated_at"] = datetime.utcnow()
        await db[sr.REPORTS].update_one({"id": report_id, "user_id": current_user}, {"$set": changes})
        report = {**report, **changes}
        if report.get("is_default") and ("trigger" in changes or "template" in changes):
            await _write_brief(db, report)
        return {"report": _out(report)}


@router.delete("/api/slack-reports/{report_id}")
async def delete_report(report_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        report = await _get(db, current_user, report_id)
        if report.get("is_default"):
            raise HTTPException(400, "The daily/weekly brief can't be deleted — turn it off instead")
        await db[sr.REPORTS].delete_one({"id": report_id, "user_id": current_user})
        await db[sr.RUNS].delete_many({"report_id": report_id})
        return {"ok": True}


@router.post("/api/slack-reports/{report_id}/test")
async def test_report(report_id: str, current_user: str = Depends(get_current_user)):
    """Post the report now. Event reports get sample event details. Not recorded as a run."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        report = await _get(db, current_user, report_id)
        trigger = report.get("trigger") or {}
        event = describe_event(trigger["event"], SAMPLE_EVENT) if trigger.get("type") == "event" else None
        status, error = await sr.send_report(db, report, datetime.now(timezone.utc),
                                             key=datetime.now(timezone.utc).date().isoformat(), event=event,
                                             record=False)
        return {"status": status, "error": error}
