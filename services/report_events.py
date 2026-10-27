"""
services/report_events.py
-------------------------
Birdy events that send Slack reports: an alert starts firing, a custom metric
is created, a client account is added.

emit() records the event in `report_events` (an outbox) and starts delivering
it at once, without making the caller wait on Slack. If that delivery never
finishes — a serverless request frozen after its response, a crash — the
report tick sweeps the outbox (sweep_events) and finishes it. Each report
claims "<report id>:event:<event id>" before posting (services/slack_reports.py),
so the instant path and the sweep can never both send it.

An event report matches when its trigger names the same event, and, for
alerts, either the same alert or none (any alert). A report that sent within
COOLDOWN is skipped, so a burst (a bulk import, a flapping alert) posts once.

The event's details fill the {{event}}, {{alert_name}}, {{alert_message}},
{{account_name}} and {{metric_name}} placeholders.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from services import slack_reports as sr

logger = logging.getLogger(__name__)

EVENTS_COLL = "report_events"
COOLDOWN = timedelta(minutes=5)
SWEEP_AFTER = timedelta(minutes=2)     # leave the instant path time to finish first
GIVE_UP_AFTER = timedelta(days=1)      # an event this old is news nobody needs

TITLES = {
    "alert_fired": "Alert: {alert_name}",
    "metric_created": "New metric: {metric_name}",
    "account_created": "New account: {account_name}",
}

_tasks: set[asyncio.Task] = set()   # strong refs, so a running delivery isn't collected


def describe_event(event: str, payload: dict) -> dict:
    """The placeholder values an event provides."""
    values = {k: str(payload.get(k) or "") for k in ("alert_name", "alert_message", "account_name", "metric_name")}
    values["event"] = TITLES[event].format(**values)
    return values


async def emit(db, user_id: str, event: str, payload: dict | None = None, *, wait: bool = False) -> str | None:
    """
    Record an event and deliver it. Never raises: an event report must never
    break the action that caused it. `wait` delivers before returning (jobs,
    tests); otherwise delivery runs in the background.
    """
    if event not in sr.EVENTS:
        raise ValueError(f"unknown event {event!r}")
    try:
        # Nothing to do (and nothing to store) unless a report listens for it.
        if not await db[sr.REPORTS].find_one(
            {"user_id": user_id, "enabled": True, "trigger.type": "event", "trigger.event": event}, {"_id": 1},
        ):
            return None
        doc = {"_id": uuid.uuid4().hex, "user_id": user_id, "event": event, "payload": payload or {},
               "created_at": datetime.utcnow(), "done": False}
        await db[EVENTS_COLL].insert_one(doc)
    except Exception as e:
        logger.error("report event %s for %s not recorded: %s", event, user_id, e, exc_info=True)
        return None

    if wait:
        await deliver(db, doc)
    else:
        task = asyncio.get_running_loop().create_task(deliver(db, doc))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return doc["_id"]


def _matches(report: dict, doc: dict) -> bool:
    trigger = report.get("trigger") or {}
    if trigger.get("event") != doc["event"]:
        return False
    if doc["event"] == "alert_fired" and trigger.get("alert_id"):
        return trigger["alert_id"] == (doc.get("payload") or {}).get("alert_id")
    return True


def _cooling(report: dict, now: datetime) -> bool:
    at = report.get("last_sent_at")
    return bool(at and now.replace(tzinfo=None) - at < COOLDOWN)


async def deliver(db, doc: dict, now_utc: datetime | None = None) -> dict:
    """Send every matching report for one event, then mark it done. Never raises."""
    now_utc = now_utc or datetime.now(timezone.utc)
    stats = {sr.SENT: 0, sr.SKIPPED: 0, sr.FAILED: 0}
    try:
        reports = await db[sr.REPORTS].find(
            {"user_id": doc["user_id"], "enabled": True, "trigger.type": "event", "trigger.event": doc["event"]},
        ).to_list(None)
        values = describe_event(doc["event"], doc.get("payload") or {})
        for report in reports:
            if not _matches(report, doc):
                continue
            key = f"event:{doc['_id']}"
            if _cooling(report, now_utc):
                # Recorded on the run only: the card keeps showing the real send.
                if await sr.claim(db, report, key, status=sr.SKIPPED, error="sent less than 5 minutes ago"):
                    stats[sr.SKIPPED] += 1
                continue
            if not await sr.claim(db, report, key):
                continue   # the other path has it
            stats[await sr.run_claimed(db, report, key, now_utc, event=values)] += 1
        # A failed send stays open for the sweep to retry; anything else is finished.
        if not stats[sr.FAILED]:
            await db[EVENTS_COLL].update_one({"_id": doc["_id"]}, {"$set": {"done": True}})
    except Exception as e:
        logger.error("report event %s failed: %s", doc.get("_id"), e, exc_info=True)
    return stats


async def sweep_events(db, now_utc: datetime | None = None) -> int:
    """Finish events the instant path left open. Called by the report tick."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now = now_utc.replace(tzinfo=None)
    await db[EVENTS_COLL].update_many(
        {"done": False, "created_at": {"$lt": now - GIVE_UP_AFTER}}, {"$set": {"done": True, "expired": True}})
    swept = 0
    async for doc in db[EVENTS_COLL].find({"done": False, "created_at": {"$lt": now - SWEEP_AFTER}}):
        await deliver(db, doc, now_utc)
        swept += 1
    return swept


async def create_report_event_indexes(mongo_client):
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db[EVENTS_COLL].create_index([("done", 1), ("created_at", 1)])
    await db[EVENTS_COLL].create_index("created_at", expireAfterSeconds=30 * 24 * 3600)


# ── Emitters for the places events happen ─────────────────────────────────────


def names_label(names: list[str], shown: int = 2) -> str:
    """['Glow', 'Smile', 'Bright', 'Ortho'] → 'Glow, Smile +2'."""
    names = [n for n in names if n]
    more = f" +{len(names) - shown}" if len(names) > shown else ""
    return ", ".join(names[:shown]) + more


async def alert_fired(db, alert: dict, eval_result: dict, *, wait: bool = False) -> None:
    """Emit alert_fired when an evaluation moves an alert *into* triggered.

    An alert stays triggered across passes while its condition holds; only the
    first pass of a run counts, or the report would post every 15 minutes."""
    if not eval_result.get("triggered") or alert.get("status") == "triggered":
        return
    clients = [r.get("group_name") for r in eval_result.get("per_client_results") or [] if r.get("triggered")]
    await emit(db, alert["user_id"], "alert_fired", {
        "alert_id": alert.get("id"), "alert_name": alert.get("name"),
        "alert_message": eval_result.get("message"), "account_name": names_label(clients),
    }, wait=wait)
