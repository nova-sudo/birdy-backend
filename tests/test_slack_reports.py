"""
Slack reports (services/slack_reports.py): the brief becomes the default
report, any number of scheduled reports send at any minute to their own
channel, and each period is sent exactly once.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.crypto import encrypt
import services.slack_reports as sr

USER = "agency@x.com"
BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "Spend {{spend}}"}}]
BRIEF = {"version": 4, "frequency": "daily", "time": "9:00 AM", "day": None, "blocks": BLOCKS}


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def schedule(time="09:05", frequency="daily", day=None, tz=None):
    return {"type": "schedule", "frequency": frequency, "day": day, "time": time, "timezone": tz}


def report(**kw):
    trigger = kw.pop("trigger", schedule())
    return sr.new_report(USER, name=kw.pop("name", "Ops"), trigger=trigger,
                         template={"version": 4, "frequency": "daily", "blocks": BLOCKS}, **kw)


async def _seed(db, *, brief=None, slack=True, last_sent=None, tz="Europe/London"):
    slack_bot = {}
    if brief:
        slack_bot["brief"] = brief
    if last_sent:
        slack_bot["brief_last_sent"] = last_sent
    if slack:
        slack_bot.update(bot_token_encrypted=encrypt("xoxb-test"), notify_channel_id="C_BIRDY")
    await db["users"].insert_one({"user_id": USER, "default_currency": "GBP", "timezone": tz,
                                  "integrations": {"slack_bot": slack_bot}})
    await db["client_groups"].insert_one({
        "id": "g1", "user_id": USER, "name": "Glow Clinic", "client_status": "Active",
        "facebook_cache": {"yesterday": {"metrics": {"insights": {"spend": 120, "results": 12}}}},
    })


@pytest.fixture
def slack(monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    posts = []

    async def fake_post(self, **kw):
        posts.append({"token": self.token, **kw})
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return posts


# ── when a schedule is due ────────────────────────────────────────────────────
# 2026-09-30 is a Wednesday in British Summer Time: 09:05 London is 08:05 UTC.


def test_any_minute_in_the_reports_timezone():
    r = report()
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 8, 4)) is None
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 8, 5)) == "2026-09-30"
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 11, 4)) == "2026-09-30"
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 11, 5)) is None    # past LATE_LIMIT


def test_own_timezone_beats_the_accounts():
    r = report(trigger=schedule("09:05", tz="America/New_York"))       # EDT: 13:05 UTC
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 8, 5)) is None
    assert sr.due_key(r, "Europe/London", utc(2026, 9, 30, 13, 5)) == "2026-09-30"


def test_weekly_disabled_and_event_reports():
    weekly = report(trigger=schedule("09:05", "weekly", "Monday"))
    assert sr.due_key(weekly, None, utc(2026, 9, 30, 8, 5)) is None            # Wednesday
    assert sr.due_key(weekly, None, utc(2026, 10, 5, 8, 5)) == "2026-10-05"    # Monday
    assert sr.due_key(report(enabled=False), None, utc(2026, 9, 30, 8, 5)) is None
    event = report(trigger={"type": "event", "event": "metric_created", "alert_id": None})
    assert sr.due_key(event, None, utc(2026, 9, 30, 8, 5)) is None


def test_brief_time_round_trips():
    assert sr.parse_hhmm("8:30 PM").strftime("%H:%M") == "20:30"
    assert sr.parse_hhmm("07:45").strftime("%H:%M") == "07:45"
    assert sr.brief_time_label(sr.parse_hhmm("09:05")) == "9:05 AM"


# ── the brief becomes the default report ──────────────────────────────────────


async def test_migration_makes_one_default_report_and_never_resends_today(mock_db, slack):
    await _seed(mock_db, brief=BRIEF, last_sent="2026-09-30")

    out = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 0))
    again = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 5))

    assert out["migrated"] == 1 and again["migrated"] == 0
    reports = await mock_db["slack_reports"].find({"user_id": USER}).to_list(None)
    assert len(reports) == 1
    d = reports[0]
    assert d["is_default"] and d["name"] == "Daily brief" and d["enabled"]
    assert d["trigger"] == schedule("09:00")
    assert slack == []                                   # today's brief already went out

    await sr.send_due_reports(mock_db, utc(2026, 10, 1, 8, 0))
    assert len(slack) == 1 and slack[0]["channel"] == "C_BIRDY"


async def test_brief_edits_sync_but_keep_the_reports_own_settings(mock_db):
    await _seed(mock_db, brief=BRIEF)
    first = await sr.sync_default_from_brief(mock_db, USER)
    await mock_db["slack_reports"].update_one({"id": first["id"]}, {"$set": {
        "name": "Morning", "channel_id": "C_OPS", "enabled": False, "trigger.timezone": "Asia/Dubai"}})

    await mock_db["users"].update_one({"user_id": USER}, {"$set": {
        "integrations.slack_bot.brief": {**BRIEF, "frequency": "weekly", "day": "Friday", "time": "5:30 PM"}}})
    await sr.sync_default_from_brief(mock_db, USER)

    d = await mock_db["slack_reports"].find_one({"id": first["id"]})
    assert d["trigger"] == schedule("17:30", "weekly", "Friday", tz="Asia/Dubai")
    assert d["template"]["frequency"] == "weekly"
    assert (d["name"], d["channel_id"], d["enabled"]) == ("Morning", "C_OPS", False)
    assert await mock_db["slack_reports"].count_documents({}) == 1


async def test_brief_of_round_trips_the_schedule():
    r = report(trigger=schedule("17:30", "weekly", "Friday"))
    assert sr.brief_of(r) == {"version": 4, "frequency": "weekly", "time": "5:30 PM",
                              "day": "Friday", "blocks": BLOCKS}


async def test_no_brief_no_default_report(mock_db):
    await _seed(mock_db)
    assert await sr.sync_default_from_brief(mock_db, USER) is None
    assert (await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 5)))["migrated"] == 0


# ── sending ───────────────────────────────────────────────────────────────────


async def test_each_report_posts_once_to_its_own_channel(mock_db, slack):
    await _seed(mock_db)
    await mock_db["slack_reports"].insert_many([
        report(name="Ops", channel_id="C_OPS"),
        report(name="Late", trigger=schedule("14:00")),
    ])

    out = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 5))
    again = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 10))

    assert out["sent"] == 1 and again["sent"] == 0
    assert [p["channel"] for p in slack] == ["C_OPS"]
    assert slack[0]["token"] == "xoxb-test" and "£120" in slack[0]["text"]
    ops = await mock_db["slack_reports"].find_one({"name": "Ops"})
    assert ops["last_run"]["status"] == "sent" and ops["last_figures"]


async def test_failure_records_slacks_error_and_retries(mock_db, monkeypatch):
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient
    calls = []

    async def flaky(self, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise SlackApiError("nope", {"ok": False, "error": "not_in_channel"})
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", flaky)
    await _seed(mock_db)
    await mock_db["slack_reports"].insert_one(report(channel_id="C_PRIVATE"))

    first = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 5))
    r = await mock_db["slack_reports"].find_one({})
    assert first["failed"] == 1
    assert r["last_run"] == {**r["last_run"], "status": "failed", "error": "not_in_channel"}

    second = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 10))
    assert second["sent"] == 1 and len(calls) == 2


async def test_no_slack_is_skipped_not_retried(mock_db, slack):
    await _seed(mock_db, slack=False)
    await mock_db["slack_reports"].insert_one(report())
    out = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 5))
    again = await sr.send_due_reports(mock_db, utc(2026, 9, 30, 8, 10))
    assert out["skipped"] == 1 and again["due"] == 0 and slack == []


async def test_racing_ticks_send_once(mock_db, slack):
    import asyncio
    await _seed(mock_db)
    await mock_db["slack_reports"].insert_one(report())
    now = utc(2026, 9, 30, 8, 5)
    await asyncio.gather(sr.send_due_reports(mock_db, now), sr.send_due_reports(mock_db, now))
    assert len(slack) == 1


async def test_brief_endpoint_keeps_the_default_report_in_step(mock_mongo_client, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from contextlib import asynccontextmanager
    from core.database import DB_NAME
    import routers.onboarding as onboarding
    from dependencies import get_current_user

    db = mock_mongo_client[DB_NAME]
    await _seed(db, brief=BRIEF)

    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(onboarding, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(onboarding.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    resp = TestClient(app).put("/api/integrations/slack/brief",
                               json={"frequency": "weekly", "time": "7:15 AM", "day": "Tuesday"})
    assert resp.status_code == 200, resp.text

    d = await db["slack_reports"].find_one({"user_id": USER, "is_default": True})
    assert d["trigger"] == schedule("07:15", "weekly", "Tuesday")
    assert d["template"]["blocks"] == BLOCKS
