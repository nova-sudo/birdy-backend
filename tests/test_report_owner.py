"""
Which agent a Slack report belongs to (services/report_owner.py), enforced server-side: Birdy makes
client-level reports only, campaign / ad set / ad / creative level is Marketing Guru's, calls are
Sales Coach's. The report tools and propose_action refuse an agent outside its area, and the
delegated path (Birdy asks Marketing Guru, the owner approves, Marketing Guru makes it) works end to end.
Mongomock and scripted providers only: no model, no live DB, no Slack.
"""

import contextlib
import json
import types

import pytest
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

import credits
from ai import mcp_client
from ai.mcp import slack_reports_mcp as t
from ai.team import definitions as defs
from services import report_blocks as rb
from services import report_owner as ro
from services import slack_reports as sr
from test_agent_team import G1, U, TeamProvider, _use, api, call, groups, registry, say  # noqa: F401

SCHEDULE = {"type": "schedule", "frequency": "daily", "time": "09:00"}


def _tpl(*texts, **binding):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": s}} for s in texts]
    if binding:
        blocks.append({"type": "data_table", "caption": "Table", "birdy_data": binding})
    return {"frequency": "daily", "blocks": blocks}


CLIENT_LEVEL = _tpl("*Spend* {{spend}} · *Leads* {{leads}} · *CPL* {{cpl}}\n*Won* {{ghl_won_opps}} {{ghl_conversion}}",
                    metrics=["spend", "leads", "cpl", "ghl_total_opps", "ghl_won_opps"], sort="spend")
ADS = _tpl("*Spend* {{spend}}\n*Underperforming ads:* {{flagged_ads show=list}}")
CALLS = _tpl("*Booked* {{booked}}", metrics=["booked", "closed", "speed_to_lead"])
MIXED = _tpl("*Ads to pause:* {{flagged_ads}}\n*Booked* {{booked}}")


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("template,name,owner", [
    (rb.default_template(), "Birdy brief", "birdy"),               # the standard brief: client totals
    (CLIENT_LEVEL, "Daily numbers", "birdy"),
    (_tpl("{{ranking metric=cpl order=worst}} {{breakdown metrics=\"spend,leads\"}}"), None, "birdy"),
    (_tpl("🔔 {{alerts}} {{alert_message}} {{metric_name}}"), "Alert fired", "birdy"),  # placeholders aren't words
    (ADS, "Ads watch", "media_buyer"),
    (_tpl("x", metrics=["spend"], level="campaign"), None, "media_buyer"),   # validation would drop the level
    ({"frequency": "daily", "blocks": [{"type": "data_visualization", "title": "Spend",
                                        "birdy_data": {"chart": "bar", "by": "adset", "metrics": ["spend"]}}]},
     None, "media_buyer"),
    (_tpl("{{breakdown metrics=\"spend,cpl\" by=campaign}}"), None, "media_buyer"),
    (_tpl("{{spend}}"), "Campaign performance", "media_buyer"),
    (_tpl("Top creatives this week: {{leads}}"), None, "media_buyer"),
    (_tpl("{{spend}} {{cpl}}"), "Marketing Guru daily", "media_buyer"),     # the owner named the agent
    (CALLS, "Sales", "sales_coach"),
    (_tpl("{{hp_total_calls}} {{hp_answer_rate}}"), None, "sales_coach"),
    (_tpl("{{ranking metric=speed_to_lead}}"), None, "sales_coach"),
    (_tpl("{{leads}}"), "Setter calls", "sales_coach"),
    ({"frequency": "daily", "blocks": json.dumps(CALLS["blocks"])}, None, "sales_coach"),   # blocks as a string
    ({"frequency": "daily", "body": "*Calls* today: {{leads}}"}, None, "sales_coach"),     # free-form body
    (MIXED, None, "mixed"),
    (None, None, "birdy"),
])
def test_report_owner_for(template, name, owner):
    assert ro.report_owner_for(template, name) == owner


def test_reasons_name_what_made_it_theirs():
    area = ro.report_area(MIXED, "Weekly")
    assert area.owner == ro.MIXED and any("flagged_ads" in r for r in area.ads)
    assert any("booked" in r for r in area.calls)


@pytest.mark.parametrize("caller,template,allowed", [
    ("birdy", CLIENT_LEVEL, True), ("birdy", ADS, False), ("birdy", CALLS, False), ("birdy", MIXED, False),
    ("media_buyer", CLIENT_LEVEL, True), ("media_buyer", ADS, True), ("media_buyer", CALLS, False),
    ("media_buyer", MIXED, False),
    ("sales_coach", CLIENT_LEVEL, True), ("sales_coach", CALLS, True), ("sales_coach", ADS, False),
    ("sales_coach", MIXED, False),
])
def test_who_may_make_what(caller, template, allowed):
    assert (ro.refusal(caller, ro.report_area(template)) is None) == allowed


def test_refusals_say_exactly_what_to_do():
    to_birdy = ro.refusal("birdy", ro.report_area(ADS))
    assert "Marketing Guru report" in to_birdy and "ask_media_buyer" in to_birdy and "client-level version" in to_birdy
    assert "ask_sales_coach" in ro.refusal("birdy", ro.report_area(CALLS))
    split = ro.refusal("birdy", ro.report_area(MIXED))
    assert "Split it" in split and "ask_media_buyer" in split and "ask_sales_coach" in split
    coach = ro.refusal("sales_coach", ro.report_area(ADS))
    assert "Marketing Guru report" in coach and "ask_birdy" in coach
    guru = ro.refusal("media_buyer", ro.report_area(MIXED))
    assert "Sales Coach" in guru and "ask_birdy" in guru and "client-level totals can stay" in guru


# ---------------------------------------------------------------------------
# The report tools
# ---------------------------------------------------------------------------

def _as(monkeypatch, agent):
    claims = {"sub": U, "type": "access", **({"agent": agent} if agent else {})}
    monkeypatch.setattr("ai.mcp.server.get_access_token", lambda: types.SimpleNamespace(claims=claims))


async def test_birdy_is_refused_an_ads_report_before_anything_is_saved(mock_db, monkeypatch):
    _as(monkeypatch, "birdy")
    with pytest.raises(ToolError, match="Marketing Guru report.*ask_media_buyer"):
        await t.create_slack_report.fn(name="Ads watch", trigger=SCHEDULE, template=ADS)
    with pytest.raises(ToolError, match="Sales Coach report.*ask_sales_coach"):
        await t.create_slack_report.fn(name="Sales", trigger=SCHEDULE, template=CALLS)
    with pytest.raises(ToolError, match="Marketing Guru report"):
        await t.create_slack_report.fn(name="Marketing Guru report", trigger=SCHEDULE, template=CLIENT_LEVEL)
    assert await mock_db[sr.REPORTS].count_documents({}) == 0
    made = (await t.create_slack_report.fn(name="Daily numbers", trigger=SCHEDULE, template=CLIENT_LEVEL))["report"]
    assert made["owner"] == "birdy"


async def test_each_main_agent_makes_its_own_with_client_totals_but_not_the_others(mock_db, monkeypatch):
    _as(monkeypatch, "media_buyer")
    assert (await t.create_slack_report.fn(name="Ads watch", trigger=SCHEDULE, template=ADS))["report"]["owner"] == "media_buyer"
    with pytest.raises(ToolError, match="Sales Coach report.*ask_birdy"):
        await t.create_slack_report.fn(name="Sales", trigger=SCHEDULE, template=CALLS)
    _as(monkeypatch, "sales_coach")
    assert (await t.create_slack_report.fn(name="Sales", trigger=SCHEDULE, template=CALLS))["report"]["owner"] == "sales_coach"
    with pytest.raises(ToolError, match="Marketing Guru report.*ask_birdy"):
        await t.create_slack_report.fn(name="Campaigns", trigger=SCHEDULE)
    with pytest.raises(ToolError, match="mixes your area with Marketing Guru's"):
        await t.create_slack_report.fn(name="Weekly", trigger=SCHEDULE, template=MIXED)
    assert sorted(r["owner"] for r in await mock_db[sr.REPORTS].find().to_list(10)) == ["media_buyer", "sales_coach"]


async def test_an_update_is_checked_on_what_the_report_would_say(mock_db, monkeypatch):
    _as(monkeypatch, "birdy")
    mine = (await t.create_slack_report.fn(name="Daily numbers", trigger=SCHEDULE, template=CLIENT_LEVEL))["report"]
    with pytest.raises(ToolError, match="Sales Coach report"):
        await t.update_slack_report.fn(report_id=mine["id"], template=CALLS)
    with pytest.raises(ToolError, match="Marketing Guru report"):
        await t.update_slack_report.fn(report_id=mine["id"], name="Campaign numbers")
    # A template without blocks keeps the current content; switching it off checks nothing.
    assert (await t.update_slack_report.fn(report_id=mine["id"], template={"frequency": "weekly"}))["report"]
    assert (await t.update_slack_report.fn(report_id=mine["id"], enabled=False))["report"]["enabled"] is False
    stored = await mock_db[sr.REPORTS].find_one({"id": mine["id"]})
    assert stored["name"] == "Daily numbers" and "booked" not in json.dumps(stored["template"])


async def test_the_owner_outside_an_agent_picks_any_content(mock_db, monkeypatch):
    _as(monkeypatch, None)
    made = (await t.create_slack_report.fn(name="Everything", trigger=SCHEDULE, template=MIXED))["report"]
    assert made["owner"] == "birdy"


# ---------------------------------------------------------------------------
# propose_action and the delegated path, end to end
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_mcp(monkeypatch):
    """The MCP hop as an agent run makes it, in process: the run's agent goes into the token claims,
    and the real Slack report tools run against the mock DB."""
    tools = {n: getattr(t, n) for n in ("list_slack_reports", "create_slack_report", "update_slack_report")}
    calls = []

    @contextlib.asynccontextmanager
    async def client_for(user_id, agent=None):
        yield types.SimpleNamespace(user_id=user_id, agent=agent)

    async def schemas(client, allowed):
        return [{"type": "function", "function": {"name": n, "description": tool.description,
                                                  "parameters": tool.parameters}}
                for n, tool in tools.items() if allowed is None or n in allowed]

    async def call_tool(client, name, args):
        calls.append((client.agent, name, args))
        _as(monkeypatch, client.agent)
        try:
            return json.dumps(await tools[name].fn(**args), default=str)
        except ToolError as e:
            return mcp_client.error_payload(name, str(e))

    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: True)
    monkeypatch.setattr("ai.orchestrator.mcp_client.mcp_client_for", client_for)
    monkeypatch.setattr("ai.orchestrator.mcp_client.get_mcp_schemas", schemas)
    monkeypatch.setattr("ai.orchestrator.mcp_client.call_mcp_tool", call_tool)
    credits._settings_cache["data"] = None
    yield calls
    credits._settings_cache["data"] = None


def _tool_call(tool, args):
    from ai.providers.base import ProviderResponse, ToolCall, Usage
    return ProviderResponse(content=None, tool_calls=[ToolCall(id=f"tc-{tool}", name=tool, arguments=json.dumps(args))],
                            finish_reason="tool_calls", usage=Usage(1000, 100))


def _payload(template, name="Aura ads watch"):
    return {"name": name, "trigger": SCHEDULE, "template": template}


def test_birdy_delegates_and_marketing_guru_makes_it_as_its_own(api, monkeypatch, registry, mock_db, groups, fake_mcp):
    chat_router, app = api
    p = TeamProvider({
        "birdy": [_tool_call("create_slack_report", _payload(ADS)),           # refused: not hers
                  call("ask_media_buyer", question="Create a daily ads report for Aura with its flagged ads",
                       client=G1),
                  say("Marketing Guru proposed it; approve its card.")],
        "media_buyer": [call("propose_action", title="New Slack report: Aura ads watch", detail="daily",
                             kind="create_slack_report", payload=_payload(ADS)),
                        say("Proposed the report.")],
    })
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    first = client.post("/api/chat", json={"message": "make a Marketing Guru report for Aura", "agent": "birdy"}).json()
    refused = next(m for m in p.calls if m[0] == "birdy" and any(x.get("role") == "tool" for x in m[2]))[2]
    assert "ask_media_buyer" in next(x["content"] for x in refused if x.get("role") == "tool")
    assert len(first["proposals"]) == 1
    prop = first["proposals"][0]
    assert prop["agent"] == "media_buyer" and prop["kind"] == "create_slack_report" and prop["client_group_id"] == G1

    p.scripts["media_buyer"] = [_tool_call("create_slack_report", _payload(ADS)), say("Made it: Aura ads watch.")]
    out = client.post(f"/api/chat/proposals/{prop['id']}/apply")
    assert out.status_code == 200
    owner_turn = [c for c in p.calls if c[0] == "media_buyer"][-1]
    # Run agency-wide, so the report tools are offered even though the ask was about one client.
    assert "create_slack_report" in owner_turn[1] and "propose_action" not in owner_turn[1]
    reports = await_list(mock_db)
    assert [(r["name"], r["owner"]) for r in reports] == [("Aura ads watch", "media_buyer")]
    assert [(a, n) for a, n, _ in fake_mcp] == [("birdy", "create_slack_report"), ("media_buyer", "create_slack_report")]


def await_list(db):
    """The stored reports, read from this sync test (the app ran on TestClient's own loop)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(db[sr.REPORTS].find({}, {"_id": 0}).to_list(10))
    finally:
        loop.close()


async def test_a_proposal_outside_the_agents_area_is_refused_at_once(mock_db, registry):
    from test_agent_team import chat
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="Make a campaign report", client="agency"), say("ok")],
        "sales_coach": [call("propose_action", title="Campaign report", detail="d", kind="create_slack_report",
                             payload=_payload(ADS, "Campaigns")),
                        call("propose_action", title="Sales report", detail="d", kind="create_slack_report",
                             payload=_payload(CALLS, "Sales")),
                        say("done")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    assert [x["title"] for x in out["proposals"]] == ["Sales report"]
    tool_msgs = [m for r, _, msgs in p.calls if r == "sales_coach" for m in msgs if m.get("role") == "tool"]
    assert any("Marketing Guru report" in m["content"] and "ask_birdy" in m["content"] for m in tool_msgs)


def test_list_report_metrics_is_read_in_a_one_client_ask():
    assert "list_report_metrics" in defs.tool_names(defs.MEDIA_BUYER, nested=True, one_client=True)
    assert "create_slack_report" not in defs.tool_names(defs.MEDIA_BUYER, nested=False, one_client=True)
