"""
tests/test_agent_setup_tools.py
-------------------------------
The "agent_setup" chat page: the tools Birdy uses to set up a new custom agent
one step at a time (ai/tools/agent_tools.py), its prompt, its tool list, and the
ui_block a tool returns reaching the turn's agent_changes.
"""

import json

import pytest

from ai.agents import replay, store
from ai.agents.store import get_agent, list_rules
from ai.orchestrator import _PAGE_ONLY_TOOLS, _PAGE_SYSTEM_PROMPTS, _PAGE_TOOLS
from ai.prompts import agent_setup
from ai.tools import agent_tools as t
from ai.tools.registry import ToolRegistry

U = "u1@example.com"
NOT_FOUND = {"ok": False, "error": "not_found"}


@pytest.fixture(autouse=True)
def _reset_training_agent():
    token = t.current_training_agent.set(None)
    yield
    t.current_training_agent.reset(token)


async def create_agent(db, user=U, **kw):
    """Create an agent and point the chat at it, like the chat router does for agent_setup."""
    agent = await store.create_agent(db, user, **kw)
    t.current_training_agent.set(agent["_id"])
    return agent


async def group(db, gid, name, user=U, cpl=None):
    doc = {"id": gid, "user_id": user, "name": name}
    if cpl is not None:
        doc["targets"] = {"cpl": cpl}
    await db["client_groups"].insert_one(doc)


async def steps(db, agent):
    return ((await db.agents.find_one({"_id": agent["_id"]})).get("training") or {}).get("steps") or {}


async def rule(db, agent, kind):
    return next(r for r in await list_rules(db, agent["_id"]) if r["kind"] == kind)


# ---------------------------------------------------------------------------
# What it does
# ---------------------------------------------------------------------------

async def test_set_purpose_marks_step(mock_db):
    agent = await create_agent(mock_db, name="New agent")
    out = await t.set_purpose(mock_db, U, name="  Cost cutter ", description="Pauses ads that cost too much")
    assert out["ok"] is True and out["changed"] == "purpose"
    saved = await get_agent(mock_db, U, agent["_id"])
    assert saved["name"] == "Cost cutter" and saved["description"] == "Pauses ads that cost too much"
    assert await steps(mock_db, agent) == {"purpose": True}

    for bad in (dict(name="", description="x"), dict(name="x" * 61, description="x"),
                dict(name="Ok", description="x" * 281), dict(name=5, description="x")):
        assert (await t.set_purpose(mock_db, U, **bad))["ok"] is False
    assert (await get_agent(mock_db, U, agent["_id"]))["name"] == "Cost cutter"


# ---------------------------------------------------------------------------
# Its playbook
# ---------------------------------------------------------------------------

async def test_draft_playbook_rejects_unknown_kind(mock_db):
    agent = await create_agent(mock_db)
    before = await rule(mock_db, agent, "pause_over_target")
    for rules in ([{"kind": "scale_budget", "enabled": True, "params": {}}],
                  [{"kind": "pause_over_target", "enabled": True, "params": {"bogus": 1}}],
                  [{"kind": "pause_over_target", "enabled": "yes"}],
                  [{"kind": "pause_over_target"}, {"kind": "pause_over_target"}],
                  [], "nope"):
        out = await t.draft_playbook(mock_db, U, rules=rules)
        assert out["ok"] is False and out["error"], rules
    # All or nothing: a valid rule next to a bad one writes neither.
    out = await t.draft_playbook(mock_db, U, rules=[
        {"kind": "pause_over_target", "enabled": True, "params": {"cpl_multiple": 2}},
        {"kind": "nope", "enabled": True}])
    assert out["ok"] is False
    assert (await rule(mock_db, agent, "pause_over_target"))["version"] == before["version"]
    assert await steps(mock_db, agent) == {}


async def test_draft_playbook_writes_rules(mock_db):
    agent = await create_agent(mock_db)
    out = await t.draft_playbook(mock_db, U, rules=[
        {"kind": "pause_over_target", "enabled": True, "params": {"cpl_multiple": 2}},
        {"kind": "revive_winner", "enabled": False},
    ])
    assert out["ok"] is True and out["changed"] == "playbook" and out["ui_block"] == "playbook"
    pause = await rule(mock_db, agent, "pause_over_target")
    assert pause["params"]["cpl_multiple"] == 2 and pause["version"] == 2
    assert pause["params"]["patience_days"] is not None  # the rest of the params are kept
    assert (await rule(mock_db, agent, "revive_winner"))["enabled"] is False
    by_kind = {r["kind"]: r for r in out["playbook"]}
    assert by_kind["pause_over_target"]["params"]["cpl_multiple"] == 2
    assert by_kind["revive_winner"]["enabled"] is False and by_kind["flag_no_spend"]["enabled"] is True
    # A draft is not a confirmation.
    assert await steps(mock_db, agent) == {}


async def test_confirm_playbook_sets_topics(mock_db):
    agent = await create_agent(mock_db)
    await group(mock_db, "g1", "Acme", cpl=20)
    await group(mock_db, "g2", "Birch")
    await store.update_agent(mock_db, U, agent["_id"], {"scope": {"client_group_ids": ["g1", "g2"]}})
    out = await t.confirm_playbook(mock_db, U)
    assert out["ok"] is True and out["changed"] == "playbook"
    saved = await mock_db.agents.find_one({"_id": agent["_id"]})
    assert saved["training"]["steps"] == {"playbook": True}
    assert saved["training"]["topics"] == {"targets": False, "pause": True, "revive": True, "flags": True}

    # Once every watched client has a target, targets is covered too.
    await mock_db.client_groups.update_one({"id": "g2"}, {"$set": {"targets": {"cpl": 30}}})
    await t.confirm_playbook(mock_db, U)
    assert (await mock_db.agents.find_one({"_id": agent["_id"]}))["training"]["topics"]["targets"] is True


async def test_confirm_playbook_with_no_clients_leaves_targets(mock_db):
    agent = await create_agent(mock_db)
    await t.confirm_playbook(mock_db, U)
    assert (await mock_db.agents.find_one({"_id": agent["_id"]}))["training"]["topics"]["targets"] is False


# ---------------------------------------------------------------------------
# Blocks, report time, Slack
# ---------------------------------------------------------------------------

async def test_show_block_writes_nothing(mock_db):
    agent = await create_agent(mock_db)
    for block in ("clients", "permissions", "schedule", "slack"):
        assert await t.show_block(mock_db, U, block=block) == {"ok": True, "ui_block": block}
    assert (await t.show_block(mock_db, U, block="nope"))["ok"] is False
    assert await steps(mock_db, agent) == {}


@pytest.mark.parametrize("raw,expected", [
    ("7am", "07:00"), ("7:30 pm", "19:30"), ("07:00", "07:00"), ("12am", "00:00"), ("12 PM", "12:00"),
    ("9.15am", "09:15"), ("25:00", None), ("13pm", None), ("soon", None), ("", None),
])
async def test_set_schedule_parses_times(mock_db, raw, expected):
    agent = await create_agent(mock_db)
    out = await t.set_schedule(mock_db, U, time=raw)
    saved = await get_agent(mock_db, U, agent["_id"])
    if expected is None:
        assert out == {"ok": False, "error": "bad_time"}
        assert saved["scope"]["schedule"]["time"] == "07:00" and await steps(mock_db, agent) == {}
    else:
        assert out["ok"] is True and out["changed"] == "schedule"
        assert saved["scope"]["schedule"] == {"time": expected, "tz": "Europe/London"}
        assert await steps(mock_db, agent) == {"schedule": True}


async def test_set_schedule_timezone(mock_db):
    agent = await create_agent(mock_db)
    out = await t.set_schedule(mock_db, U, time="8am", tz="America/New_York")
    assert out["ok"] is True
    assert (await get_agent(mock_db, U, agent["_id"]))["scope"]["schedule"] == {
        "time": "08:00", "tz": "America/New_York"}
    assert await t.set_schedule(mock_db, U, time="8am", tz="Nope/Zone") == {"ok": False, "error": "bad_tz"}


async def test_set_schedule_falls_back_to_user_timezone(mock_db):
    agent = await create_agent(mock_db)
    await mock_db.agents.update_one({"_id": agent["_id"]}, {"$unset": {"scope.schedule": ""}})
    await mock_db.users.insert_one({"user_id": U, "timezone": "Asia/Dubai"})
    await t.set_schedule(mock_db, U, time="6am")
    assert (await get_agent(mock_db, U, agent["_id"]))["scope"]["schedule"] == {"time": "06:00", "tz": "Asia/Dubai"}


async def test_set_slack_rejects_unknown_channel(mock_db, monkeypatch):
    import services.slack_bot_service as sbs

    async def fake_find(db, user_id, ref):
        known = [{"id": "C1", "name": "ads-alerts", "is_private": False}]
        ref = str(ref).lstrip("#")
        return next((c for c in known if user_id == U and ref in (c["id"], c["name"])), None)

    monkeypatch.setattr(sbs, "find_channel", fake_find)
    agent = await create_agent(mock_db)
    assert await t.set_slack(mock_db, U, channel_id="CX") == {"ok": False, "error": "unknown_channel"}
    assert (await get_agent(mock_db, U, agent["_id"]))["scope"].get("slack_channel_id") is None
    assert await steps(mock_db, agent) == {}

    out = await t.set_slack(mock_db, U, channel_id="#ads-alerts")  # a name works too
    assert out["ok"] is True and out["changed"] == "slack" and out["channel"] == {"id": "C1", "name": "ads-alerts"}
    saved = await get_agent(mock_db, U, agent["_id"])
    assert saved["scope"]["slack_channel_id"] == "C1"
    assert saved["scope"]["schedule"]["time"] == "07:00"  # the rest of the scope is kept
    assert await steps(mock_db, agent) == {"slack": True}


# ---------------------------------------------------------------------------
# Practice and the preview
# ---------------------------------------------------------------------------

def _case(cid="c1"):
    return {"case_id": cid, "rule_kind": "pause_over_target", "client_name": "Acme", "title": "Ad A",
            "facts": ["£80 spent, no leads"], "options": ["Pause", "Leave it", "Flag"],
            "birdy_answer": "Pause", "why": "Over the zero-lead line", "snapshot": {"eval": {}}}


async def test_ask_practice_case_hides_answer(mock_db, mock_mongo_client, monkeypatch):
    served = []

    async def fake_sample(db, mongo_client, agent, *, end, days, k, seed, exclude_ad_ids):
        served.append(k)
        return [_case()] if len(served) == 1 else []

    monkeypatch.setattr(replay, "sample_cases", fake_sample)
    agent = await create_agent(mock_db)
    out = await t.ask_practice_case(mock_db, U, mongo_client=mock_mongo_client)
    assert out["ok"] is True and out["ui_block"] == "practice"
    assert out["case"] == {"case_id": "c1", "rule_kind": "pause_over_target", "client_name": "Acme",
                           "title": "Ad A", "facts": ["£80 spent, no leads"],
                           "options": ["Pause", "Leave it", "Flag"]}
    stored = await mock_db[store.PRACTICE].find_one({"agent_id": agent["_id"], "case_id": "c1"})
    assert stored["resolution"] == "unanswered"  # answered through the existing practice endpoints

    # The unanswered case is asked again until it is answered; then none are left.
    assert (await t.ask_practice_case(mock_db, U, mongo_client=mock_mongo_client))["case"]["case_id"] == "c1"
    await mock_db[store.PRACTICE].update_one({"_id": stored["_id"]}, {"$set": {"resolution": "agreed"}})
    out = await t.ask_practice_case(mock_db, U, mongo_client=mock_mongo_client)
    assert out["ok"] is False and out["error"] == "no_cases" and out["confidence"]["ready"] is False


async def test_ask_practice_case_stops_when_ready(mock_db, mock_mongo_client, monkeypatch):
    from ai.agents import confidence

    async def ready(db, agent):
        return {"score": 85, "ready": True, "missing": []}

    async def never(*a, **kw):
        raise AssertionError("no case is drawn once the agent is ready")

    monkeypatch.setattr(confidence, "compute_confidence", ready)
    monkeypatch.setattr(replay, "sample_cases", never)
    await create_agent(mock_db)
    assert await t.ask_practice_case(mock_db, U, mongo_client=mock_mongo_client) == {
        "ok": False, "error": "ready", "confidence": {"score": 85, "ready": True, "missing": []}}


async def test_run_preview_returns_job(mock_db, mock_mongo_client, monkeypatch):
    async def fake_dry_run(db, mongo_client, agent, *, end, days=30, deadline=None):
        return replay.DryRunResult()

    monkeypatch.setattr(replay, "dry_run", fake_dry_run)
    agent = await create_agent(mock_db)
    out = await t.run_preview(mock_db, U, mongo_client=mock_mongo_client)
    assert out["ok"] is True and out["ui_block"] == "preview" and out["job_id"]
    job = await mock_db[store.JOBS].find_one({"_id": out["job_id"]})
    assert job["agent_id"] == agent["_id"] and job["user_id"] == U and job["status"] == "done"


# ---------------------------------------------------------------------------
# Scoping, page wiring and agent_changes
# ---------------------------------------------------------------------------

SETUP_CALLS = [
    ("set_purpose", dict(name="A", description="b")),
    ("draft_playbook", dict(rules=[{"kind": "pause_over_target", "enabled": True}])),
    ("confirm_playbook", {}),
    ("show_block", dict(block="clients")),
    ("set_schedule", dict(time="7am")),
    ("set_slack", dict(channel_id="C1")),
    ("ask_practice_case", {}),
    ("run_preview", {}),
]


@pytest.mark.parametrize("name,kwargs", SETUP_CALLS)
async def test_setup_tools_refuse_stock_agent(mock_db, mock_mongo_client, name, kwargs):
    stock = await store.create_agent(mock_db, U, type=store.TYPE_STOCK)
    t.current_training_agent.set(stock["_id"])
    fn = getattr(t, name)
    assert await fn(mock_db, U, mongo_client=mock_mongo_client, **kwargs) == NOT_FOUND
    # Another user's custom agent, and no agent at all, are refused the same way.
    other = await store.create_agent(mock_db, "u2@example.com")
    t.current_training_agent.set(other["_id"])
    assert await fn(mock_db, U, mongo_client=mock_mongo_client, **kwargs) == NOT_FOUND
    t.current_training_agent.set(None)
    assert await fn(mock_db, U, mongo_client=mock_mongo_client, **kwargs) == NOT_FOUND
    assert (await mock_db.agents.find_one({"_id": stock["_id"]}))["name"] == store.STOCK_NAME


def test_setup_page_tools_and_prompt():
    assert _PAGE_TOOLS["agent_setup"] == [
        "set_purpose", "draft_playbook", "confirm_playbook", "show_block", "set_schedule", "set_slack",
        "ask_practice_case", "run_preview",
        "get_playbook", "set_rule_param", "set_client_target", "set_client_override", "add_memory",
        "add_exception"]
    assert set(t.SETUP_TOOL_NAMES) <= _PAGE_ONLY_TOOLS
    assert not set(t.SETUP_TOOL_NAMES) & set(_PAGE_TOOLS["agent_training"])
    assert _PAGE_SYSTEM_PROMPTS["agent_setup"] is agent_setup.system_prompt
    p = agent_setup.system_prompt()
    order = [p.index(name) for name in ("set_purpose", "draft_playbook", 'show_block("clients")',
                                         'show_block("permissions")', "set_schedule", "set_slack",
                                         "ask_practice_case", "run_preview")]
    assert order == sorted(order)
    for needle in ("one question at a time", "steps", "pause_over_target", "revive_winner", "flag_no_spend",
                   "flag_declining", "no_cases", "I want to change"):
        assert needle.lower() in p.lower(), needle


def test_setup_tools_registered():
    reg = ToolRegistry()
    t.register_agent_tools(reg)
    assert set(t.SETUP_TOOL_NAMES) <= set(reg.get_tool_names())


async def test_get_playbook_reports_steps(mock_db):
    agent = await create_agent(mock_db)
    await store.mark_step(mock_db, agent["_id"], "purpose")
    out = await t.get_playbook(mock_db, U)
    assert out["steps"] == {k: k == "purpose" for k in store.STEP_KEYS}


async def test_ui_block_reaches_agent_changes(mock_db, monkeypatch):
    from ai.orchestrator import run_chat
    from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall

    class Scripted(BaseLLMProvider):
        def __init__(self, responses):
            self._r, self.model = list(responses), "stub-model"

        async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
            return self._r.pop(0)

    def call(name, args, cid):
        return ProviderResponse(content=None, finish_reason="tool_calls",
                                tool_calls=[ToolCall(id=cid, name=name, arguments=json.dumps(args))])

    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    reg = ToolRegistry()
    t.register_agent_tools(reg)
    await create_agent(mock_db)
    provider = Scripted([
        call("draft_playbook", {"rules": [{"kind": "pause_over_target", "enabled": True,
                                           "params": {"cpl_multiple": 2}}]}, "a"),
        call("show_block", {"block": "clients"}, "b"),
        call("show_block", {"block": "nope"}, "c"),
        call("set_purpose", {"name": "Cutter", "description": "Pauses costly ads"}, "d"),
        ProviderResponse(content="Here's the playbook."),
    ])
    out = await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id=U,
                         message="pause ads that cost too much", page="agent_setup")
    changes = out["agent_changes"]
    assert [c["tool"] for c in changes] == ["draft_playbook", "show_block", "set_purpose"]
    playbook = changes[0]
    assert playbook["changed"] == "playbook" and playbook["ui_block"] == "playbook"
    assert {r["kind"]: r for r in playbook["playbook"]}["pause_over_target"]["params"]["cpl_multiple"] == 2
    assert changes[1] == {"tool": "show_block", "ui_block": "clients"}
    assert changes[2]["changed"] == "purpose" and "ui_block" not in changes[2]


@pytest.mark.parametrize("page,expected", [("agent_setup", "a1"), ("agent_training", "a1"), ("dashboard", None)])
def test_chat_router_points_setup_chat_at_its_agent(mock_mongo_client, monkeypatch, page, expected):
    import contextlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import routers.chat as chat_router

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def no_op(*a, **kw):
        return None

    seen = {}

    async def fake_run_chat(**kw):
        seen["agent"] = t.current_training_agent.get()
        return {"reply": "ok", "session_id": "s1"}

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", no_op)
    monkeypatch.setattr(chat_router, "get_provider_for_user", no_op)
    monkeypatch.setattr(chat_router, "run_chat", fake_run_chat)
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[chat_router.get_current_user] = lambda: U
    r = TestClient(app).post("/api/chat", json={"message": "hi", "page": page, "agent_id": "a1"})
    assert r.status_code == 200, r.text
    assert seen["agent"] == expected
    assert t.current_training_agent.get() is None  # reset after the request
