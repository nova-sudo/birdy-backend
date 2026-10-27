"""
The Slack reports API (routers/slack_reports.py): create, edit, turn off,
delete and test-send reports, with the brief as the default one.
"""
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.crypto import encrypt
from core.database import DB_NAME
from dependencies import get_current_user
import routers.slack_reports as api

USER = "agency@x.com"
BLOCKS = [{"type": "section", "text": "Spend {{spend}}"}]
BRIEF = {"version": 4, "frequency": "daily", "time": "9:00 AM", "day": None,
         "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Spend {{spend}}"}}]}


@pytest.fixture
def db(mock_mongo_client):
    return mock_mongo_client[DB_NAME]


@pytest.fixture
def client(mock_mongo_client, monkeypatch):
    @asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(api, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(api.router)
    who = {"user": USER}
    app.dependency_overrides[get_current_user] = lambda: who["user"]
    c = TestClient(app)
    c.who = who
    return c


async def _seed(db, brief=BRIEF):
    slack_bot = {"bot_token_encrypted": encrypt("xoxb-test"), "notify_channel_id": "C_BIRDY"}
    if brief:
        slack_bot["brief"] = brief
    await db["users"].insert_one({"user_id": USER, "timezone": "Europe/London", "default_currency": "GBP",
                                  "integrations": {"slack_bot": slack_bot}})
    await db["client_groups"].insert_one({
        "id": "g1", "user_id": USER, "name": "Glow Clinic", "client_status": "Active",
        "facebook_cache": {"yesterday": {"metrics": {"insights": {"spend": 120, "results": 12}}}},
    })


SCHEDULE = {"type": "schedule", "frequency": "weekly", "day": "friday", "time": "16:45", "timezone": "Asia/Dubai"}


async def test_list_starts_with_the_migrated_brief(db, client):
    await _seed(db)
    reports = client.get("/api/slack-reports").json()["reports"]
    assert len(reports) == 1
    assert reports[0]["is_default"] and reports[0]["name"] == "Daily brief"
    assert reports[0]["summary"] == ["section"]


async def test_create_edit_toggle_delete(db, client):
    await _seed(db, brief=None)
    r = client.post("/api/slack-reports", json={
        "name": "Leadership", "trigger": SCHEDULE, "channel_id": "C0LEAD", "channel_name": "leadership",
        "template": {"frequency": "weekly", "blocks": BLOCKS}})
    assert r.status_code == 200, r.text
    rep = r.json()["report"]
    assert rep["trigger"] == {**SCHEDULE, "day": "Friday"} and rep["enabled"]
    assert rep["template"]["frequency"] == "weekly"

    r = client.patch(f"/api/slack-reports/{rep['id']}", json={
        "enabled": False, "trigger": {"type": "event", "event": "alert_fired", "alert_id": "a1"}})
    assert r.json()["report"]["enabled"] is False
    assert r.json()["report"]["trigger"] == {"type": "event", "event": "alert_fired", "alert_id": "a1"}

    assert client.delete(f"/api/slack-reports/{rep['id']}").json() == {"ok": True}
    assert client.get("/api/slack-reports").json()["reports"] == []


async def test_new_report_without_content_gets_the_default_template(db, client):
    await _seed(db, brief=None)
    rep = client.post("/api/slack-reports", json={
        "name": "New accounts", "trigger": {"type": "event", "event": "account_created"}}).json()["report"]
    assert rep["template"]["blocks"] and rep["template"]["frequency"] == "daily" and rep["channel_id"] is None

    # A data window alone keeps the content and only changes what the numbers cover.
    weekly = client.post("/api/slack-reports", json={
        "name": "Wrap", "trigger": SCHEDULE, "template": {"frequency": "weekly"}}).json()["report"]
    assert weekly["template"]["frequency"] == "weekly" and weekly["template"]["blocks"]
    custom = client.patch(f"/api/slack-reports/{rep['id']}", json={"template": {"frequency": "daily", "blocks": BLOCKS}}).json()["report"]
    again = client.patch(f"/api/slack-reports/{rep['id']}", json={"template": {"frequency": "weekly"}}).json()["report"]
    assert again["template"] == {**custom["template"], "frequency": "weekly"}


@pytest.mark.parametrize("body, message", [
    ({"trigger": SCHEDULE}, "name"),
    ({"name": "x"}, "when"),
    ({"name": "x", "trigger": {**SCHEDULE, "time": "9am"}}, "HH:MM"),
    ({"name": "x", "trigger": {**SCHEDULE, "timezone": "Mars/Base"}}, "timezone"),
    ({"name": "x", "trigger": {"type": "event", "event": "lunch"}}, "event"),
    ({"name": "x", "trigger": SCHEDULE, "channel_id": "#general"}, "channel"),
])
async def test_bad_input_is_refused(db, client, body, message):
    await _seed(db, brief=None)
    r = client.post("/api/slack-reports", json=body)
    assert r.status_code in (400, 422) and message.lower() in r.text.lower()


async def test_bad_blocks_list_their_errors(db, client):
    await _seed(db, brief=None)
    r = client.post("/api/slack-reports", json={
        "name": "x", "trigger": SCHEDULE, "template": {"frequency": "daily", "blocks": [{"type": "section", "text": "{{nope}}"}]}})
    assert r.status_code == 400 and "unknown placeholder" in r.text


async def test_default_report_writes_the_brief_and_cannot_go(db, client):
    await _seed(db)
    default = client.get("/api/slack-reports").json()["reports"][0]

    assert client.delete(f"/api/slack-reports/{default['id']}").status_code == 400
    r = client.patch(f"/api/slack-reports/{default['id']}", json={"trigger": {"type": "event", "event": "metric_created"}})
    assert r.status_code == 400

    r = client.patch(f"/api/slack-reports/{default['id']}", json={"trigger": SCHEDULE, "name": "Friday wrap"})
    assert r.status_code == 200
    user = await db["users"].find_one({"user_id": USER})
    brief = user["integrations"]["slack_bot"]["brief"]
    assert (brief["frequency"], brief["day"], brief["time"]) == ("weekly", "Friday", "4:45 PM")


async def test_reports_are_private_to_their_account(db, client):
    await _seed(db, brief=None)
    rep = client.post("/api/slack-reports", json={"name": "Mine", "trigger": SCHEDULE}).json()["report"]
    client.who["user"] = "someone@else.com"
    assert client.get("/api/slack-reports").json()["reports"] == []
    assert client.patch(f"/api/slack-reports/{rep['id']}", json={"name": "Theirs"}).status_code == 404
    assert client.delete(f"/api/slack-reports/{rep['id']}").status_code == 404
    assert client.post(f"/api/slack-reports/{rep['id']}/test").status_code == 404


async def test_limit(db, client, monkeypatch):
    monkeypatch.setattr(api.sr, "MAX_REPORTS", 1)
    await _seed(db, brief=None)
    assert client.post("/api/slack-reports", json={"name": "1", "trigger": SCHEDULE}).status_code == 200
    assert client.post("/api/slack-reports", json={"name": "2", "trigger": SCHEDULE}).status_code == 400


async def test_test_send_fills_sample_event_and_records_nothing(db, client, monkeypatch):
    from slack_sdk.web.async_client import AsyncWebClient
    posts = []

    async def fake_post(self, **kw):
        posts.append(kw)
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    await _seed(db, brief=None)
    rep = client.post("/api/slack-reports", json={
        "name": "Alerts", "trigger": {"type": "event", "event": "alert_fired"}, "channel_id": "C0OPS",
        "template": {"frequency": "daily", "blocks": [{"type": "section", "text": "{{event}} · {{spend}}"}]}}).json()["report"]

    assert client.post(f"/api/slack-reports/{rep['id']}/test").json() == {"status": "sent", "error": None}
    assert posts[0]["channel"] == "C0OPS"
    assert posts[0]["text"] == "Alert: Cost per lead above target · £120"
    stored = await db["slack_reports"].find_one({"id": rep["id"]})
    assert stored["last_run"] is None and stored["last_figures"] is None
