import pytest

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.store import RULE_VERSIONS, create_agent, get_agent, list_memory, list_rules
from ai.orchestrator import _PAGE_SYSTEM_PROMPTS, _PAGE_TOOLS
from ai.prompts import agent_training
from ai.tools import agent_tools as t
from ai.tools.registry import ToolRegistry

U = "u1@example.com"
NO_AGENT = {"ok": False, "error": "Set up the Media Buying Agent first"}


async def group(db, gid, name, user=U, cpl=None, currency=None):
    doc = {"id": gid, "user_id": user, "name": name}
    if cpl is not None:
        doc["targets"] = {"cpl": cpl}
    if currency:
        doc["ad_account_currency"] = currency
    await db["client_groups"].insert_one(doc)


async def rule(db, agent, kind):
    return next(r for r in await list_rules(db, agent["_id"]) if r["kind"] == kind)


async def test_set_rule_param_validates_and_versions(mock_db):
    agent = await create_agent(mock_db, U)
    out = await t.set_rule_param(mock_db, U, rule_kind="pause_over_target", param="cpl_multiple", value=2)
    assert out == {"ok": True, "changed": {"rule_kind": "pause_over_target", "param": "cpl_multiple",
                                           "before": 1.5, "after": 2}}
    r = await rule(mock_db, agent, "pause_over_target")
    assert r["params"]["cpl_multiple"] == 2 and r["version"] == 2
    assert r["params"]["patience_days"] == DEFAULT_PARAMS["pause_over_target"]["patience_days"]
    assert await mock_db[RULE_VERSIONS].count_documents({"rule_id": r["_id"]}) == 1

    for bad in (dict(rule_kind="nope", param="x", value=1),
                dict(rule_kind="pause_over_target", param="bogus", value=1),
                dict(rule_kind="pause_over_target", param="cpl_multiple", value=-1),
                dict(rule_kind="pause_over_target", param="cpl_multiple", value="2"),
                dict(rule_kind="pause_over_target", param="cpl_multiple", value=True)):
        out = await t.set_rule_param(mock_db, U, **bad)
        assert out["ok"] is False and out["error"]
    assert (await rule(mock_db, agent, "pause_over_target"))["version"] == 2


async def test_set_client_target_ambiguous_name_lists_candidates(mock_db):
    await create_agent(mock_db, U)
    await group(mock_db, "g1", "Acme Dental")
    await group(mock_db, "g2", "Acme Roofing")
    await group(mock_db, "g3", "Birch Law")
    out = await t.set_client_target(mock_db, U, client_name="acme", cpl=25)
    assert out["ok"] is False and sorted(out["candidates"]) == ["Acme Dental", "Acme Roofing"]
    out = await t.set_client_target(mock_db, U, client_name="zzz", cpl=25)
    assert out["ok"] is False and len(out["candidates"]) == 3
    assert "targets" not in await mock_db["client_groups"].find_one({"id": "g1"})

    out = await t.set_client_target(mock_db, U, client_name="BIRCH law", cpl=30)
    assert out["ok"] is True
    assert out["changed"] == {"client": "Birch Law", "param": "cpl", "before": None, "after": 30}
    assert (await mock_db["client_groups"].find_one({"id": "g3"}))["targets"]["cpl"] == 30
    out = await t.set_client_target(mock_db, U, client_name="birch law", cpl=-5)
    assert out["ok"] is False


async def test_set_client_target_exact_beats_partial_and_ignores_other_users(mock_db):
    await create_agent(mock_db, U)
    await group(mock_db, "g1", "Acme")
    await group(mock_db, "g2", "Acme Roofing")
    await group(mock_db, "g9", "Acme Other", user="u2@example.com")
    out = await t.set_client_target(mock_db, U, client_name="acme", cpl=20)
    assert out["ok"] is True and out["changed"]["client"] == "Acme"


async def test_set_client_override(mock_db):
    agent = await create_agent(mock_db, U)
    await group(mock_db, "g1", "Acme")
    out = await t.set_client_override(mock_db, U, client_name="acme", rule_kind="pause_over_target",
                                      param="patience_days", value=5)
    assert out["ok"] is True
    assert out["changed"] == {"rule_kind": "pause_over_target", "param": "patience_days",
                              "client": "Acme", "before": 3, "after": 5}
    r = await rule(mock_db, agent, "pause_over_target")
    assert r["client_overrides"] == {"g1": {"patience_days": 5}} and r["version"] == 2
    out = await t.set_client_override(mock_db, U, client_name="acme", rule_kind="pause_over_target",
                                      param="patience_spend", value=90)
    assert (await rule(mock_db, agent, "pause_over_target"))["client_overrides"]["g1"] == {
        "patience_days": 5, "patience_spend": 90}
    out = await t.set_client_override(mock_db, U, client_name="acme", rule_kind="pause_over_target",
                                      param="nope", value=1)
    assert out["ok"] is False
    out = await t.set_client_override(mock_db, U, client_name="ghost", rule_kind="pause_over_target",
                                      param="patience_days", value=1)
    assert out["ok"] is False and out["candidates"] == ["Acme"]


async def test_add_memory(mock_db):
    agent = await create_agent(mock_db, U)
    out = await t.add_memory(mock_db, U, text="  Never pause the brand ads  ")
    assert out == {"ok": True, "changed": {"text": "Never pause the brand ads"}}
    (m,) = await list_memory(mock_db, agent["_id"])
    assert (m["kind"], m["source"], m["filter"]) == ("fact", "chat", None)
    assert (await t.add_memory(mock_db, U, text="  "))["ok"] is False


async def test_add_exception_creates_filter(mock_db):
    agent = await create_agent(mock_db, U)
    await group(mock_db, "g1", "Acme")
    out = await t.add_exception(mock_db, U, text="Acme runs weekend promos", client_name="acme",
                                ad_name_contains="Promo", weekdays=[5, 6])
    assert out["ok"] is True and out["changed"]["text"] == "Acme runs weekend promos"
    (m,) = await list_memory(mock_db, agent["_id"])
    assert (m["kind"], m["source"]) == ("exception", "chat")
    assert m["filter"] == {"client_group_id": "g1", "ad_name_contains": "Promo", "weekdays": [5, 6]}

    # no filter -> use add_memory
    out = await t.add_exception(mock_db, U, text="x")
    assert out["ok"] is False and "add_memory" in out["error"]
    for bad in ([7], [-1], ["1"], [True], "5", []):
        out = await t.add_exception(mock_db, U, text="x", weekdays=bad)
        assert out["ok"] is False, bad
    out = await t.add_exception(mock_db, U, text="x", client_name="nobody")
    assert out["ok"] is False and out["candidates"] == ["Acme"]
    assert len(await list_memory(mock_db, agent["_id"])) == 1


async def test_answer_topic_sets_flag(mock_db):
    agent = await create_agent(mock_db, U)
    out = await t.answer_topic(mock_db, U, topic="pause")
    assert out == {"ok": True, "changed": {"topic": "pause", "answered": True}}
    topics = (await get_agent(mock_db, U, agent["_id"]))["training"]["topics"]
    assert topics == {"targets": False, "pause": True, "revive": False, "flags": False}
    assert (await t.answer_topic(mock_db, U, topic="budget"))["ok"] is False


async def test_get_playbook(mock_db):
    agent = await create_agent(mock_db, U)
    await group(mock_db, "g1", "Acme", cpl=22, currency="GBP")
    await group(mock_db, "g2", "Birch")
    out = await t.get_playbook(mock_db, U)
    assert out["ok"] is True
    assert {r["kind"] for r in out["rules"]} == set(DEFAULT_PARAMS)
    pr = next(r for r in out["rules"] if r["kind"] == "pause_over_target")
    assert pr["params"] == DEFAULT_PARAMS["pause_over_target"] and pr["mode"] == "ask"
    assert pr["client_overrides"] == {}
    assert out["targets"] == {"Acme": 22, "Birch": None}
    assert out["currencies"]["Acme"] == "£" and out["currencies"]["Birch"] == "$"
    assert out["topics"] == {"targets": False, "pause": False, "revive": False, "flags": False}

    # watched scope narrows the clients
    from ai.agents.store import update_agent
    await update_agent(mock_db, U, agent["_id"], {"scope": {"client_group_ids": ["g2"], "schedule": {}}})
    assert (await t.get_playbook(mock_db, U))["targets"] == {"Birch": None}


@pytest.mark.parametrize("name,kwargs", [
    ("get_playbook", {}),
    ("set_rule_param", dict(rule_kind="pause_over_target", param="cpl_multiple", value=2)),
    ("set_client_target", dict(client_name="a", cpl=1)),
    ("set_client_override", dict(client_name="a", rule_kind="pause_over_target", param="patience_days", value=1)),
    ("add_memory", dict(text="x")),
    ("add_exception", dict(text="x", client_name="a")),
    ("answer_topic", dict(topic="pause")),
])
async def test_no_agent(mock_db, name, kwargs):
    assert await getattr(t, name)(mock_db, U, **kwargs) == NO_AGENT


def test_page_tools_scoped():
    assert _PAGE_TOOLS["agent_training"] == [
        "get_playbook", "set_rule_param", "set_client_target", "set_client_override",
        "add_memory", "add_exception", "answer_topic", "get_client_groups"]


def test_tools_registered_with_page_names():
    reg = ToolRegistry()
    t.register_agent_tools(reg)
    assert set(reg.get_tool_names()) == set(_PAGE_TOOLS["agent_training"]) - {"get_client_groups"}


def test_prompt_content():
    p = agent_training.system_prompt()
    assert "£" not in p
    for needle in ("one question at a time", "answer_topic", "get_playbook", "Practice case", "user_right",
                   "currency"):
        assert needle.lower() in p.lower(), needle
    order = [p.lower().index(k) for k in ("missing targets", "new-ad patience", "pause line", "revive policy", "what to flag")]
    assert order == sorted(order)
    assert _PAGE_SYSTEM_PROMPTS["agent_training"] is agent_training.system_prompt


def test_agent_tools_hidden_from_general_chat():
    from ai.orchestrator import _PAGE_ONLY_TOOLS
    assert set(t.AGENT_TOOL_NAMES) <= _PAGE_ONLY_TOOLS


async def test_run_chat_returns_agent_changes(mock_db, monkeypatch):
    import json
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
    await create_agent(mock_db, U)
    provider = Scripted([
        call("set_rule_param", {"rule_kind": "pause_over_target", "param": "cpl_multiple", "value": 2}, "a"),
        call("set_rule_param", {"rule_kind": "pause_over_target", "param": "bogus", "value": 1}, "b"),
        call("answer_topic", {"topic": "pause"}, "c"),
        ProviderResponse(content="Done."),
    ])
    out = await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id=U,
                         message="pause at 2x", page="agent_training")
    assert out["agent_changes"] == [
        {"tool": "set_rule_param", "changed": {"rule_kind": "pause_over_target", "param": "cpl_multiple",
                                               "before": 1.5, "after": 2}},
        {"tool": "answer_topic", "changed": {"topic": "pause", "answered": True}},
    ]
