"""
A report made from the chat waits for the owner's approval with a preview (1A), and Slack is a stop on
the route map (2A). services/report_proposals.py, ai/orchestrator.py, ai/team/delegation.py,
routers/chat.apply_proposal, routers/report_designer.preview, ai/team/route.py, and the MCP
connector's two-step create (ai/mcp/slack_reports_mcp.py). Mongomock and scripted providers only.
"""

import contextlib
import json
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

from ai.mcp import slack_reports_mcp as t
from ai.team import route as rt
from services import report_proposals as rp
from services import slack_report_ops as ops
from services import slack_reports as sr
from test_agent_team import G1, U, TeamProvider, _events, _use, api, call, chat, groups, registry, say  # noqa: F401
from test_report_owner import ADS, CALLS, CLIENT_LEVEL, _as, _tool_call, await_list, fake_mcp  # noqa: F401

SCHEDULE = {"type": "schedule", "frequency": "daily", "time": "09:00"}


async def _bot(db, channel="C0BIRDY", name="birdy-reports"):
    await db["users"].insert_one({"user_id": U, "integrations": {"slack_bot": {
        "notify_channel_id": channel, "notify_channel_name": name}}})


def _create(name="Daily numbers", template=CLIENT_LEVEL, **kw):
    return _tool_call("create_slack_report", {"name": name, "trigger": SCHEDULE, "template": template, **kw})


# ---------------------------------------------------------------------------
# 1A: an agent's create in its chat is a proposal with its preview fields
# ---------------------------------------------------------------------------

async def test_an_agents_create_in_its_chat_is_a_proposal_not_a_report(mock_db, registry, fake_mcp):
    await _bot(mock_db)
    routes = []
    p = TeamProvider({"birdy": [_create(), say("It's waiting for your approval.")]})
    out = await chat(p, registry, mock_db, agent="birdy", on_route=routes.append)
    assert await mock_db[sr.REPORTS].count_documents({}) == 0 and fake_mcp == []
    [prop] = out["proposals"]
    assert prop["kind"] == "slack_report" and prop["agent"] == "birdy" and prop["proposed_by"] == "birdy"
    assert prop["title"] == "Daily numbers" and prop["status"] == "pending"
    assert set(prop["payload"]) == {"name", "owner", "trigger", "channel_id", "channel_name", "enabled", "template"}
    assert prop["payload"]["owner"] == "birdy" and prop["payload"]["template"]["version"] == 4   # cleaned
    assert prop["sends_to"] == {"channel_id": "C0BIRDY", "channel_name": "birdy-reports", "default_channel": True}
    assert prop["schedule_label"] == "Every day · 09:00"
    told = next(m["content"] for _, _, msgs in p.calls for m in msgs if m.get("role") == "tool")
    assert "waiting for the owner's approval" in told
    archived = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert archived["proposals"][0]["id"] == prop["id"]
    # 2A: the Slack tool is a Slack stop, live and done
    live = [s for r in routes for s in r["steps"] if s["doing_kind"] == "slack"]
    assert live and live[0]["doing"] == "Creating the report"
    done = out["route"]["steps"][0]
    assert done["doing"] is None and done["doing_kind"] is None
    assert done["tools"] == [{"name": "create_slack_report", "label": "Creating the report", "detail": None,
                              "kind": "slack"}]


async def test_a_refused_create_proposes_nothing_and_tells_the_agent_the_fix(mock_db, registry, fake_mcp):
    bad = {"frequency": "daily", "blocks": [{"type": "section", "text": "{{nope}}"}]}
    p = TeamProvider({"media_buyer": [_create(template=bad), say("Fixing it.")]})
    out = await chat(p, registry, mock_db, agent="media_buyer")
    assert out["proposals"] == []
    told = next(m["content"] for _, _, msgs in p.calls for m in msgs if m.get("role") == "tool")
    assert "unknown placeholder" in told and "Fix these and call again" in told


async def test_update_in_chat_still_saves_directly(mock_db, registry, fake_mcp):
    """Only a new report needs the card; a change goes through update_slack_report as before."""
    made = (await rp.apply(mock_db, U, await rp.build(mock_db, U, "birdy",
                                                     {"name": "Daily numbers", "trigger": SCHEDULE})))["report"]
    p = TeamProvider({"birdy": [_tool_call("update_slack_report", {"report_id": made["id"], "name": "Morning"}),
                                say("Renamed.")]})
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["proposals"] == [] and (await mock_db[sr.REPORTS].find_one({"id": made["id"]}))["name"] == "Morning"
    assert out["route"]["steps"][0]["tools"][0]["kind"] == "slack"


async def test_a_delegated_propose_action_is_the_same_report_proposal(mock_db, registry):
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="Make a daily sales report", client="agency"), say("ok")],
        "sales_coach": [call("propose_action", title="x", detail="d", kind="create_slack_report",
                             payload={"name": "Sales", "trigger": {"type": "schedule", "frequency": "weekly",
                                                                   "day": "monday", "time": "08:30"},
                                      "template": CALLS}),
                        call("propose_action", title="bad", detail="d", kind="create_slack_report",
                             payload={"name": "Broken"}),
                        say("Proposed.")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    [prop] = out["proposals"]
    assert prop["kind"] == "slack_report" and prop["agent"] == "sales_coach" and prop["title"] == "Sales"
    assert prop["schedule_label"] == "Mondays · 08:30" and prop["payload"]["trigger"]["day"] == "Monday"
    errors = [m["content"] for r, _, msgs in p.calls if r == "sales_coach" for m in msgs if m.get("role") == "tool"]
    assert any("Choose when the report sends" in e for e in errors)


@pytest.mark.parametrize("trigger,label", [
    ({"type": "schedule", "frequency": "daily", "time": "09:00"}, "Every day · 09:00"),
    ({"type": "schedule", "frequency": "weekly", "day": "Monday", "time": "07:15"}, "Mondays · 07:15"),
    ({"type": "schedule", "frequency": "daily", "time": "09:00", "timezone": "Europe/London"},
     "Every day · 09:00 (Europe/London)"),
    ({"type": "event", "event": "alert_fired"}, "When an alert fires"),
    ({"type": "event", "event": "account_created"}, "When a new account is added"),
])
def test_schedule_label(trigger, label):
    assert ops.schedule_label(trigger) == label


# ---------------------------------------------------------------------------
# 1A: approving creates the report directly, after checking it again
# ---------------------------------------------------------------------------

@pytest.fixture
def chat_api(api, monkeypatch, registry, fake_mcp):
    chat_router, app = api

    def run(scripts):
        p = TeamProvider(scripts)
        _use(monkeypatch, chat_router, p, registry)
        return p, TestClient(app)
    return run


def test_approve_creates_the_report_without_an_agent_run(chat_api, mock_db):
    p, client = chat_api({"media_buyer": [_create("Ads watch", ADS, channel_id="C0ADS", channel_name="ads"),
                                          say("Proposed.")]})
    first = client.post("/api/chat", json={"message": "an ads report", "agent": "media_buyer"}).json()
    prop = first["proposals"][0]
    assert prop["sends_to"] == {"channel_id": "C0ADS", "channel_name": "ads", "default_channel": False}
    n = len(p.calls)
    res = client.post(f"/api/chat/proposals/{prop['id']}/apply")
    assert res.status_code == 200 and len(p.calls) == n
    body = res.json()
    assert body["ok"] is True and body["proposal_id"] == prop["id"]
    [stored] = await_list(mock_db)
    assert body["report"] == {"id": stored["id"], "owner": "media_buyer", "name": "Ads watch"}
    assert stored["channel_id"] == "C0ADS" and stored["template"] == prop["payload"]["template"]
    approved = client.get("/api/chat/proposals", params={"status": "approved"}).json()["proposals"]
    assert [x["id"] for x in approved] == [prop["id"]] and approved[0]["can_undo"] is False
    assert client.post(f"/api/chat/proposals/{prop['id']}/apply").status_code == 404


def test_approve_checks_again_and_a_refusal_leaves_it_pending(chat_api, mock_db, monkeypatch):
    p, client = chat_api({"birdy": [_create(), say("Proposed.")]})
    prop = client.post("/api/chat", json={"message": "a report", "agent": "birdy"}).json()["proposals"][0]
    monkeypatch.setattr(sr, "MAX_REPORTS", 0)      # the account filled up meanwhile
    res = client.post(f"/api/chat/proposals/{prop['id']}/apply")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "report_refused" and "up to 0 reports" in res.json()["detail"]["message"]
    assert await_list(mock_db) == []
    pending = client.get("/api/chat/proposals").json()["proposals"]
    assert [x["id"] for x in pending] == [prop["id"]]
    monkeypatch.setattr(sr, "MAX_REPORTS", 10)
    assert client.post(f"/api/chat/proposals/{prop['id']}/apply").status_code == 200
    assert client.post(f"/api/chat/proposals/{prop['id']}/dismiss").status_code == 404


def test_decline_saves_nothing(chat_api, mock_db):
    _, client = chat_api({"birdy": [_create(), say("Proposed.")]})
    prop = client.post("/api/chat", json={"message": "a report", "agent": "birdy"}).json()["proposals"][0]
    assert client.post(f"/api/chat/proposals/{prop['id']}/dismiss").json()["status"] == "dismissed"
    assert client.post(f"/api/chat/proposals/{prop['id']}/apply").status_code == 404
    assert await_list(mock_db) == []


async def test_apply_refuses_a_payload_outside_its_owners_area(mock_db):
    prop = await rp.build(mock_db, U, "birdy", {"name": "Daily numbers", "trigger": SCHEDULE,
                                                "template": CLIENT_LEVEL})
    prop["payload"]["template"] = ADS
    with pytest.raises(ops.ReportError) as e:
        await rp.apply(mock_db, U, prop)
    assert "Marketing Guru report" in rp.error_text(e.value)
    assert await mock_db[sr.REPORTS].count_documents({}) == 0


# ---------------------------------------------------------------------------
# 1A: the card's preview, and the MCP connector's two steps
# ---------------------------------------------------------------------------

async def test_the_preview_endpoint_takes_the_owner_and_fills_sales_metrics_and_compare(mock_db, mock_mongo_client,
                                                                                      monkeypatch):
    import routers.report_designer as designer
    from dependencies import get_current_user
    from test_report_sales import USER, _seed

    await _seed(mock_db)

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client
    monkeypatch.setattr(designer, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(designer.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    template = {"frequency": "daily", "blocks": [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Booked {{booked}} · closed {{closed}} · "
                                                                "speed {{speed_to_lead}} · {{speed_within_target}}"}},
        {"type": "data_visualization", "title": "Booked", "birdy_data": {"chart": "bar", "by": "client",
                                                                         "metrics": ["booked"], "compare": "previous"}}]}
    res = TestClient(app).post("/api/report-designer/preview", json={"template": template, "owner": "sales_coach"})
    assert res.status_code == 200
    out = res.json()
    assert out["sample"] is False
    assert out["blocks"][0]["text"]["text"] == "Booked 4 · closed 1 · speed 4.3 min · 66.7%"
    chart = out["blocks"][1]["chart"]
    assert chart["series"][0]["data"] == [{"label": "Aura", "value": 3.0}, {"label": "Bloom", "value": 1.0}]
    # compare: nothing was sent before, so the card says when the earlier figures appear
    assert "the day before shows next to yesterday" in out["blocks"][2]["elements"][0]["text"]
    bad = TestClient(app).post("/api/report-designer/preview", json={"template": template, "owner": "nobody"})
    assert bad.status_code == 422


async def test_the_connector_confirms_with_the_rendered_message_first(mock_db, monkeypatch):
    await _bot(mock_db)
    _as(monkeypatch, None)
    args = dict(name="Daily numbers", trigger=SCHEDULE, template=CLIENT_LEVEL)
    first = await t.create_slack_report.fn(**args)
    assert first["needs_confirmation"] is True and await mock_db[sr.REPORTS].count_documents({}) == 0
    shown = first["preview"]
    assert shown["name"] == "Daily numbers" and shown["schedule"] == "Every day · 09:00"
    assert shown["sends_to"]["channel_name"] == "birdy-reports" and "*Spend*" in shown["message"]
    with pytest.raises(ToolError, match="unknown placeholder"):   # refused before any code is issued
        await t.create_slack_report.fn(name="x", trigger=SCHEDULE,
                                       template={"frequency": "daily", "blocks": [{"type": "section", "text": "{{nope}}"}]})
    changed = await t.create_slack_report.fn(**{**args, "name": "Other"}, confirm_code=first["confirm_code"])
    assert "error" in changed and await mock_db[sr.REPORTS].count_documents({}) == 0
    again = await t.create_slack_report.fn(**args)
    made = (await t.create_slack_report.fn(**args, confirm_code=again["confirm_code"]))["report"]
    assert made["name"] == "Daily numbers" and await mock_db[sr.REPORTS].count_documents({}) == 1


# ---------------------------------------------------------------------------
# 2A: Slack on the route
# ---------------------------------------------------------------------------

def test_every_slack_tool_has_a_plain_label_and_the_slack_kind():
    for name in rt.SLACK_TOOLS:
        assert name in rt.TOOL_LABELS and rt.tool_kind(name) == "slack"
        assert rt.tool_entry(name, None)["kind"] == "slack"
    assert rt.TOOL_LABELS["preview_report"] == "Building the preview"
    assert rt.TOOL_LABELS["send_test_slack_report"] == "Sending a test to Slack"
    assert rt.tool_kind("get_alerts") is None and "kind" not in rt.tool_entry("get_alerts", None)


def test_a_slack_only_birdy_turn_streams_its_route(chat_api):
    _, client = chat_api({"birdy": [_tool_call("list_slack_reports", {}), say("You have no reports yet.")]})
    res = client.post("/api/chat/stream", json={"message": "what reports do I have?", "agent": "birdy"})
    events = _events(res.text)
    routes = [d for e, d in events if e == "route"]
    assert any(s["doing_kind"] == "slack" and s["doing"] == "Slack reports" for r in routes for s in r["steps"])
    final = next(d for e, d in events if e == "final")
    [step] = final["route"]["steps"]
    assert step["doing_kind"] is None and step["tools"][0]["kind"] == "slack"
