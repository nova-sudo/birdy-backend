"""Slack report MCP tools (ai/mcp/slack_reports_mcp.py)."""

import pytest
from fastmcp.exceptions import ToolError
from slack_sdk.web.async_client import AsyncWebClient

from ai.mcp import slack_reports_mcp as t
from core.crypto import encrypt
from services import slack_reports as sr

ALICE, BOB = "alice@example.com", "bob@example.com"
CONFIRM_LINE = "Show the preview to the user and only call again with confirm_code after they say yes."
SCHEDULE = {"type": "schedule", "frequency": "weekly", "day": "friday", "time": "16:45", "timezone": "Asia/Dubai"}
BLOCKS = [{"type": "section", "text": "Spend {{spend}}"}]

pytestmark = pytest.mark.asyncio


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def bob(set_current_user):
    set_current_user(BOB)


async def _connect(db, user=ALICE, channel="C_BIRDY", with_client=True):
    await db["users"].insert_one({"user_id": user, "timezone": "Europe/London", "default_currency": "GBP",
                                  "integrations": {"slack_bot": {"bot_token_encrypted": encrypt("xoxb-test"),
                                                                 "notify_channel_id": channel,
                                                                 "notify_channel_name": "birdy-alerts"}}})
    if with_client:
        await db["client_groups"].insert_one({
            "id": f"g-{user}", "user_id": user, "name": "Glow Clinic", "client_status": "Active",
            "facebook_cache": {"yesterday": {"metrics": {"insights": {"spend": 120, "results": 12}}}}})


@pytest.fixture
def posts(monkeypatch):
    sent = []

    async def fake_post(self, **kw):
        sent.append(kw)
        return {"ok": True}

    monkeypatch.setattr(AsyncWebClient, "chat_postMessage", fake_post)
    return sent


async def _make(user_tool_kwargs=None, **kw):
    args = {"name": "Friday wrap", "trigger": SCHEDULE, "channel_id": "C0OPS", "channel_name": "ops",
            "template": {"frequency": "daily", "blocks": BLOCKS}, **kw}
    return (await t.create_slack_report.fn(**args))["report"]


async def test_tool_tags_match_the_spec():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    assert tools["list_slack_reports"].tags == {"read"}
    for n in ("create_slack_report", "update_slack_report", "delete_slack_report"):
        assert tools[n].tags == {"write"}
    assert tools["send_test_slack_report"].tags == {"write", "live"}
    assert CONFIRM_LINE in tools["send_test_slack_report"].description


async def test_create_validates_like_router(mock_db, alice):
    bad = [
        ({"name": "  ", "trigger": SCHEDULE}, "name"),
        ({"name": "x", "trigger": {**SCHEDULE, "time": "9am"}}, "HH:MM"),
        ({"name": "x", "trigger": {**SCHEDULE, "timezone": "Mars/Base"}}, "timezone"),
        ({"name": "x", "trigger": {"type": "event", "event": "lunch"}}, "event"),
        ({"name": "x", "trigger": {"type": "sometimes"}}, "trigger"),
        ({"name": "x", "trigger": SCHEDULE, "channel_id": "#general"}, "channel"),
        ({"name": "x" * 81, "trigger": SCHEDULE}, "name"),
        ({"name": "x", "trigger": SCHEDULE, "template": {"frequency": "daily", "blocks": [{"type": "section", "text": "{{nope}}"}]}},
         "unknown placeholder"),
    ]
    for args, message in bad:
        with pytest.raises(ToolError) as e:
            await t.create_slack_report.fn(**args)
        assert message.lower() in str(e.value).lower(), (args, str(e.value))
    assert await mock_db["slack_reports"].count_documents({}) == 0


async def test_create_and_list(mock_db, alice):
    rep = await _make()
    assert rep["name"] == "Friday wrap" and rep["trigger"]["day"] == "Friday" and rep["enabled"] is True
    assert rep["channel_id"] == "C0OPS"
    stored = await mock_db["slack_reports"].find_one({"id": rep["id"]})
    assert stored["user_id"] == ALICE
    out = await t.list_slack_reports.fn()
    assert [r["id"] for r in out["reports"]] == [rep["id"]] and out["max"] == sr.MAX_REPORTS


async def test_create_limit(mock_db, alice, monkeypatch):
    monkeypatch.setattr(sr, "MAX_REPORTS", 1)
    await _make()
    with pytest.raises(ToolError, match="up to 1 reports"):
        await _make()


async def test_update_applies_only_the_fields_given(mock_db, alice):
    rep = await _make()
    out = (await t.update_slack_report.fn(report_id=rep["id"], name="Renamed"))["report"]
    assert out["name"] == "Renamed"
    stored = await mock_db["slack_reports"].find_one({"id": rep["id"]})
    for k in ("trigger", "template", "channel_id", "channel_name", "enabled"):
        assert stored[k] == rep[k], k
    await t.update_slack_report.fn(report_id=rep["id"], enabled=False)
    stored = await mock_db["slack_reports"].find_one({"id": rep["id"]})
    assert stored["enabled"] is False and stored["name"] == "Renamed"
    with pytest.raises(ToolError, match="HH:MM"):
        await t.update_slack_report.fn(report_id=rep["id"], trigger={**SCHEDULE, "time": "late"})


async def test_default_report_rules(mock_db, alice):
    await _connect(mock_db)
    await mock_db["users"].update_one({"user_id": ALICE}, {"$set": {"integrations.slack_bot.brief": {
        "version": 4, "frequency": "daily", "time": "9:00 AM", "day": None,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Spend {{spend}}"}}]}}})
    default = (await t.list_slack_reports.fn())["reports"][0]
    assert default["is_default"]
    with pytest.raises(ToolError, match="can't be deleted"):
        await t.delete_slack_report.fn(report_id=default["id"])
    with pytest.raises(ToolError, match="schedule"):
        await t.update_slack_report.fn(report_id=default["id"], trigger={"type": "event", "event": "metric_created"})


async def test_delete(mock_db, alice):
    rep = await _make()
    await mock_db["slack_report_runs"].insert_one({"report_id": rep["id"]})
    assert (await t.delete_slack_report.fn(report_id=rep["id"])) == {"ok": True}
    assert await mock_db["slack_reports"].count_documents({}) == 0
    assert await mock_db["slack_report_runs"].count_documents({}) == 0
    with pytest.raises(ToolError, match="Report not found"):
        await t.delete_slack_report.fn(report_id=rep["id"])


async def test_send_test_needs_confirmation(mock_db, alice, posts):
    await _connect(mock_db)
    rep = await _make()
    first = await t.send_test_slack_report.fn(report_id=rep["id"])
    assert first["needs_confirmation"] and first["confirm_code"]
    p = first["preview"]
    assert p["channel"] == "ops" and p["report_id"] == rep["id"]
    assert "Spend £120" in p["message"]
    assert posts == []
    out = await t.send_test_slack_report.fn(report_id=rep["id"], confirm_code=first["confirm_code"])
    assert out == {"ok": True, "status": "sent", "error": None, "summary": "Posted the report to #ops."}
    assert len(posts) == 1 and posts[0]["channel"] == "C0OPS" and posts[0]["text"] == p["message"]
    again = await t.send_test_slack_report.fn(report_id=rep["id"], confirm_code=first["confirm_code"])
    assert "Nothing was changed" in again["error"] and len(posts) == 1
    stored = await mock_db["slack_reports"].find_one({"id": rep["id"]})
    assert stored.get("last_figures") is None


async def test_send_test_uses_default_channel_name(mock_db, alice, posts):
    await _connect(mock_db)
    rep = await _make(channel_id=None, channel_name=None)
    first = await t.send_test_slack_report.fn(report_id=rep["id"])
    assert first["preview"]["channel"] == "birdy-alerts"


async def test_no_slack_error(mock_db, alice, posts):
    await mock_db["users"].insert_one({"user_id": ALICE, "integrations": {}})
    rep = await _make()
    with pytest.raises(ToolError, match=r"Connect Slack in Birdy first \(Settings → Integrations\)\."):
        await t.send_test_slack_report.fn(report_id=rep["id"])
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    assert posts == []


async def test_send_without_channel_issues_no_code(mock_db, alice, posts):
    await _connect(mock_db, channel=None)
    rep = await _make(channel_id=None, channel_name=None)
    with pytest.raises(ToolError, match="channel"):
        await t.send_test_slack_report.fn(report_id=rep["id"])
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0


async def test_send_code_replayed_for_other_report_is_refused(mock_db, alice, posts):
    await _connect(mock_db)
    r1, r2 = await _make(), await _make(name="Other")
    first = await t.send_test_slack_report.fn(report_id=r1["id"])
    out = await t.send_test_slack_report.fn(report_id=r2["id"], confirm_code=first["confirm_code"])
    assert "Nothing was changed" in out["error"] and posts == []


async def test_other_accounts_cannot_touch_my_reports(mock_db, set_current_user, posts):
    set_current_user(ALICE)
    await _connect(mock_db, ALICE)
    await _connect(mock_db, BOB)
    rep = await _make()
    before = await mock_db["slack_reports"].find_one({"id": rep["id"]})
    set_current_user(BOB)
    assert (await t.list_slack_reports.fn())["reports"] == []
    for call in (lambda: t.update_slack_report.fn(report_id=rep["id"], name="Hacked"),
                 lambda: t.delete_slack_report.fn(report_id=rep["id"]),
                 lambda: t.send_test_slack_report.fn(report_id=rep["id"])):
        with pytest.raises(ToolError, match="Report not found"):
            await call()
    assert await mock_db["slack_reports"].find_one({"id": rep["id"]}) == before
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0 and posts == []
    # a code alice was given cannot be used by bob
    set_current_user(ALICE)
    code = (await t.send_test_slack_report.fn(report_id=rep["id"]))["confirm_code"]
    set_current_user(BOB)
    with pytest.raises(ToolError, match="Report not found"):
        await t.send_test_slack_report.fn(report_id=rep["id"], confirm_code=code)
    assert posts == []


async def test_read_scope_cannot_call_write_or_live_tools(mcp_call, mock_db, alice):
    for tool, args in [("create_slack_report", {"name": "x", "trigger": SCHEDULE}),
                       ("update_slack_report", {"report_id": "r", "name": "x"}),
                       ("delete_slack_report", {"report_id": "r"}),
                       ("send_test_slack_report", {"report_id": "r"})]:
        res = await mcp_call(tool, args, scope="birdy.read", token_type="mcp")
        assert res.is_error and "view-only" in res.text, tool
    ok = await mcp_call("list_slack_reports", {}, scope="birdy.read", token_type="mcp")
    assert not ok.is_error
