"""
tests/test_agent_team.py
------------------------
The agent team (ai/team): agent definitions, ask tools (hub and spoke), nested
runs, limits, client scope, read-and-propose, the route in the response, the
usage record and the streaming endpoint. Everything runs on mongomock with
scripted providers: no model calls, no live DB.
"""

import asyncio
import contextlib
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import credits
from ai.orchestrator import run_chat
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall, Usage
from ai.team import definitions as defs
from ai.team import delegation
from ai.team import route as rt
from ai.team import sub_agents
from ai.tools.registry import ToolRegistry
from ai.tools.stock_card_tools import current_card_read_only, current_stock_card

U = "owner@example.com"
G1, G2 = "grp-aura", "grp-beaulea"

ROUTE_KEYS = {"status", "seconds", "credits", "limit_hit", "steps"}
STEP_KEYS = {"id", "agent", "name", "kind", "status", "summary", "question", "asked_by", "tools",
             "answer", "credits", "seconds"}
TOOL_KEYS = {"name", "label", "detail"}
PROPOSAL_KEYS = {"id", "agent", "title", "detail", "kind", "payload", "status"}


# ── scripted provider: one script per agent, picked from the system prompt ───

def role_of(messages) -> str:
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "a sub-agent working for" in system:
        return "sub:" + system.split("You are ", 1)[1].split(",", 1)[0]
    for key, marker in (("birdy", "Your role: Birdy"), ("media_buyer", "Your role: Media Buyer"),
                        ("sales_coach", "Your role: Sales Coach")):
        if marker in system:
            return key
    return "default"


class TeamProvider(BaseLLMProvider):
    def __init__(self, scripts: dict, delays: dict | None = None):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.delays = delays or {}
        self.model = "gpt-4o"
        self.calls = []   # (role, tool names offered, messages)

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
        role = role_of(messages)
        self.calls.append((role, [t["function"]["name"] for t in tools or []], messages))
        if self.delays.get(role):
            await asyncio.sleep(self.delays[role])
        script = self.scripts.get(role) or []
        if not script:
            return ProviderResponse(content=f"{role} has nothing more to say.", usage=Usage(10, 5))
        return script.pop(0)

    def offered(self, role) -> list:
        return next(t for r, t, _ in self.calls if r == role)


def call(name, **args):
    return ProviderResponse(content=None, tool_calls=[ToolCall(id=f"tc-{name}", name=name,
                                                               arguments=json.dumps(args))],
                            finish_reason="tool_calls", usage=Usage(1000, 100))


def say(text):
    return ProviderResponse(content=text, usage=Usage(1000, 100))


@pytest.fixture
def registry():
    """Stub tools that record what they were called with."""
    reg = ToolRegistry()
    seen = {}

    def stub(name, result):
        async def run(db, user_id, mongo_client=None, **kwargs):
            seen.setdefault(name, []).append(kwargs)
            return result
        reg.register(name=name, description="stub", parameters={"type": "object", "properties": {}},
                     executor=run)

    stub("get_client_groups", {"client_groups": [{"id": G1, "name": "Aura"}, {"id": G2, "name": "Beaulea"}],
                               "total": 2})
    stub("get_call_center_stats", {"total_calls": 212})
    stub("get_call_recordings_summary", {"recordings": 38})
    stub("analyze_call_recordings", {"ok": True})
    stub("get_campaign_insights", {"campaigns": []})
    stub("get_account_summary", {"spend": 100})
    stub("create_alert", {"ok": True})
    stub("get_ghl_opp_stats_windowed", {"won": 1})
    reg.seen = seen
    return reg


@pytest.fixture(autouse=True)
def isolate(monkeypatch, mock_db):
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


@pytest.fixture
async def groups(mock_db):
    await mock_db["client_groups"].insert_many([{"user_id": U, "id": G1, "name": "Aura"},
                                                {"user_id": U, "id": G2, "name": "Beaulea"}])


async def chat(provider, registry, db, message="hi", **kw):
    return await run_chat(provider=provider, tool_registry=registry, db=db, user_id=U, message=message, **kw)


def assert_route_shape(route):
    assert set(route) == ROUTE_KEYS
    assert route["status"] in ("working", "done", "failed")
    assert route["limit_hit"] in (None, "asks", "depth", "repeat")
    for s in route["steps"]:
        assert set(s) == STEP_KEYS
        assert s["kind"] in ("orchestrator", "main", "sub")
        assert s["status"] in ("working", "done", "failed", "skipped")
        assert len(s["summary"]) <= 60
        for t in s["tools"]:
            assert set(t) == TOOL_KEYS


# ── 1. default behaviour unchanged ────────────────────────────────────────────

async def test_without_agent_nothing_changes(mock_db, registry):
    p = TeamProvider({"default": [say("All good.")]})
    out = await chat(p, registry, mock_db)
    assert out["reply"] == "All good."
    assert "route" not in out and "proposals" not in out
    offered = p.offered("default")
    assert not set(offered) & delegation.TOOL_NAMES
    assert "create_alert" in offered     # the general chat still has every tool
    row = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert "agent" not in row and "route" not in row


async def test_without_agent_the_ledger_row_is_unchanged(mock_db, registry):
    await mock_db["users"].insert_one({"user_id": U, "credits": {**credits._effective_credits({})[0],
                                                                  "topup_balance": 1000.0}})
    await chat(TeamProvider({"default": [say("ok")]}), registry, mock_db)
    row = await mock_db["ai_usage"].find_one({})
    assert row["feature"] == "ask_birdy" and not {"agent", "depth", "agents"} & set(row)


def test_chat_models_default_and_validate():
    from pydantic import ValidationError
    from core.models import ChatRequest, ChatResponse
    assert ChatRequest(message="m").agent is None
    assert ChatRequest(message="m", agent="sales_coach").agent == "sales_coach"
    with pytest.raises(ValidationError):
        ChatRequest(message="m", agent="sub:x")
    r = ChatResponse(reply="x")
    assert r.route is None and r.proposals == []


# ── 2. tool lists per agent ───────────────────────────────────────────────────

def test_sales_coach_only_has_call_tools():
    names = set(defs.tool_names(defs.SALES_COACH, nested=False))
    assert names == {"get_client_groups", "get_call_center_stats", "get_call_trend", "get_call_recordings_summary",
                     "analyze_call_recordings", "render_ui",
                     "list_lessons", "save_lesson", "edit_lesson", "remove_lesson"}   # its own memory


def test_media_buyer_has_no_call_or_pipeline_tools():
    names = set(defs.tool_names(defs.MEDIA_BUYER, nested=False))
    assert {"get_campaign_insights", "get_ad_insights", "list_creative_tests", "post_creative_test",
            "list_lessons"} <= names
    assert not names & {"get_call_center_stats", "get_call_recordings_summary", "analyze_call_recordings",
                        "get_ghl_opp_stats_windowed", "get_unified_leads"}


def test_birdy_reads_across_areas_and_writes_only_agency_settings():
    names = set(defs.tool_names(defs.BIRDY, nested=False))
    assert {"get_account_summary", "get_call_center_stats", "get_campaign_insights"} - names == {"get_campaign_insights"}
    assert names & defs.WRITE_TOOLS == set(defs.BIRDY_WRITE)
    # never an ads or a call-centre write
    assert not names & (set(defs.MEDIA_BUYER_WRITE) | set(defs.SALES_COACH_WRITE))
    # asked by another agent, Birdy only reads
    assert not set(defs.tool_names(defs.BIRDY, nested=True)) & defs.WRITE_TOOLS


async def test_offered_tools_per_agent(mock_db, registry):
    p = TeamProvider({})
    for agent in defs.AGENT_KEYS:
        await chat(p, registry, mock_db, agent=agent)
    birdy, mb, sc = p.offered("birdy"), p.offered("media_buyer"), p.offered("sales_coach")
    assert {"ask_media_buyer", "ask_sales_coach"} <= set(birdy) and "ask_birdy" not in birdy
    assert "ask_birdy" in mb and "ask_sales_coach" not in mb and "ask_media_buyer" not in mb
    assert "ask_birdy" in sc and "ask_media_buyer" not in sc and "ask_sales_coach" not in sc
    assert "get_call_center_stats" in sc and "get_call_center_stats" not in mb
    assert "create_alert" in birdy and "create_alert" not in mb + sc


# ── 3. ask down, ask up ───────────────────────────────────────────────────────

async def test_birdy_asks_sales_coach(mock_db, registry):
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="How did calls go last week?", client="agency"),
                  say("**Calls are fine.** Sales Coach checked 212 calls.")],
        "sales_coach": [call("get_call_center_stats", date_preset="last_7d"),
                        say("212 calls, connect rate 41%.\nSUMMARY: 212 calls checked")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    route = out["route"]
    assert_route_shape(route)
    assert [s["agent"] for s in route["steps"]] == ["birdy", "sales_coach"]
    root, sc = route["steps"]
    assert root["kind"] == "orchestrator" and root["question"] is None and root["asked_by"] is None
    assert sc["kind"] == "main" and sc["asked_by"] == "birdy" and sc["question"] == "How did calls go last week?"
    assert sc["status"] == "done" and sc["summary"] == "212 calls checked"
    assert sc["answer"] == "212 calls, connect rate 41%."
    assert sc["tools"] == [{"name": "get_call_center_stats", "label": "Call stats", "detail": "last_7d"}]
    assert root["tools"][0]["name"] == "ask_sales_coach" and root["tools"][0]["label"] == "Asked Sales Coach"
    assert route["status"] == "done" and route["limit_hit"] is None
    # the child's answer reached Birdy as the tool result
    tool_msg = next(m for m in p.calls[-1][2] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["answer"] == "212 calls, connect rate 41%."


async def test_media_buyer_asks_up_through_birdy(mock_db, registry):
    p = TeamProvider({
        "media_buyer": [call("ask_birdy", question="Are the calls converting for Aura?", client="agency"),
                        say("Ads are fine; Birdy says the calls are the problem.")],
        "birdy": [call("ask_sales_coach", question="Aura call conversion last 7 days?", client="agency"),
                  say("Sales Coach: 3 of 40 calls booked.")],
        "sales_coach": [say("3 of 40 calls booked.\nSUMMARY: 40 calls checked")],
    })
    out = await chat(p, registry, mock_db, agent="media_buyer")
    steps = out["route"]["steps"]
    assert [(s["agent"], s["asked_by"]) for s in steps] == [
        ("media_buyer", None), ("birdy", "media_buyer"), ("sales_coach", "birdy")]
    assert [s["kind"] for s in steps] == ["main", "orchestrator", "main"]
    # Birdy, asked by Media Buyer, is never offered an ask back to Media Buyer
    assert "ask_media_buyer" not in p.offered("birdy") and "ask_sales_coach" in p.offered("birdy")
    # Sales Coach at the end of the chain can't ask Birdy again
    assert "ask_birdy" not in p.offered("sales_coach")


async def test_main_agents_cannot_ask_each_other(mock_db, registry):
    p = TeamProvider({"media_buyer": [call("ask_sales_coach", question="q", client="agency"), say("ok")]})
    out = await chat(p, registry, mock_db, agent="media_buyer")
    tool_msg = next(m for m in p.calls[-1][2] if m["role"] == "tool")
    assert "isn't available" in json.loads(tool_msg["content"])["error"]
    assert out["route"] is None


async def test_no_route_when_no_other_agent_involved(mock_db, registry):
    p = TeamProvider({"sales_coach": [call("get_call_center_stats"), say("212 calls.")]})
    out = await chat(p, registry, mock_db, agent="sales_coach")
    assert out["route"] is None and out["proposals"] == []


# ── 4. nested runs save, archive and debit once ───────────────────────────────

async def test_nested_run_saves_and_debits_once(mock_db, registry):
    await mock_db["users"].insert_one({"user_id": U, "credits": {**credits._effective_credits({})[0],
                                                                  "topup_balance": 1000.0}})
    p = TeamProvider({
        "birdy": [call("ask_media_buyer", question="Why is Aura's CPL up?", client="agency"), say("CPL up on one ad.")],
        "media_buyer": [call("get_campaign_insights"), say("One ad doubled its CPL.\nSUMMARY: 4 campaigns checked")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    assert await mock_db["ai_chat_sessions"].count_documents({}) == 1
    assert await mock_db["ai_conversation_log"].count_documents({}) == 2      # one question, one answer
    rows = await mock_db["ai_usage"].find({}).to_list(None)
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "birdy" and row["depth"] == 0
    assert [(a["agent"], a["depth"]) for a in row["agents"]] == [("birdy", 0), ("media_buyer", 1)]
    # 4 model calls in all (2 per agent), summed into the single charge
    assert row["model_calls"] == 4 and row["prompt_tokens"] == 4000 and row["completion_tokens"] == 400
    assert out["route"]["credits"] == row["credits"] > 0
    steps = out["route"]["steps"]
    assert steps[1]["credits"] > 0 and steps[0]["credits"] > 0
    archived = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert archived["agent"] == "birdy" and archived["route"]["steps"][1]["agent"] == "media_buyer"


async def test_child_does_not_inherit_request_flags(mock_db, registry):
    seen = {}

    async def peek(db, user_id, mongo_client=None, **kw):
        seen["card"], seen["read_only"] = current_stock_card.get(), current_card_read_only.get()
        return {"ok": True}
    registry.register(name="get_campaign_insights", description="stub",
                      parameters={"type": "object", "properties": {}}, executor=peek)
    p = TeamProvider({"birdy": [call("ask_media_buyer", question="q", client="agency"), say("done")],
                      "media_buyer": [call("get_campaign_insights"), say("ok")]})
    card_t = current_stock_card.set("card-123")
    ro_t = current_card_read_only.set(False)
    try:
        await chat(p, registry, mock_db, agent="birdy")
        assert seen == {"card": None, "read_only": True}
        assert current_stock_card.get() == "card-123" and current_card_read_only.get() is False
    finally:
        current_card_read_only.reset(ro_t)
        current_stock_card.reset(card_t)


# ── 5. limits ─────────────────────────────────────────────────────────────────

def _subs(n):
    return [{"id": f"s{i}", "name": f"Checker {i}", "instructions": "check", "tools": ["get_campaign_insights"]}
            for i in range(1, n + 1)]


async def test_asks_limit(mock_db, registry, monkeypatch):
    async def source(db, parent, user_id):
        return _subs(4) if parent == "media_buyer" else []
    monkeypatch.setattr(sub_agents, "_source", source)
    p = TeamProvider({
        "media_buyer": [call("ask_sub_agent", sub_agent_id=f"s{i}", question=f"q{i}", client="agency")
                        for i in range(1, 5)] + [say("Here is what I have.")],
    })
    out = await chat(p, registry, mock_db, agent="media_buyer")
    route = out["route"]
    assert_route_shape(route)
    assert route["limit_hit"] == "asks"
    assert [s["status"] for s in route["steps"]] == ["done", "done", "done", "done", "skipped"]
    assert [s["kind"] for s in route["steps"]] == ["main", "sub", "sub", "sub", "sub"]
    assert route["steps"][1]["agent"] == "sub:s1" and route["steps"][1]["name"] == "Checker 1"
    assert out["reply"] == "Here is what I have."


async def test_repeat_limit_returns_earlier_answer(mock_db, registry):
    p = TeamProvider({
        "birdy": [call("ask_media_buyer", question="q1", client="agency"),
                  call("ask_media_buyer", question="q2", client="agency"), say("done")],
        "media_buyer": [say("First answer.")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["route"]["limit_hit"] == "repeat"
    assert len(out["route"]["steps"]) == 2
    second = [m for m in p.calls[-1][2] if m["role"] == "tool"][1]
    body = json.loads(second["content"])
    assert body["limit_hit"] == "repeat" and body["earlier_answer"] == "First answer."


async def test_depth_limit(mock_db, registry):
    frame = rt.new_turn("media_buyer")
    deep = rt.Frame(turn=frame.turn, agent="media_buyer", depth=rt.MAX_DEPTH, chain=("x", "y", "z", "media_buyer"),
                    step=frame.step, usage=frame.usage)
    sub = sub_agents.coerce(_subs(1)[0], "media_buyer")
    res = json.loads(await delegation.handle("ask_sub_agent", {"sub_agent_id": "s1", "question": "q",
                                                               "client": "agency"},
                                             deep, {"db": mock_db, "user_id": U, "subs": [sub]}))
    assert res["limit_hit"] == "depth" and frame.turn.limit_hit == "depth"
    assert frame.turn.steps[-1]["status"] == "skipped"


async def test_longest_chain_reaches_a_sub_agent(mock_db, registry, monkeypatch):
    async def source(db, parent, user_id):
        return _subs(1) if parent == "sales_coach" else []
    monkeypatch.setattr(sub_agents, "_source", source)
    p = TeamProvider({
        "media_buyer": [call("ask_birdy", question="calls?", client="agency"), say("done")],
        "birdy": [call("ask_sales_coach", question="calls?", client="agency"), say("done")],
        "sales_coach": [call("ask_sub_agent", sub_agent_id="s1", question="check", client="agency"), say("done")],
        "sub:Checker 1": [call("get_call_center_stats"), say("fine")],
    })
    out = await chat(p, registry, mock_db, agent="media_buyer")
    steps = out["route"]["steps"]
    assert [s["agent"] for s in steps] == ["media_buyer", "birdy", "sales_coach", "sub:s1"]
    # the sub-agent asked for get_campaign_insights, but its main agent (Sales Coach) has no such tool
    assert p.offered("sub:Checker 1") == ["propose_action"]
    assert steps[3]["tools"][0]["name"] == "get_call_center_stats"
    last_sub = [c for c in p.calls if c[0] == "sub:Checker 1"][-1]
    refused = json.loads(next(m for m in last_sub[2] if m["role"] == "tool")["content"])
    assert "isn't available" in refused["error"]
    assert "get_call_center_stats" not in registry.seen


async def test_ask_timeout_degrades(mock_db, registry, monkeypatch):
    monkeypatch.setattr(rt, "ASK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(rt, "MIN_ASK_SECONDS", 0.05)
    p = TeamProvider({"birdy": [call("ask_sales_coach", question="q", client="agency"), say("Partial answer.")],
                      "sales_coach": [say("too late")]}, delays={"sales_coach": 2})
    out = await chat(p, registry, mock_db, agent="birdy")
    sc = out["route"]["steps"][1]
    assert sc["status"] == "failed" and sc["summary"] == "Ran out of time" and sc["answer"] is None
    assert out["reply"] == "Partial answer." and out["route"]["status"] == "done"
    body = json.loads(next(m for m in p.calls[-1][2] if m["role"] == "tool")["content"])
    assert body["error"] == "timeout"


async def test_no_ask_starts_without_time_left(mock_db, registry, monkeypatch):
    # Time left for the parent's own loop, none for an ask.
    monkeypatch.setattr(rt, "PARENT_RESERVE_SECONDS", rt.TURN_BUDGET_SECONDS)
    p = TeamProvider({"birdy": [call("ask_sales_coach", question="q", client="agency"), say("ok")]})
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["route"]["steps"][1]["status"] == "failed"
    assert not [c for c in p.calls if c[0] == "sales_coach"]


# ── 6. client scope ───────────────────────────────────────────────────────────

async def test_child_inherits_client_and_tools_are_pinned(mock_db, registry, groups):
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="Calls for Beaulea?", client=G2), say("done")],
        "sales_coach": [call("get_call_center_stats", group_ids=[G2]), call("get_client_groups"), say("ok")],
    })
    await chat(p, registry, mock_db, agent="birdy", client_group_id=G1, client_name="Aura")
    assert registry.seen["get_call_center_stats"][-1]["group_ids"] == [G1]
    system = p.calls[[c[0] for c in p.calls].index("sales_coach")][2][0]["content"]
    assert f'"Aura" (group ID: {G1})' in system
    last_sc = [c for c in p.calls if c[0] == "sales_coach"][-1]
    client_list = json.loads([m for m in last_sc[2] if m["role"] == "tool"][-1]["content"])
    assert [g["id"] for g in client_list["client_groups"]] == [G1]


async def test_agency_ask_carries_one_client(mock_db, registry, groups):
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="Calls for Beaulea?", client=G2), say("done")],
        "sales_coach": [call("get_call_center_stats"), say("ok")],
    })
    await chat(p, registry, mock_db, agent="birdy")
    assert registry.seen["get_call_center_stats"][-1]["group_ids"] == [G2]


async def test_ask_with_unknown_client_is_refused(mock_db, registry, groups):
    p = TeamProvider({"birdy": [call("ask_sales_coach", question="q", client="grp-not-mine"), say("ok")]})
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["route"] is None
    body = json.loads(next(m for m in p.calls[-1][2] if m["role"] == "tool")["content"])
    assert body["ok"] is False and "get_client_groups" in body["error"]


async def test_top_level_agent_run_pins_more_tools(mock_db, registry, groups):
    p = TeamProvider({"birdy": [call("get_ghl_opp_stats_windowed", group_id=G2), say("ok")]})
    await chat(p, registry, mock_db, agent="birdy", client_group_id=G1, client_name="Aura")
    assert registry.seen["get_ghl_opp_stats_windowed"][-1]["group_id"] == G1


# ── 7. read and propose ───────────────────────────────────────────────────────

async def test_nested_run_has_no_write_tools_and_proposes(mock_db, registry):
    p = TeamProvider({
        "birdy": [call("ask_sales_coach", question="Analyze Aura's calls", client="agency"), say("See the proposal.")],
        "sales_coach": [call("analyze_call_recordings", group_id=G1, confirmed=True, limit=10),
                        call("propose_action", title="Analyze 10 Aura calls", detail="38 recorded calls this week",
                             kind="analyze_call_recordings", payload={"group_id": G1, "limit": 10}),
                        say("Proposed an analysis of 10 calls.")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    offered = p.offered("sales_coach")
    assert not set(offered) & defs.WRITE_TOOLS and "propose_action" in offered
    assert "analyze_call_recordings" not in registry.seen      # refused, never executed
    assert len(out["proposals"]) == 1
    prop = out["proposals"][0]
    assert PROPOSAL_KEYS <= set(prop)
    assert prop["agent"] == "sales_coach" and prop["kind"] == "analyze_call_recordings"
    assert prop["status"] == "pending" and prop["payload"] == {"group_id": G1, "limit": 10}
    archived = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert archived["proposals"][0]["id"] == prop["id"]


async def test_top_level_agent_keeps_its_own_write_tools(mock_db, registry):
    p = TeamProvider({})
    await chat(p, registry, mock_db, agent="sales_coach")
    offered = p.offered("sales_coach")
    assert "analyze_call_recordings" in offered and "propose_action" not in offered


async def test_unknown_proposal_kind_becomes_other(mock_db, registry):
    p = TeamProvider({
        "birdy": [call("ask_media_buyer", question="q", client="agency"), say("ok")],
        "media_buyer": [call("propose_action", title="Call the client", detail="d", kind="send_email"), say("ok")],
    })
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["proposals"][0]["kind"] == "other" and out["proposals"][0]["agent"] == "media_buyer"


# ── 8. route helpers ──────────────────────────────────────────────────────────

def test_summaries_are_short_and_plain():
    assert rt.split_summary("Answer.\nSUMMARY: 212 calls checked") == ("Answer.", "212 calls checked")
    assert len(rt.summarize("**Aura** " + "word " * 40)) <= 60
    assert rt.summarize("**Aura is fine.** More here.") == "Aura is fine."


# ── 9. HTTP: /api/chat and /api/chat/stream ───────────────────────────────────

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    import routers.chat as chat_router

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def no_gate(user, client):
        return None

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", no_gate)
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[chat_router.get_current_user] = lambda: U
    return chat_router, app


def _use(monkeypatch, chat_router, provider, registry):
    async def get_provider(user, db):
        return provider
    monkeypatch.setattr(chat_router, "get_provider_for_user", get_provider)
    monkeypatch.setattr(chat_router, "registry", registry)


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = [ln for ln in block.split("\n") if not ln.startswith(":")]
        if not lines:
            continue
        event = lines[0].removeprefix("event: ")
        out.append((event, json.loads(lines[1].removeprefix("data: "))))
    return out


def _script():
    return TeamProvider({
        "birdy": [call("ask_sales_coach", question="Calls?", client="agency"), say("Calls are fine.")],
        "sales_coach": [call("get_call_center_stats"), say("212 calls.\nSUMMARY: 212 calls checked")],
    })


def test_chat_endpoint_returns_route(api, monkeypatch, registry):
    chat_router, app = api
    _use(monkeypatch, chat_router, _script(), registry)
    body = TestClient(app).post("/api/chat", json={"message": "how are calls?", "agent": "birdy"}).json()
    assert body["reply"] == "Calls are fine."
    assert_route_shape(body["route"])
    assert body["proposals"] == []
    assert TestClient(app).post("/api/chat", json={"message": "x", "agent": "nobody"}).status_code == 422


def test_chat_endpoint_without_agent_has_null_route(api, monkeypatch, registry):
    chat_router, app = api
    _use(monkeypatch, chat_router, TeamProvider({"default": [say("hi")]}), registry)
    body = TestClient(app).post("/api/chat", json={"message": "hi"}).json()
    assert body["route"] is None and body["proposals"] == [] and body["reply"] == "hi"


def test_stream_event_order(api, monkeypatch, registry):
    chat_router, app = api
    _use(monkeypatch, chat_router, _script(), registry)
    with TestClient(app).stream("POST", "/api/chat/stream", json={"message": "how are calls?", "agent": "birdy"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _events(r.read().decode())
    kinds = [e for e, _ in events]
    assert kinds[-1] == "final" and set(kinds[:-1]) == {"route"} and len(kinds) >= 4
    routes = [d for e, d in events if e == "route"]
    for rte in routes:
        assert_route_shape(rte)
    # agent starts -> tool finishes -> agent answers -> reply done
    assert routes[0]["steps"][1]["status"] == "working"
    assert any(r["steps"][1]["tools"] for r in routes if len(r["steps"]) > 1)
    assert routes[-1]["status"] == "done"
    final = events[-1][1]
    assert final["reply"] == "Calls are fine." and final["route"]["steps"][1]["summary"] == "212 calls checked"
    assert set(final) >= {"reply", "tools_used", "session_id", "route", "proposals", "scope"}
    assert final["route"]["steps"] == routes[-1]["steps"]


def test_stream_error_event(api, monkeypatch, registry):
    chat_router, app = api

    class Broken(TeamProvider):
        async def chat_completion(self, *a, **k):
            raise RuntimeError("boom")
    _use(monkeypatch, chat_router, Broken({}), registry)
    with TestClient(app).stream("POST", "/api/chat/stream", json={"message": "x", "agent": "birdy"}) as r:
        events = _events(r.read().decode())
    assert events == [("error", {"message": "Failed to process chat message"})]


def test_stream_keeps_status_codes(api, monkeypatch, registry):
    chat_router, app = api
    from fastapi import HTTPException

    async def out_of_credits(user, client):
        raise HTTPException(status_code=402, detail="OUT_OF_CREDITS")
    monkeypatch.setattr(chat_router, "check_credits", out_of_credits)
    _use(monkeypatch, chat_router, _script(), registry)
    assert TestClient(app).post("/api/chat/stream", json={"message": "x", "agent": "birdy"}).status_code == 402


# ── 10. approving a proposal hands it to the owning agent ─────────────────────

def _proposing_script():
    return TeamProvider({
        "birdy": [call("ask_sales_coach", question="Analyze Aura's calls", client="agency"), say("See the card.")],
        "sales_coach": [call("propose_action", title="Analyze 10 Aura calls", detail="38 recorded",
                             kind="analyze_call_recordings", payload={"group_id": G1, "limit": 10}),
                        say("Proposed it.")],
    })


def test_apply_proposal_runs_the_owner_agent(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    p = _proposing_script()
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    first = client.post("/api/chat", json={"message": "calls?", "agent": "birdy"}).json()
    pid = first["proposals"][0]["id"]
    p.scripts["sales_coach"] = [say("This shows a preview first: 10 calls, about 25 credits. Go ahead?")]
    out = client.post(f"/api/chat/proposals/{pid}/apply").json()
    owner_call = [c for c in p.calls if c[0] == "sales_coach"][-1]
    # a top-level Sales Coach turn: its own write tool is offered, with its own confirm flow
    assert "analyze_call_recordings" in owner_call[1] and "propose_action" not in owner_call[1]
    user_turn = next(m for m in owner_call[2] if m["role"] == "user")
    assert user_turn["content"].startswith("[APPROVED_PROPOSAL]") and '"limit": 10' in user_turn["content"]
    assert out["session_id"] != first["session_id"] and out["reply"].endswith("Go ahead?")
    # approved once only
    assert client.post(f"/api/chat/proposals/{pid}/apply").status_code == 404
    convs = client.get("/api/chat/conversations", params={"agent": "sales_coach"}).json()["conversations"]
    assert [c["session_id"] for c in convs] == [out["session_id"]]
    # the unfiltered list says which agent each chat is with, so each section can show its own
    every = client.get("/api/chat/conversations").json()["conversations"]
    assert {c["session_id"]: c["agent"] for c in every}[out["session_id"]] == "sales_coach"
    msgs = client.get(f"/api/chat/conversations/{first['session_id']}").json()["messages"]
    assert msgs[-1]["proposals"][0]["status"] == "approved" and msgs[-1]["route"]["steps"]


def test_dismiss_proposal(api, monkeypatch, registry):
    chat_router, app = api
    _use(monkeypatch, chat_router, _proposing_script(), registry)
    client = TestClient(app)
    pid = client.post("/api/chat", json={"message": "calls?", "agent": "birdy"}).json()["proposals"][0]["id"]
    assert client.post(f"/api/chat/proposals/{pid}/dismiss").json()["status"] == "dismissed"
    assert client.post(f"/api/chat/proposals/{pid}/apply").status_code == 404
    assert client.post("/api/chat/proposals/p_nope/dismiss").status_code == 404


async def test_stream_is_not_buffered_by_the_app_middleware(mock_mongo_client, monkeypatch, registry):
    """Through main.app's whole middleware stack (CORS, token refresh, ETag), route
    events reach the client while the turn is still running, not all at the end."""
    import time

    import main
    import routers.chat as chat_router

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def no_gate(user, client):
        return None
    p = _script()
    p.delays = {"birdy": 0.3}
    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", no_gate)
    _use(monkeypatch, chat_router, p, registry)
    main.app.dependency_overrides[chat_router.get_current_user] = lambda: U
    body = json.dumps({"message": "calls?", "agent": "birdy"}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/api/chat/stream", "raw_path": b"/api/chat/stream", "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"host", b"test")],
             "client": ("t", 1), "server": ("test", 80), "root_path": ""}
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.sleep(10)
        return {"type": "http.disconnect"}

    chunks = []
    t0 = time.monotonic()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            chunks.append((time.monotonic() - t0, message["body"].decode()))
    try:
        await main.app(scope, receive, send)
    finally:
        main.app.dependency_overrides.pop(chat_router.get_current_user, None)
    first_route = next(t for t, c in chunks if c.startswith("event: route"))
    final = next(t for t, c in chunks if c.startswith("event: final"))
    assert final - first_route >= 0.25     # Birdy's last model call (0.3 s) came after the first route event


def test_main_agent_is_offered_propose_sub_agent_only_in_its_own_chat():
    from ai.team import delegation, route as rt

    def names(agent):
        return {t["function"]["name"] for t in delegation.tool_schemas(rt.new_turn(agent), [])}

    assert delegation.PROPOSE_SUB_AGENT in names("media_buyer")
    assert delegation.PROPOSE_SUB_AGENT in names("sales_coach")
    assert delegation.PROPOSE_SUB_AGENT not in names("birdy")


# ── 11. the top-level agent's own loop stops on time ──────────────────────────

async def test_parent_loop_answers_when_the_reply_is_out_of_time(mock_db, registry, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])

    class Slow(TeamProvider):
        async def chat_completion(self, messages, tools=None, **kw):
            clock["t"] += 100       # every model call takes 100 s
            return await super().chat_completion(messages, tools=tools, **kw)

    p = Slow({"birdy": [call("get_account_summary"), call("get_account_summary"), call("get_account_summary"),
                        say("Here is what I have.")]})
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["reply"] == "Here is what I have."
    birdy_calls = [c for c in p.calls if c[0] == "birdy"]
    # before the calls at 0 s, 100 s, 200 s tools are offered; the third call ends at 300 s, past the
    # 250 s budget, so its tool is not run and the fourth call answers without tools
    assert [bool(t) for _, t, _ in birdy_calls] == [True, True, True, False]
    assert "out of time" in birdy_calls[-1][2][0]["content"]
    assert len(registry.seen["get_account_summary"]) == 2


async def test_parent_loop_skips_tools_once_out_of_time(mock_db, registry, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])

    async def slow_tool(db, user_id, mongo_client=None, **kw):
        clock["t"] += 230
        return {"spend": 1}
    registry.register(name="get_account_summary", description="stub",
                      parameters={"type": "object", "properties": {}}, executor=slow_tool)
    p = TeamProvider({"birdy": [ProviderResponse(content=None, tool_calls=[
        ToolCall(id="a", name="get_account_summary", arguments="{}"),
        ToolCall(id="b", name="get_call_center_stats", arguments="{}")], usage=Usage(1, 1)),
        say("Partial.")]})
    out = await chat(p, registry, mock_db, agent="birdy")
    assert out["reply"] == "Partial."
    assert "get_call_center_stats" not in registry.seen       # no time left for the second tool
    skipped = [m for m in p.calls[-1][2] if m["role"] == "tool"][1]
    assert "No time is left" in json.loads(skipped["content"])["error"]
    assert p.calls[-1][1] == []


async def test_call_trend_compares_a_period_with_the_one_before(mock_db):
    from datetime import date, timedelta
    from ai.tools.hp_tools import get_call_center_stats, get_call_trend

    def day(n):
        return (date.today() - timedelta(days=n)).isoformat()

    rows = [{"date": day(n), "calls": 10, "answered": 8, "transfers": 1, "inbound": 2, "outbound": 8, "talk_time": 5}
            for n in range(1, 8)]
    rows += [{"date": day(n), "calls": 20, "answered": 10, "transfers": 4, "inbound": 4, "outbound": 16, "talk_time": 9}
             for n in range(8, 15)]
    rows.append({"date": day(0), "calls": 99, "answered": 99})  # today is left out
    await mock_db["client_groups"].insert_many([
        {"user_id": "u1", "id": "g1", "name": "Aura", "hp_daily_calls": rows},
        {"user_id": "u1", "id": "g2", "name": "No calls"},
        {"user_id": "other", "id": "g9", "name": "Theirs", "hp_daily_calls": rows},
    ])
    out = await get_call_trend(mock_db, "u1", days=7)
    assert out["current_period"]["calls"] == 70 and out["previous_period"]["calls"] == 140
    assert out["change_pct"]["calls"] == -50.0 and out["current_period"]["connect_rate"] == 80.0
    assert [c["client_group_name"] for c in out["clients"]] == ["Aura"]
    assert len(out["daily"]) == 14
    assert "error" in await get_call_trend(mock_db, "u1", days=0)
    assert "error" in await get_call_trend(mock_db, "u1", group_ids=["g2"])
    # an unknown window is an error that names the valid ones, not an empty result
    bad = await get_call_center_stats(mock_db, "u1", preset="last_week")
    assert "last_7d" in bad["error"] and "get_call_trend" in bad["error"]


def test_sales_coach_and_birdy_have_the_call_trend_tool():
    from ai.team import definitions as defs
    assert "get_call_trend" in defs.tool_names("sales_coach", nested=True)
    assert "get_call_trend" in defs.tool_names("birdy", nested=True)
    assert "get_call_trend" not in defs.tool_names("media_buyer", nested=False)
