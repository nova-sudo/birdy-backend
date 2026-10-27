"""
Event-triggered Slack reports (services/report_events.py): which reports an
event sends, exactly once, with the event's details filled in.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.crypto import encrypt
import services.report_events as ev
import services.slack_reports as sr

USER = "agency@x.com"


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def event_report(event, alert_id=None, text="{{event}} · {{account_name}} · Spend {{spend}}", **kw):
    return sr.new_report(
        USER, name=kw.pop("name", event), trigger={"type": "event", "event": event, "alert_id": alert_id},
        template={"version": 4, "frequency": "daily",
                  "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]}, **kw)


async def _seed(db, *reports):
    await db["users"].insert_one({"user_id": USER, "default_currency": "GBP", "timezone": "Europe/London",
                                  "integrations": {"slack_bot": {"bot_token_encrypted": encrypt("xoxb-test"),
                                                                 "notify_channel_id": "C_BIRDY"}}})
    await db["client_groups"].insert_one({
        "id": "g1", "user_id": USER, "name": "Glow Clinic", "client_status": "Active",
        "facebook_cache": {"yesterday": {"metrics": {"insights": {"spend": 120, "results": 12}}}},
    })
    if reports:
        await db["slack_reports"].insert_many(list(reports))


@pytest.fixture
def slack(monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return posts


async def test_new_account_sends_matching_reports_with_its_details(mock_db, slack):
    await _seed(mock_db, event_report("account_created"), event_report("metric_created"),
                event_report("account_created", name="Off", enabled=False))

    await ev.emit(mock_db, USER, "account_created", {"account_name": "Smile Co"}, wait=True)

    assert len(slack) == 1
    assert slack[0]["text"] == "New account: Smile Co · Smile Co · Spend £120"


async def test_specific_alert_or_any_alert(mock_db, slack):
    await _seed(mock_db, event_report("alert_fired", alert_id="a1", name="Only a1"),
                event_report("alert_fired", name="Any"))

    await ev.emit(mock_db, USER, "alert_fired", {"alert_id": "a2", "alert_name": "CPL high"}, wait=True)
    assert len(slack) == 1                                   # only "Any"

    await mock_db["slack_reports"].update_many({}, {"$set": {"last_sent_at": None}})   # past the cooldown
    await ev.emit(mock_db, USER, "alert_fired", {"alert_id": "a1", "alert_name": "CPL high"}, wait=True)
    assert len(slack) == 3


async def test_nothing_listening_records_nothing(mock_db, slack):
    await _seed(mock_db)
    assert await ev.emit(mock_db, USER, "metric_created", {"metric_name": "ROAS"}, wait=True) is None
    assert await mock_db["report_events"].count_documents({}) == 0


async def test_a_burst_posts_once(mock_db, slack):
    await _seed(mock_db, event_report("metric_created"))
    for name in ("A", "B", "C"):
        await ev.emit(mock_db, USER, "metric_created", {"metric_name": name}, wait=True)
    assert len(slack) == 1
    r = await mock_db["slack_reports"].find_one({})
    assert r["last_run"]["status"] == "sent"        # the card keeps showing the real send
    runs = await mock_db["slack_report_runs"].find({}).to_list(None)
    assert sorted(x["status"] for x in runs) == ["sent", "skipped", "skipped"]


async def test_background_delivery_and_the_sweep_never_double_send(mock_db, slack):
    await _seed(mock_db, event_report("metric_created"))
    event_id = await ev.emit(mock_db, USER, "metric_created", {"metric_name": "ROAS"})
    import asyncio
    await asyncio.gather(*ev._tasks)
    assert len(slack) == 1

    # The sweep sees the event as open (as if the instant path died mid-send) — still one post.
    await mock_db["report_events"].update_one({"_id": event_id}, {"$set": {"done": False}})
    later = datetime.now(timezone.utc) + timedelta(minutes=3)
    assert await ev.sweep_events(mock_db, later) == 1
    assert len(slack) == 1


async def test_failed_event_send_is_retried_by_the_tick(mock_db, monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    calls = []

    async def flaky(self, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("timeout")
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", flaky)
    await _seed(mock_db, event_report("metric_created"))
    await ev.emit(mock_db, USER, "metric_created", {"metric_name": "ROAS"}, wait=True)
    assert (await mock_db["report_events"].find_one({}))["done"] is False

    out = await sr.send_due_reports(mock_db, datetime.now(timezone.utc) + timedelta(minutes=3))
    assert out["events_swept"] == 1 and len(calls) == 2
    assert (await mock_db["report_events"].find_one({}))["done"] is True


async def test_alert_fires_only_on_the_transition(mock_db, slack):
    await _seed(mock_db, event_report("alert_fired", text="{{alert_name}}: {{alert_message}} ({{account_name}})"))
    alert = {"id": "a1", "user_id": USER, "name": "CPL high", "status": "active"}
    result = {"triggered": True, "message": "Triggered for: Glow Clinic",
              "per_client_results": [{"group_name": "Glow Clinic", "triggered": True},
                                     {"group_name": "Smile", "triggered": False}]}

    await ev.alert_fired(mock_db, alert, result, wait=True)
    await ev.alert_fired(mock_db, {**alert, "status": "triggered"}, result, wait=True)   # still firing
    await ev.alert_fired(mock_db, alert, {**result, "triggered": False}, wait=True)

    assert [p["text"] for p in slack] == ["CPL high: Triggered for: Glow Clinic (Glow Clinic)"]


async def test_alert_job_emits_on_transition(mock_mongo_client, monkeypatch, slack):
    from contextlib import asynccontextmanager
    from core.database import DB_NAME
    import jobs.alert_jobs as jobs

    db = mock_mongo_client[DB_NAME]
    await _seed(db, event_report("alert_fired"))
    await db["alerts"].insert_one({"id": "a1", "user_id": USER, "name": "CPL high", "status": "active"})

    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def fake_eval(alert, client):
        return {"triggered": True, "message": "over", "current_value": 1.0, "progress_pct": 100.0}

    monkeypatch.setattr(jobs, "get_mongo_client", fake_client)
    monkeypatch.setattr(jobs, "evaluate_alert", fake_eval)
    await jobs.evaluate_all_alerts()
    await jobs.evaluate_all_alerts()
    assert len(slack) == 1 and slack[0]["text"].startswith("Alert: CPL high")


def test_event_placeholders_validate_and_preview():
    import services.report_blocks as rb
    import services.report_template as core
    t = rb.validate({"frequency": "daily", "time": "9:00 AM",
                     "blocks": [{"type": "section", "text": "{{event}} {{metric_name}}"}]}, {})
    assert t["blocks"]
    ctx = core.Context(frequency="daily", groups=[], currency="GBP", now_utc=utc(2026, 9, 30),
                       alerts=[], flagged=[], preview=True)
    assert core._resolve({"name": "metric_name", "opts": {}, "raw": ""}, ctx) == "‹metric name›"
    ctx.preview = False
    assert core._resolve({"name": "metric_name", "opts": {}, "raw": ""}, ctx) == ""


def test_names_label():
    assert ev.names_label(["A", "B", "C", "D"]) == "A, B +2"
    assert ev.names_label(["A"]) == "A"
