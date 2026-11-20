"""One-client agent runs: the tools with no group argument are run client-scoped (ai/team/pinning.py).
mongomock and stub tools only."""

import json

import pytest

from ai.agents.store import AGENTS, MEMORY, TYPE_STOCK
from ai.team import pinning
from ai.tools.stock_lesson_tools import current_lessons_agent
from services.creatives.store import TEST_ADS, TESTS
from tests.test_agent_team import TeamProvider, call, chat, isolate, registry, say  # noqa: F401 (fixtures)

U = "owner@example.com"
G1, G2, G3 = "grp-aura", "grp-beaulea", "grp-cedar"
SHARED, OWN = "act_shared", "act_cedar"


def _cache(*ads):
    return {"last_30d": {"ads": [{"id": i, "name": n, "spend": s, "total_leads": 1} for i, n, s in ads]}}


@pytest.fixture
async def data(mock_db):
    await mock_db["client_groups"].insert_many([
        {"user_id": U, "id": G1, "name": "Aura", "meta_ad_account_id": SHARED,
         "facebook_cache": _cache(("a1", "Aura spring", 50))},
        {"user_id": U, "id": G2, "name": "Beaulea", "meta_ad_account_id": SHARED,
         "facebook_cache": _cache(("b1", "Beaulea promo", 80))},
        {"user_id": U, "id": G3, "name": "Cedar", "meta_ad_account_id": OWN, "facebook_cache": _cache()},
    ])
    await mock_db["alerts"].insert_many([
        {"user_id": U, "id": "al1", "name": "Aura CPL", "target_group_ids": [G1], "target_group_names": ["Aura"],
         "status": "active", "trigger_count": 3, "condition": {}},
        {"user_id": U, "id": "al2", "name": "Everyone", "target_group_ids": [G1, G2],
         "target_group_names": ["Aura", "Beaulea"], "status": "active", "trigger_count": 9, "condition": {}},
        {"user_id": U, "id": "al3", "name": "Beaulea only", "target_group_ids": [G2],
         "target_group_names": ["Beaulea"], "status": "active", "trigger_count": 1, "condition": {}},
    ])
    await mock_db[TESTS].insert_many([
        {"user_id": U, "id": "t1", "code": "CT-0001", "name": "Aura test", "status": "live"},
        {"user_id": U, "id": "t2", "code": "CT-0002", "name": "Beaulea test", "status": "live"},
        {"user_id": U, "id": "t3", "code": "CT-0003", "name": "Cedar old test", "status": "live"},
    ])
    await mock_db[TEST_ADS].insert_many([
        {"user_id": U, "test_id": "t1", "ad_account_id": SHARED, "client_group_id": G1, "adset_name": "A",
         "meta": {"ad_id": "x1"}},
        {"user_id": U, "test_id": "t1", "ad_account_id": SHARED, "client_group_id": G2, "adset_name": "B",
         "meta": {"ad_id": "x2"}},
        {"user_id": U, "test_id": "t2", "ad_account_id": SHARED, "client_group_id": G2, "adset_name": "B",
         "meta": {"ad_id": "x3"}},
        # an old row with no client, on an account only Cedar uses
        {"user_id": U, "test_id": "t3", "ad_account_id": OWN, "adset_name": "C", "meta": {"ad_id": "x4"}},
    ])


async def run(db, name, gid=G1, **args):
    return json.loads(await pinning.run_pinned(name, args, db=db, user_id=U, client_group_id=gid))


async def test_alerts_are_this_clients_only(mock_db, data):
    out = await run(mock_db, "get_alerts")
    assert {a["id"] for a in out["alerts"]} == {"al1", "al2"}
    by = {a["id"]: a for a in out["alerts"]}
    assert by["al1"]["trigger_count"] == 3 and by["al1"]["target_group_names"] == ["Aura"]
    # an alert covering other clients too: their names and its mixed trigger history are not shown
    assert by["al2"]["target_group_names"] == ["Aura"] and by["al2"]["trigger_count"] is None
    assert out["counts"]["total"] == 2


async def test_find_ads_and_accounts_are_this_clients_only(mock_db, data):
    ads = await run(mock_db, "find_ads", q="")
    assert [a["ad_id"] for a in ads["ads"]] == ["a1"]            # not Beaulea's ad on the shared account
    refused = await run(mock_db, "find_ads", q="promo", ad_account_id=OWN)
    assert "not this client's" in refused["error"]
    accounts = await run(mock_db, "list_ad_accounts")
    assert accounts == {"ad_accounts": [{"ad_account_id": SHARED, "name": "Aura", "currency": None,
                                         "clients": [{"id": G1, "name": "Aura"}]}]}


async def test_adsets_refuse_another_account_and_filter_by_owner(mock_db, data, monkeypatch):
    from services.creatives import reads
    refused = await run(mock_db, "list_adsets", ad_account_id=OWN, destination="website")
    assert "not this client's" in refused["error"]

    async def fake_adsets(db, mongo_client, user_id, act, destination):
        assert act == SHARED
        return [{"id": "as1", "name": "Aura set", "compatible": True, "reason": None, "owning_client_group_id": G1},
                {"id": "as2", "name": "Beaulea set", "compatible": True, "reason": None,
                 "owning_client_group_id": G2}]
    monkeypatch.setattr(reads, "account_adsets", fake_adsets)
    out = await run(mock_db, "list_adsets", destination="website")      # no account given: the client's own
    assert [a["id"] for a in out["adsets"]] == ["as1"]


async def test_creative_tests_are_this_clients_only(mock_db, data):
    assert [t["id"] for t in (await run(mock_db, "list_creative_tests"))["tests"]] == ["t1"]
    assert [t["id"] for t in (await run(mock_db, "list_creative_tests", gid=G3))["tests"]] == ["t3"]
    one = await run(mock_db, "get_creative_test", code_or_id="CT-0001")
    assert [a["ad_id"] for a in one["ads"]] == ["x1"]                # Beaulea's ad in the same test is removed
    assert "not found" in (await run(mock_db, "get_creative_test", code_or_id="t2"))["error"]


async def test_lessons_are_this_clients_and_the_agencys(mock_db, data):
    await mock_db[AGENTS].insert_one({"_id": "stock1", "user_id": U, "type": TYPE_STOCK})
    for i, gid in enumerate([G1, None, G2]):
        await mock_db[MEMORY].insert_one({"_id": f"m{i}", "agent_id": "stock1", "text": f"lesson {gid}",
                                          "client_group_id": gid, "active": True, "created_at": i})
    token = current_lessons_agent.set("stock1")
    try:
        out = await run(mock_db, "list_lessons")
    finally:
        current_lessons_agent.reset(token)
    assert {l["id"] for l in out["lessons"]} == {"m0", "m1"}


async def test_unknown_client_and_no_account(mock_db, data):
    assert "not found" in (await run(mock_db, "find_ads", gid="grp-nope"))["error"]
    await mock_db["client_groups"].insert_one({"user_id": U, "id": "grp-bare", "name": "Bare"})
    assert "no Meta ad account" in (await run(mock_db, "list_adsets", gid="grp-bare", destination="website"))["error"]


async def test_the_orchestrator_runs_the_pinned_version(mock_db, data, registry):
    """In a one-client Media Buyer run, list_ad_accounts never reaches the general tool."""
    registry.register(name="list_ad_accounts", description="stub", parameters={"type": "object", "properties": {}},
                      executor=lambda **kw: pytest.fail("the unpinned tool ran"))
    p = TeamProvider({"media_buyer": [call("list_ad_accounts"), say("One account.")]})
    await chat(p, registry, mock_db, agent="media_buyer", client_group_id=G1, client_name="Aura")
    tool_msg = next(m for m in p.calls[-1][2] if m["role"] == "tool")
    assert [a["ad_account_id"] for a in json.loads(tool_msg["content"])["ad_accounts"]] == [SHARED]


async def test_agency_runs_are_not_pinned(mock_db, data, registry):
    seen = []

    async def accounts(db, user_id, mongo_client=None, **kw):
        seen.append(1)
        return {"ad_accounts": []}
    registry.register(name="list_ad_accounts", description="stub", parameters={"type": "object", "properties": {}},
                      executor=accounts)
    p = TeamProvider({"media_buyer": [call("list_ad_accounts"), say("ok")]})
    await chat(p, registry, mock_db, agent="media_buyer")
    assert seen == [1]


# ── Birdy's own agency-level write tools ──────────────────────────────────────

def tool_call(name, args):
    from ai.providers.base import ProviderResponse, ToolCall, Usage
    return ProviderResponse(content=None, tool_calls=[ToolCall(id=f"tc-{name}", name=name, arguments=json.dumps(args))],
                            finish_reason="tool_calls", usage=Usage(1, 1))


def _stub(registry, name, result=None):
    seen = registry.seen.setdefault(name, [])

    async def run(db, user_id, mongo_client=None, **kw):
        seen.append(kw)
        return result or {"ok": True}
    registry.register(name=name, description="stub", parameters={"type": "object", "properties": {}}, executor=run)


@pytest.fixture
def mcp(monkeypatch, registry):
    """A stand-in MCP server: it hosts the MCP tool names and Birdy's MCP-only tools, and runs each call
    on the registry stub of that name (recorded in registry.seen) or answers {"ok": true}."""
    import contextlib

    import ai.orchestrator as orch
    from ai.mcp_client import MCP_TOOL_NAMES
    from ai.team import definitions as defs
    hosted = set(MCP_TOOL_NAMES) | set(defs.MCP_HOSTED)
    calls = []

    @contextlib.asynccontextmanager
    async def fake_client(user_id):
        yield object()

    async def schemas(client, allowed):
        return [{"type": "function", "function": {"name": n, "description": "", "parameters": {"type": "object"}}}
                for n in sorted(hosted) if allowed is None or n in allowed]

    async def call_tool(client, name, args):
        calls.append((name, dict(args)))
        registry.seen.setdefault(name, []).append(dict(args))
        return json.dumps({"ok": True})
    monkeypatch.setattr(orch.mcp_client, "needs_mcp", lambda allowed: True)
    monkeypatch.setattr(orch.mcp_client, "mcp_client_for", fake_client)
    monkeypatch.setattr(orch.mcp_client, "get_mcp_schemas", schemas)
    monkeypatch.setattr(orch.mcp_client, "call_mcp_tool", call_tool)
    return calls


async def test_birdy_keeps_agency_writes_in_its_own_chat_only(mock_db, registry, data, mcp):
    from ai.team import definitions as defs
    p = TeamProvider({"media_buyer": [call("ask_birdy", question="q", client="agency"), say("ok")],
                      "birdy": [say("Birdy here.")]})
    await chat(p, registry, mock_db, agent="birdy")
    own = set(p.offered("birdy"))
    assert set(defs.BIRDY_WRITE) <= own
    assert not own & {"draft_creative_test", "post_creative_test", "apply_card_lines", "analyze_call_recordings",
                      "create_saved_view", "propose_report"}
    p.calls.clear()
    await chat(p, registry, mock_db, agent="media_buyer")
    asked = set(p.offered("birdy"))          # Birdy asked by Media Buyer: read only
    assert not asked & set(defs.BIRDY_WRITE) and "get_client" in asked


async def test_one_client_birdy_has_no_agency_wide_config_and_pins_its_writes(mock_db, registry, data, mcp):
    from ai.team import definitions as defs
    p = TeamProvider({"birdy": [call("add_client_note", client="Beaulea", text="Called them"),
                                call("set_client_target", client=G2, cpl=40),
                                tool_call("create_alert", {"name": "CPL", "metric": "cpl", "operator": "gt",
                                                           "value": 50, "target_group_ids": [G1, G2]}),
                                call("update_alert", alert_id="al2", status="paused"),
                                call("update_alert", alert_id="al1", status="paused", target_group_ids=[G2]),
                                say("Done.")]})
    await chat(p, registry, mock_db, agent="birdy", client_group_id=G1, client_name="Aura")
    offered = set(p.offered("birdy"))
    assert not offered & defs.AGENCY_ONLY_TOOLS and "add_client_note" in offered
    assert registry.seen["add_client_note"][-1]["client"] == G1
    assert registry.seen["set_client_target"][-1]["client"] == G1
    assert registry.seen["create_alert"][-1]["target_group_ids"] == [G1]
    # al2 covers Beaulea too: refused; al1 is Aura's alone: changed, and kept on Aura
    assert [c["alert_id"] for c in registry.seen["update_alert"]] == ["al1"]
    assert registry.seen["update_alert"][-1]["target_group_ids"] == [G1]


async def test_birdy_calls_mcp_only_tools_through_mcp(mock_db, registry, mcp):
    """add_client_note and set_client_target live on the MCP server only; an agent run calls them through
    MCP, and the registry's set_client_target (a custom-agent training tool) is never offered instead."""
    async def training_tool(**kw):
        raise AssertionError("the training tool ran")
    registry.register(name="set_client_target", description="training", parameters={"type": "object"},
                      executor=training_tool)
    p = TeamProvider({"birdy": [call("add_client_note", client="Aura", text="x"),
                                call("set_client_target", client="Aura", cpl=30), say("Saved.")]})
    await chat(p, registry, mock_db, agent="birdy")
    assert mcp == [("add_client_note", {"client": "Aura", "text": "x"}),
                   ("set_client_target", {"client": "Aura", "cpl": 30})]
    assert p.offered("birdy").count("set_client_target") == 1
