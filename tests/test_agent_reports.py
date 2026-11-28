"""
Each agent's own reports: Slack reports carry an `owner`, the report tools are scoped server-side to the
agent whose chat run calls them (from the run's signed token), and Marketing Guru's morning and weekly
reports can be posted to Slack as a test (route and two-step MCP tool).
"""

import contextlib
import json
import types
from datetime import datetime

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

import routers.agent_stock as stock_router
import routers.agents as agents_router
import routers.slack_reports as reports_api
from ai import mcp_client
from ai.agents.store import CARDS, RUNS
from ai.mcp import server as mcp_server
from ai.mcp import slack_reports_mcp as t
from ai.mcp import stock_agent_mcp as stock_mcp
from ai.team import definitions as defs
from core.config import JWT_ALGORITHM, JWT_SECRET
from core.crypto import encrypt
from core.database import DB_NAME
from dependencies import get_current_user
from services import slack_reports as sr
from test_stock_apply import _agent, _card, _ln, _run
from test_stock_runner import USER
from test_stock_slack import SLACK, TEAM, FakeSlack, no_ai  # noqa: F401 - no_ai is an autouse fixture

pytestmark = pytest.mark.asyncio

SCHEDULE = {"type": "schedule", "frequency": "daily", "time": "09:00"}
BLOCKS = [{"type": "section", "text": "Spend {{spend}}"}]


def _as(monkeypatch, agent: str | None, token_type: str = "access"):
    """An MCP request from `agent`'s chat run (None: an outside assistant)."""
    claims = {"sub": USER, "type": token_type, **({"agent": agent} if agent else {})}
    monkeypatch.setattr("ai.mcp.server.get_access_token", lambda: types.SimpleNamespace(claims=claims))


async def _report(db, name, owner=None, **kw):
    report = sr.new_report(USER, name=name, trigger={**SCHEDULE, "frequency": "daily", "day": None, "timezone": None},
                           template={"version": 4, "frequency": "daily", "blocks": BLOCKS}, **kw)
    if owner is None:
        report.pop("owner")     # stored before owners existed
    else:
        report["owner"] = owner
    await db[sr.REPORTS].insert_one(dict(report))
    return report


# ---------------------------------------------------------------------------
# A. The owner on the API
# ---------------------------------------------------------------------------

@pytest.fixture
def reports_client(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(reports_api, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(reports_api.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    return TestClient(app)


async def test_list_filters_by_owner_and_a_report_without_one_is_birdys(mock_mongo_client, reports_client):
    db = mock_mongo_client[DB_NAME]
    await _report(db, "Old one")                      # no owner field
    await _report(db, "Ads", owner="media_buyer")
    await _report(db, "Calls", owner="sales_coach")

    every = reports_client.get("/api/slack-reports").json()["reports"]
    assert {r["name"]: r["owner"] for r in every} == {"Old one": "birdy", "Ads": "media_buyer", "Calls": "sales_coach"}
    for owner, names in (("birdy", ["Old one"]), ("media_buyer", ["Ads"]), ("sales_coach", ["Calls"])):
        listed = reports_client.get(f"/api/slack-reports?owner={owner}").json()["reports"]
        assert [r["name"] for r in listed] == names
    assert reports_client.get("/api/slack-reports?owner=nobody").status_code == 422


async def test_create_takes_an_owner_and_a_change_never_moves_it(mock_mongo_client, reports_client):
    body = {"name": "Calls daily", "trigger": SCHEDULE, "template": {"frequency": "daily", "blocks": BLOCKS}}
    made = reports_client.post("/api/slack-reports", json={**body, "owner": "sales_coach"}).json()["report"]
    assert made["owner"] == "sales_coach"
    assert reports_client.post("/api/slack-reports", json=body).json()["report"]["owner"] == "birdy"

    changed = reports_client.patch(f"/api/slack-reports/{made['id']}", json={"name": "Calls", "owner": "birdy"}).json()
    assert changed["report"]["name"] == "Calls" and changed["report"]["owner"] == "sales_coach"
    stored = await mock_mongo_client[DB_NAME][sr.REPORTS].find_one({"id": made["id"]})
    assert stored["owner"] == "sales_coach"


# ---------------------------------------------------------------------------
# B. Scoped to the calling agent, from the token
# ---------------------------------------------------------------------------

def test_the_internal_token_names_the_calling_agent():
    claims = pyjwt.decode(mcp_client._mint_internal_token(USER, "sales_coach"), JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert claims["agent"] == "sales_coach" and claims["sub"] == USER
    assert "agent" not in pyjwt.decode(mcp_client._mint_internal_token(USER), JWT_SECRET, algorithms=[JWT_ALGORITHM])


async def test_only_an_internal_token_can_name_an_agent(monkeypatch):
    _as(monkeypatch, "media_buyer")
    assert mcp_server.current_agent() == "media_buyer"
    _as(monkeypatch, "media_buyer", token_type="mcp")     # a manual token can't claim to be an agent run
    assert mcp_server.current_agent() is None
    _as(monkeypatch, None)
    assert mcp_server.current_agent() is None


def test_an_agent_run_passes_its_agent_and_a_sub_agent_its_main_agent():
    from ai.orchestrator import _mcp_caller
    assert _mcp_caller(None) == {}
    assert _mcp_caller(types.SimpleNamespace(agent="sales_coach", parent_main=None)) == {"agent": "sales_coach"}
    assert _mcp_caller(types.SimpleNamespace(agent="sub_1", parent_main="media_buyer")) == {"agent": "media_buyer"}


async def test_an_agent_lists_and_creates_only_its_own(mock_db, monkeypatch):
    await _report(mock_db, "Brief")
    await _report(mock_db, "Calls", owner="sales_coach")
    _as(monkeypatch, "media_buyer")
    assert (await t.list_slack_reports.fn())["reports"] == []
    # In an agent's chat a create only proposes; the owner's approval makes it (services/report_proposals.py).
    proposed = await t.create_slack_report.fn(name="Ads daily", trigger=SCHEDULE)
    assert proposed["proposed"] and proposed["proposal"]["agent"] == "media_buyer"
    assert await mock_db[sr.REPORTS].count_documents({"name": "Ads daily"}) == 0
    from services import report_proposals
    made = (await report_proposals.apply(mock_db, USER, proposed["proposal"]))["report"]
    assert made["owner"] == "media_buyer"
    listed = (await t.list_slack_reports.fn())["reports"]
    assert [(r["name"], r["owner_name"]) for r in listed] == [("Ads daily", "Marketing Guru")]


async def test_birdy_lists_everyones_labelled_but_changes_only_hers(mock_db, monkeypatch):
    mine = await _report(mock_db, "Brief")
    theirs = await _report(mock_db, "Ads", owner="media_buyer")
    _as(monkeypatch, "birdy")
    listed = {r["name"]: r["owner_name"] for r in (await t.list_slack_reports.fn())["reports"]}
    assert listed == {"Brief": "Agency Ops (Birdy)", "Ads": "Marketing Guru"}

    for call in (lambda: t.update_slack_report.fn(report_id=theirs["id"], name="Mine now"),
                 lambda: t.delete_slack_report.fn(report_id=theirs["id"]),
                 lambda: t.send_test_slack_report.fn(report_id=theirs["id"])):
        with pytest.raises(ToolError, match="Marketing Guru's report.*ask_media_buyer"):
            await call()
    assert (await mock_db[sr.REPORTS].find_one({"id": theirs["id"]}))["name"] == "Ads"
    assert (await t.update_slack_report.fn(report_id=mine["id"], name="Morning"))["report"]["name"] == "Morning"
    assert (await t.create_slack_report.fn(name="Ops", trigger=SCHEDULE))["proposal"]["payload"]["owner"] == "birdy"


async def test_sales_coach_is_sent_to_birdy_for_hers(mock_db, monkeypatch):
    mine = await _report(mock_db, "Brief")
    _as(monkeypatch, "sales_coach")
    with pytest.raises(ToolError, match="Agency Ops.*ask_birdy"):
        await t.delete_slack_report.fn(report_id=mine["id"])
    assert await mock_db[sr.REPORTS].count_documents({"id": mine["id"]}) == 1


async def test_an_outside_assistant_is_the_owner_and_unscoped(mock_db, monkeypatch):
    await _report(mock_db, "Brief")
    theirs = await _report(mock_db, "Calls", owner="sales_coach")
    _as(monkeypatch, None)
    assert len((await t.list_slack_reports.fn())["reports"]) == 2
    assert (await t.update_slack_report.fn(report_id=theirs["id"], name="Calls daily"))["report"]["owner"] == "sales_coach"


async def test_scoping_holds_through_the_real_auth_path(mock_db, mcp_call):
    """The agent comes from the verified token on a real MCP request, as an agent run sends it."""
    await mock_db["users"].insert_one({"user_id": USER, "integrations": {"slack_bot": {}}})
    await _report(mock_db, "Brief")
    await _report(mock_db, "Calls", owner="sales_coach")
    coach = await mcp_call("list_slack_reports", {}, user=USER, agent="sales_coach")
    assert not coach.is_error and [r["name"] for r in json.loads(coach.text)["reports"]] == ["Calls"]
    owner = await mcp_call("list_slack_reports", {}, user=USER)
    assert len(json.loads(owner.text)["reports"]) == 2


def test_each_agent_has_its_report_tools_in_its_own_chat_only():
    for agent in defs.AGENT_KEYS:
        own = set(defs.tool_names(agent, nested=False))
        assert {*defs.REPORT_READ, *defs.REPORT_WRITE} <= own, agent
        asked = set(defs.tool_names(agent, nested=True))
        assert "list_slack_reports" in asked and not asked & defs.WRITE_TOOLS, agent
        assert not set(defs.tool_names(agent, nested=False, one_client=True)) & defs.AGENCY_ONLY_TOOLS
    assert "send_test_agent_report" in defs.tool_names(defs.MEDIA_BUYER, nested=False)
    assert "send_test_agent_report" not in defs.tool_names(defs.BIRDY, nested=False)
    assert "send_test_agent_report" not in defs.tool_names(defs.SALES_COACH, nested=False)
    assert "send_test_agent_report" in defs.write_tools(defs.MEDIA_BUYER)   # it can be proposed when asked


async def test_prompts_keep_each_agents_reports_with_it(mock_db):
    async def prompt(agent):
        text = await defs.build_instructions(agent, mock_db, USER, client_group_id=None, client_name=None,
                                             asked_by=None)
        return " ".join(text.split())

    mg = await prompt(defs.MEDIA_BUYER)
    assert "send_test_agent_report" in mg and "Never ask Birdy for it" in mg
    coach = await prompt(defs.SALES_COACH)
    assert "the calls report" in coach and "Never ask Birdy for it" in coach
    birdy = await prompt(defs.BIRDY)
    assert "Never send, test or change one of your own reports in its place" in birdy


# ---------------------------------------------------------------------------
# C. Marketing Guru's morning and weekly reports, as a test
# ---------------------------------------------------------------------------

@pytest.fixture
def slack(monkeypatch):
    from ai.agents.stock import slack as stock_slack
    fake = FakeSlack()
    monkeypatch.setattr(stock_slack, "AsyncWebClient", lambda **kw: fake)
    return fake


@pytest.fixture
async def installed(mock_db):
    from services.slack_bot_service import save_slack_bot_installation
    await save_slack_bot_installation(mock_db, USER, TEAM, "xoxb-test", "UBOT")


@pytest.fixture
def stock_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(stock_router, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(stock_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    return TestClient(app)


def _text(blocks) -> str:
    return json.dumps(blocks)


async def test_morning_test_uses_the_latest_report_without_buttons(mock_db, slack, installed, stock_api):
    agent = await _agent(mock_db, settings={"slack": SLACK})
    await mock_db[RUNS].insert_one(_run(agent, phase="done", as_of=datetime(2026, 11, 3, 7)))
    await _card(mock_db, agent, _ln())

    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/morning/test")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "kind": "summary", "channel_id": "C1", "channel_name": "ads", "source": "latest"}
    (post,) = slack.posts
    assert post["channel"] == "C1" and "thread_ts" not in post and post["text"].startswith("Test: ")
    assert "Test* of Marketing Guru's morning report" in _text(post["blocks"]) and "2026-11-03" in _text(post["blocks"])
    assert not [b for b in post["blocks"] if b["type"] == "actions"]
    # recorded nowhere: the real run keeps no Slack message
    assert (await mock_db[RUNS].find_one({"_id": "r1"})).get("slack") is None
    assert (await mock_db[CARDS].find_one({"_id": "c1"})).get("slack") is None


async def test_weekly_test_falls_back_to_sample_figures(mock_db, slack, installed, stock_api):
    agent = await _agent(mock_db, settings={"slack": SLACK})
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/weekly/test")
    assert r.status_code == 200 and r.json()["source"] == "sample"
    assert "sample figures" in _text(slack.posts[0]["blocks"])


async def test_weekly_test_uses_the_latest_weekly(mock_db, slack, installed, stock_api):
    agent = await _agent(mock_db, settings={"slack": SLACK})
    values = {"week": "27 Oct to 2 Nov", "comparison": "1 improved", "improved": "• Client g1: CPL 9 to 7",
              "decreased": "None", "status_check": "1 of 1 green", "wins": "• Ad 1"}
    await mock_db[RUNS].insert_one(_run(agent, phase="done", weekly={"values": values}))
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/weekly/test")
    assert r.json()["source"] == "latest"
    assert "27 Oct to 2 Nov" in _text(slack.posts[0]["blocks"])


async def test_test_refusals(mock_db, slack, stock_api):
    agent = await _agent(mock_db, settings={"slack": {**SLACK, "channel_id": None}})
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/lunch/test")
    assert r.status_code == 400 and "morning, weekly or card" in r.json()["detail"]
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/weekly/test")
    assert r.status_code == 409 and "No Slack channel" in r.json()["detail"]
    await mock_db["agents"].update_one({"_id": agent["_id"]}, {"$set": {"settings.slack.channel_id": "C1"}})
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/weekly/test")      # Slack not installed
    assert r.status_code == 409 and "Slack isn't connected" in r.json()["detail"]
    assert slack.posts == []


async def test_slack_refusing_the_channel_is_a_409(mock_db, slack, installed, stock_api):
    agent = await _agent(mock_db, settings={"slack": SLACK})
    slack.fail["post"] = ["not_in_channel"]
    r = stock_api.post(f"/api/agents/{agent['_id']}/slack-templates/weekly/test")
    assert r.status_code == 409 and "can't post to this Slack channel" in r.json()["detail"]


async def test_the_chat_tool_previews_then_posts(mock_db, slack, installed, monkeypatch):
    agent = await _agent(mock_db, settings={"slack": SLACK})
    _as(monkeypatch, "media_buyer")
    first = await stock_mcp.send_test_agent_report.fn(kind="morning")
    assert first["needs_confirmation"] and slack.posts == []
    assert first["preview"]["channel"] == "ads" and first["preview"]["figures"] == "sample"
    done = await stock_mcp.send_test_agent_report.fn(kind="morning", confirm_code=first["confirm_code"])
    assert done["ok"] and done["summary"] == "Posted a test of the morning report to #ads."
    assert len(slack.posts) == 1


async def test_the_chat_tool_refuses_before_asking_when_slack_is_not_connected(mock_db, slack, monkeypatch):
    await _agent(mock_db, settings={"slack": SLACK})
    _as(monkeypatch, "media_buyer")
    with pytest.raises(ToolError, match="Slack isn't connected"):
        await stock_mcp.send_test_agent_report.fn(kind="weekly")


async def test_only_marketing_guru_sends_its_reports(mock_db, monkeypatch):
    await _agent(mock_db, settings={"slack": SLACK})
    for caller in ("birdy", "sales_coach"):
        _as(monkeypatch, caller)
        with pytest.raises(ToolError, match="Marketing Guru's reports"):
            await stock_mcp.send_test_agent_report.fn(kind="weekly")
