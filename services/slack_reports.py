"""
services/slack_reports.py
-------------------------
Slack reports: any number per account, each a Block Kit template (the same one
the brief uses — services/report_blocks.py) posted to its own channel when its
trigger fires.

Stored in `slack_reports`:

    {id, user_id, name, enabled, is_default,
     trigger:  {type: "schedule", frequency: "daily" | "weekly", day, time: "HH:MM", timezone}
             | {type: "event", event: "alert_fired" | "metric_created" | "account_created", alert_id},
     channel_id, channel_name,
     template: {version: 4, frequency: "daily" | "weekly", blocks: [...]},   # frequency = data window
     last_figures, last_run: {at, key, status, error}, created_at, updated_at}

A schedule's `timezone` may be None: it then follows the account's timezone
(`users.timezone`), as the brief always did.

The brief (`users.integrations.slack_bot.brief`) is the account's *default*
report. Onboarding and Settings still write the brief; every write syncs the
default report (sync_default_from_brief), and accounts that never had one
synced are migrated on the next tick. The scheduler only ever reads reports.

Each send is claimed by inserting a row into `slack_report_runs` with
_id = "<report id>:<key>" — the local date for a schedule, "event:<id>" for an
event (services/report_events.py) — so two ticks racing each other cannot both send. A failed post deletes
its row so the next tick retries; a skipped one (no Slack, no clients) keeps it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, time as dtime, timezone

from pymongo.errors import DuplicateKeyError

from services import report_blocks as rt
from services.slack_brief import (
    DEFAULT_TIMEZONE, FAILED, LATE_LIMIT, SENT, SKIPPED, WEEKDAYS, parse_time, user_zone,
)

logger = logging.getLogger(__name__)

REPORTS = "slack_reports"
RUNS = "slack_report_runs"
MAX_REPORTS = 20
EVENTS = ("alert_fired", "metric_created", "account_created")
DEFAULT_NAMES = {"daily": "Daily brief", "weekly": "Weekly brief"}


async def create_slack_report_indexes(mongo_client):
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db[REPORTS].create_index("id", unique=True)
    await db[REPORTS].create_index([("user_id", 1), ("is_default", 1)])
    await db[REPORTS].create_index([("enabled", 1), ("trigger.type", 1)])
    await db[RUNS].create_index("report_id")
    await db[RUNS].create_index("at", expireAfterSeconds=90 * 24 * 3600)


# ── Times ─────────────────────────────────────────────────────────────────────


def parse_hhmm(value: str | None) -> dtime:
    """'08:30' or the brief's '8:30 AM' → 08:30. Unparseable → 09:00."""
    value = (value or "").strip()
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        return parse_time(value)


def hhmm(t: dtime) -> str:
    return t.strftime("%H:%M")


def brief_time_label(t: dtime) -> str:
    """09:05 → '9:05 AM', the format the brief and its UI use."""
    return t.strftime("%I:%M %p").lstrip("0")


# ── Shape ─────────────────────────────────────────────────────────────────────


def template_of(brief: dict) -> dict:
    """The content half of a v4 brief: the blocks and the data window."""
    return {"version": rt.VERSION, "frequency": brief["frequency"], "blocks": brief.get("blocks") or []}


def trigger_of(brief: dict) -> dict:
    """The schedule half of a v4 brief. Timezone None = follow the account's."""
    return {
        "type": "schedule",
        "frequency": brief["frequency"],
        "day": (brief.get("day") or "Monday") if brief["frequency"] == "weekly" else None,
        "time": hhmm(parse_hhmm(brief.get("time"))),
        "timezone": None,
    }


def brief_of(report: dict) -> dict:
    """A schedule report back in the brief's own shape, for the screens that still read it."""
    trigger = report["trigger"]
    template = report["template"]
    return {
        "version": rt.VERSION,
        "frequency": trigger["frequency"],
        "time": brief_time_label(parse_hhmm(trigger.get("time"))),
        "day": trigger.get("day") if trigger["frequency"] == "weekly" else None,
        "blocks": template.get("blocks") or [],
    }


def new_report(user_id: str, *, name: str, trigger: dict, template: dict,
               channel_id: str | None = None, channel_name: str | None = None,
               enabled: bool = True, is_default: bool = False) -> dict:
    now = datetime.utcnow()
    return {
        "id": uuid.uuid4().hex, "user_id": user_id, "name": name, "enabled": enabled,
        "is_default": is_default, "trigger": trigger, "template": template,
        "channel_id": channel_id, "channel_name": channel_name,
        "last_figures": None, "last_run": None, "created_at": now, "updated_at": now,
    }


# ── The default report (the brief) ────────────────────────────────────────────


async def sync_default_from_brief(db, user_id: str) -> dict | None:
    """
    Make the default report match the account's brief, creating it the first
    time. Returns the report, or None when the account has no brief.

    Only the schedule and the content come from the brief; name, channel and
    on/off are the report's own, so editing the brief never resets them. The
    brief's last-sent day and figures carry over on creation, so switching to
    reports never sends today's brief twice.
    """
    user = await db["users"].find_one(
        {"user_id": user_id},
        {"integrations.slack_bot.brief": 1, "integrations.slack_bot.brief_last_sent": 1,
         "integrations.slack_bot.brief_last_figures": 1},
    ) or {}
    slack_bot = (user.get("integrations") or {}).get("slack_bot") or {}
    brief = rt.normalize(slack_bot.get("brief"))
    if not rt.has_content(brief):
        return None

    trigger, template = trigger_of(brief), template_of(brief)
    existing = await db[REPORTS].find_one({"user_id": user_id, "is_default": True})
    if existing:
        # Keep a timezone the report chose for itself.
        trigger["timezone"] = (existing.get("trigger") or {}).get("timezone")
        await db[REPORTS].update_one(
            {"id": existing["id"]},
            {"$set": {"trigger": trigger, "template": template, "updated_at": datetime.utcnow()}},
        )
        return {**existing, "trigger": trigger, "template": template}

    report = new_report(user_id, name=DEFAULT_NAMES[brief["frequency"]], trigger=trigger,
                        template=template, is_default=True)
    report["last_figures"] = slack_bot.get("brief_last_figures")
    # Upsert on (user, is_default) so two ticks migrating at once make one report.
    result = await db[REPORTS].update_one(
        {"user_id": user_id, "is_default": True}, {"$setOnInsert": report}, upsert=True,
    )
    if not result.upserted_id:
        return await db[REPORTS].find_one({"user_id": user_id, "is_default": True})

    last_sent = slack_bot.get("brief_last_sent")
    if last_sent:
        await claim(db, report, last_sent, status=SENT, migrated=True)
    logger.info("slack reports: migrated the brief of %s into report %s", user_id, report["id"])
    return report


async def migrate_briefs(db) -> int:
    """Create the default report for every account whose brief has none yet."""
    have = set(await db[REPORTS].distinct("user_id", {"is_default": True}))
    migrated = 0
    async for user in db["users"].find(
        {"integrations.slack_bot.brief.frequency": {"$in": list(rt.FREQUENCIES)}}, {"user_id": 1},
    ):
        if user["user_id"] in have:
            continue
        if await sync_default_from_brief(db, user["user_id"]):
            migrated += 1
    return migrated


# ── When a schedule is due ────────────────────────────────────────────────────


def due_key(report: dict, account_tz: str | None, now_utc: datetime) -> str | None:
    """
    The period key (the report's local date, ISO) to send now, or None.

    Due when local time is at or past the chosen time by less than LATE_LIMIT,
    on the chosen weekday for weekly reports. Whether that date was already
    sent is the claim's job, not this one's.
    """
    trigger = report.get("trigger") or {}
    if not report.get("enabled") or trigger.get("type") != "schedule":
        return None
    if trigger.get("frequency") not in rt.FREQUENCIES or not rt.has_content(report.get("template")):
        return None

    local_now = now_utc.astimezone(user_zone(trigger.get("timezone") or account_tz))
    if trigger["frequency"] == "weekly" and WEEKDAYS[local_now.weekday()] != (trigger.get("day") or "Monday"):
        return None

    scheduled = datetime.combine(local_now.date(), parse_hhmm(trigger.get("time")), local_now.tzinfo)
    if not (scheduled <= local_now < scheduled + LATE_LIMIT):
        return None
    return local_now.date().isoformat()


# ── Claims ────────────────────────────────────────────────────────────────────


async def claim(db, report: dict, key: str, *, status: str = "claimed", **extra) -> bool:
    try:
        await db[RUNS].insert_one({
            "_id": f"{report['id']}:{key}", "report_id": report["id"], "user_id": report["user_id"],
            "key": key, "status": status, "at": datetime.utcnow(), **extra,
        })
        return True
    except DuplicateKeyError:
        return False


async def finish(db, report: dict, key: str, status: str, error: str | None = None) -> None:
    now = datetime.utcnow()
    if status == FAILED:
        await db[RUNS].delete_one({"_id": f"{report['id']}:{key}"})  # free it for a retry
    else:
        await db[RUNS].update_one({"_id": f"{report['id']}:{key}"},
                                  {"$set": {"status": status, "at": now, "error": error}})
    fields = {"last_run": {"at": now, "key": key, "status": status, "error": error}}
    if status == SENT:
        fields["last_sent_at"] = now
    await db[REPORTS].update_one({"id": report["id"]}, {"$set": fields})


# ── Sending ───────────────────────────────────────────────────────────────────


async def _target(db, report: dict) -> tuple[str | None, str | None]:
    """(bot token, channel): the report's own channel, else the account's Birdy channel."""
    from core.crypto import decrypt

    user = await db["users"].find_one(
        {"user_id": report["user_id"]},
        {"integrations.slack_bot.bot_token_encrypted": 1, "integrations.slack_bot.notify_channel_id": 1},
    ) or {}
    slack_bot = (user.get("integrations") or {}).get("slack_bot") or {}
    token_enc = slack_bot.get("bot_token_encrypted")
    channel = report.get("channel_id") or slack_bot.get("notify_channel_id")
    if not token_enc or not channel:
        return None, None
    return decrypt(token_enc), channel


async def send_report(db, report: dict, now_utc: datetime, key: str | None = None,
                      event: dict | None = None) -> tuple[str, str | None]:
    """
    Build and post one report. Returns (status, error): SENT, SKIPPED (nothing
    to send to or about — not worth retrying) or FAILED (Slack refused).
    `event` fills the event placeholders (services/report_events.py).
    """
    template = rt.normalize(report.get("template"))
    if not rt.has_content(template):
        return SKIPPED, "the report has no content"

    token, channel = await _target(db, report)
    if not token or not channel:
        return SKIPPED, "Slack isn't connected or no channel is chosen"

    frequency = template["frequency"]
    key = key or now_utc.date().isoformat()
    previous = rt.previous_for(report.get("last_figures"), key, frequency)
    ctx = await rt.build_context(db, report["user_id"], template, now_utc, previous=previous)
    ctx.event = event or {}
    if not ctx.groups:
        return SKIPPED, "no active clients"

    take = await rt.write_take(db, report["user_id"], template, rt.render(template, ctx)[1])
    blocks, fallback = rt.render(template, ctx, take=take)

    from slack_sdk.web.async_client import AsyncWebClient
    try:
        await AsyncWebClient(token=token).chat_postMessage(channel=channel, text=fallback, blocks=blocks)
    except Exception as e:
        logger.warning("slack report %s: post failed: %s", report["id"], e)
        return FAILED, _slack_error(e)

    await db[REPORTS].update_one({"id": report["id"]},
                                 {"$set": {"last_figures": rt.snapshot(ctx, key)}})
    return SENT, None


def _slack_error(e: Exception) -> str:
    """Slack's short error code (e.g. not_in_channel) when there is one — never the token."""
    response = getattr(e, "response", None)
    try:
        code = response.get("error") if response is not None else None
    except Exception:
        code = None
    return code or type(e).__name__


async def run_claimed(db, report: dict, key: str, now_utc: datetime, event: dict | None = None) -> str:
    """Send a report whose run is already claimed, and record how it went."""
    try:
        status, error = await send_report(db, report, now_utc, key=key, event=event)
    except Exception as e:
        logger.error("slack report %s failed: %s", report["id"], e, exc_info=True)
        status, error = FAILED, type(e).__name__
    await finish(db, report, key, status, error)
    return status


async def send_due_reports(db, now_utc: datetime | None = None) -> dict:
    """Send every scheduled report that is due. Safe to call as often as you like."""
    now_utc = now_utc or datetime.now(timezone.utc)
    stats = {"migrated": await migrate_briefs(db), "checked": 0, "due": 0, SENT: 0, SKIPPED: 0, FAILED: 0}

    reports = await db[REPORTS].find({"enabled": True, "trigger.type": "schedule"}).to_list(None)
    zones = {}
    if reports:
        async for u in db["users"].find({"user_id": {"$in": list({r["user_id"] for r in reports})}},
                                        {"user_id": 1, "timezone": 1}):
            zones[u["user_id"]] = u.get("timezone")

    for report in reports:
        stats["checked"] += 1
        key = due_key(report, zones.get(report["user_id"]) or DEFAULT_TIMEZONE, now_utc)
        if not key or not await claim(db, report, key):
            continue
        stats["due"] += 1
        stats[await run_claimed(db, report, key, now_utc)] += 1

    from services.report_events import sweep_events
    try:
        stats["events_swept"] = await sweep_events(db, now_utc)
    except Exception as e:
        logger.error("slack reports: event sweep failed: %s", e, exc_info=True)

    logger.info("slack reports: %s", stats)
    return stats
