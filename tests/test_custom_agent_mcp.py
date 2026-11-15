"""Custom agent MCP tools (ai/mcp/custom_agent_mcp.py)."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ai.agents import replay
from ai.agents import store as agent_store
from ai.agents.stock import lessons as stock_lessons
from ai.mcp import custom_agent_mcp as t
from ai.suggestions import store as sug_store

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio

READ_TOOLS = {"get_agent_playbook", "get_agent_activity", "get_agent_history", "get_agent_claims", "get_agent_impact",
              "get_agent_confidence", "get_agent_proposals", "get_agent_memory", "get_agent_offers", "get_dry_run"}
WRITE_TOOLS = {"accept_proposal", "reject_proposal", "set_rule_param", "set_client_target_for_agent",
               "set_client_override", "add_agent_memory", "add_agent_exception", "set_agent_schedule",
               "set_agent_slack_channel", "delete_agent_memory", "start_dry_run", "deactivate_agent"}
LIVE_TOOLS = {"activate_agent", "undo_auto_action", "graduate_rule"}


async def _group(db, gid="g1", name="Acme", user=ALICE):
    await db["client_groups"].insert_one({"id": gid, "user_id": user, "name": name})


async def _custom(db, user=ALICE, name="Side", gids=("g1",), **patch):
    agent = await agent_store.create_agent(db, user, type=agent_store.TYPE_CUSTOM, name=name)
    scope = {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "Europe/London"}}
    return await agent_store.update_agent(db, user, agent["_id"], {"scope": scope, **patch})


async def _rule(db, agent, kind="revive_winner"):
    return next(r for r in await agent_store.list_rules(db, agent["_id"]) if r["kind"] == kind)


async def _proposal(db, agent, rule, pid="p1", **kw):
    doc = {"_id": pid, "agent_id": agent["_id"], "status": "open", "kind": "param_change", "rule_id": rule["_id"],
           "rule_kind": rule["kind"], "rule_version": rule.get("version"), "before": {"max_creative_age_days": 90},
           "after": {"max_creative_age_days": 60}, "evidence": {"count": 3}, "created_at": datetime(2026, 11, 1), **kw}
    await db[agent_store.PROPOSALS].insert_one(doc)
    return doc


@pytest.fixture
async def alice(mock_db, set_current_user):
    set_current_user(ALICE)
    await _group(mock_db)
    return await _custom(mock_db)


# ---------------------------------------------------------------------------
# Registration / tags
# ---------------------------------------------------------------------------

async def test_tool_tags_match_the_spec():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name in READ_TOOLS:
        assert tools[name].tags == {"read"}, name
    for name in WRITE_TOOLS:
        assert tools[name].tags == {"write"}, name
    for name in LIVE_TOOLS:
        assert tools[name].tags == {"write", "live"}, name
        assert "only call again with confirm_code after they say yes." in tools[name].description


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

async def test_accept_proposal_updates_rule(mock_db, alice):
    rule = await _rule(mock_db, alice)
    await _proposal(mock_db, alice, rule)
    out = await t.accept_proposal.fn(proposal_id="p1", agent=alice["_id"])
    assert out["proposal"]["status"] == "accepted" and "lesson" not in out
    assert out["rule"]["params"]["max_creative_age_days"] == 60
    stored = await _rule(mock_db, alice)
    assert stored["params"]["max_creative_age_days"] == 60 and stored["version"] == rule["version"] + 1


async def test_accept_promote_proposal_becomes_lesson(mock_db, alice):
    await mock_db[agent_store.PROPOSALS].insert_one({
        "_id": "pp", "agent_id": alice["_id"], "status": "open", "kind": stock_lessons.PROMOTE,
        "text": "Never pause brand ads", "created_at": datetime(2026, 11, 1)})
    out = await t.accept_proposal.fn(proposal_id="pp", agent=alice["_id"])
    assert out["rule"] is None and out["lesson"]["text"] == "Never pause brand ads"
    assert out["lesson"]["source"] == "promoted"
    assert (await mock_db[agent_store.PROPOSALS].find_one({"_id": "pp"}))["decided_by"] == ALICE


async def test_accept_closed_proposal_returns_error_not_raise(mock_db, alice):
    rule = await _rule(mock_db, alice)
    await _proposal(mock_db, alice, rule, status="accepted")
    out = await t.accept_proposal.fn(proposal_id="p1", agent=alice["_id"])
    assert out == {"ok": False, "error": "This proposal was already decided", "status": 409}
    out = await t.reject_proposal.fn(proposal_id="p1", agent=alice["_id"])
    assert out["ok"] is False and out["error"] == "This proposal was already decided"
    assert (await _rule(mock_db, alice))["params"]["max_creative_age_days"] == 90


async def test_accept_out_of_date_proposal_returns_error(mock_db, alice):
    rule = await _rule(mock_db, alice)
    await _proposal(mock_db, alice, rule, rule_version=99)
    out = await t.accept_proposal.fn(proposal_id="p1", agent=alice["_id"])
    assert out["ok"] is False and "out of date" in out["error"]
    assert (await mock_db[agent_store.PROPOSALS].find_one({"_id": "p1"}))["status"] == "open"


async def test_unknown_proposal_is_a_clear_error(alice):
    with pytest.raises(ToolError, match="Proposal not found"):
        await t.accept_proposal.fn(proposal_id="nope", agent=alice["_id"])
    with pytest.raises(ToolError, match="Proposal not found"):
        await t.reject_proposal.fn(proposal_id="nope", agent=alice["_id"])


async def test_reject_proposal_and_list(mock_db, alice):
    rule = await _rule(mock_db, alice)
    await _proposal(mock_db, alice, rule)
    assert [p["id"] for p in await t.get_agent_proposals.fn(agent=alice["_id"])] == ["p1"]
    out = await t.reject_proposal.fn(proposal_id="p1", agent=alice["_id"])
    assert out["proposal"]["status"] == "rejected"
    assert await t.get_agent_proposals.fn(agent=alice["_id"]) == []
    assert [p["id"] for p in await t.get_agent_proposals.fn(status="rejected", agent=alice["_id"])] == ["p1"]


# ---------------------------------------------------------------------------
# Activate / deactivate (LIVE)
# ---------------------------------------------------------------------------

def _ready(monkeypatch, ready=True):
    conf = {"score": 80, "ready": ready, "missing": [] if ready else ["Answer 3 practice cases"], "parts": {}}
    monkeypatch.setattr("ai.agents.custom_ops.agent_confidence.compute_confidence", AsyncMock(return_value=conf))


async def test_activate_needs_confirmation(mock_db, alice, monkeypatch):
    _ready(monkeypatch)
    first = await t.activate_agent.fn(agent=alice["_id"])
    assert first["needs_confirmation"] is True and first["confirm_code"]
    pre = first["preview"]
    assert pre["schedule"] == {"time": "07:00", "tz": "Europe/London"}
    assert [c["name"] for c in pre["clients"]] == ["Acme"]
    assert pre["may_change"]["permissions"]["ads"] is True and pre["may_change"]["rules"]
    assert (await agent_store.get_agent(mock_db, ALICE, alice["_id"]))["status"] != "active"
    out = await t.activate_agent.fn(agent=alice["_id"], confirm_code=first["confirm_code"])
    assert out["agent"]["status"] == "active" and "now on" in out["summary"].lower()
    stored = await agent_store.get_agent(mock_db, ALICE, alice["_id"])
    assert stored["status"] == "active" and stored["confidence"]["at_activation"] == 80
    # The code is single use.
    again = await t.activate_agent.fn(agent=alice["_id"], confirm_code=first["confirm_code"])
    assert "error" in again and "Nothing was changed" in again["error"]


async def test_activate_not_ready_issues_no_code(mock_db, alice, monkeypatch):
    _ready(monkeypatch, ready=False)
    out = await t.activate_agent.fn(agent=alice["_id"])
    assert out["ok"] is False and out["status"] == 409 and out["missing"] == ["Answer 3 practice cases"]
    assert "confirm_code" not in out and await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    assert (await agent_store.get_agent(mock_db, ALICE, alice["_id"]))["status"] != "active"


async def test_activate_already_active_issues_no_code(mock_db, alice):
    await agent_store.update_agent(mock_db, ALICE, alice["_id"], {"status": "active"})
    out = await t.activate_agent.fn(agent=alice["_id"])
    assert out["ok"] is True and "already on" in out["summary"] and "confirm_code" not in out


async def test_activate_clash_with_other_agent_issues_no_code_until_move_claims(mock_db, alice, monkeypatch):
    from ai.agents import claims
    _ready(monkeypatch)
    other = await _custom(mock_db, name="Other")
    await claims.claim_clients(mock_db, ALICE, other["_id"], ["g1"])
    out = await t.activate_agent.fn(agent=alice["_id"])
    assert out["ok"] is False and "Other" in out["error"] and out["conflicts"] and "confirm_code" not in out
    first = await t.activate_agent.fn(move_claims=True, agent=alice["_id"])
    assert first["preview"]["takes_over_clients"] == ["Acme"] and first["preview"]["ready"] is True
    done = await t.activate_agent.fn(move_claims=True, agent=alice["_id"], confirm_code=first["confirm_code"])
    assert done["agent"]["status"] == "active" and await claims.holder(mock_db, ALICE, "g1") == alice["_id"]


async def test_activate_code_replayed_with_other_arguments_is_refused(mock_db, alice, monkeypatch):
    from ai.agents import claims
    _ready(monkeypatch)
    other = await _custom(mock_db, name="Other")
    await claims.claim_clients(mock_db, ALICE, other["_id"], ["g1"])
    first = await t.activate_agent.fn(move_claims=True, agent=alice["_id"])
    out = await t.activate_agent.fn(move_claims=False, agent=alice["_id"], confirm_code=first["confirm_code"])
    assert "error" in out and "Nothing was changed" in out["error"]
    assert (await agent_store.get_agent(mock_db, ALICE, alice["_id"]))["status"] != "active"
    assert await claims.holder(mock_db, ALICE, "g1") == other["_id"]


async def test_deactivate_stock_agent_works_and_is_idempotent(mock_db, set_current_user):
    set_current_user(ALICE)
    stock = await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_STOCK)
    await agent_store.update_agent(mock_db, ALICE, stock["_id"], {"status": "active"})
    out = await t.deactivate_agent.fn()
    assert out["ok"] is True and out["agent"]["status"] == "draft" and out["agent"]["turned_off_at"]
    again = await t.deactivate_agent.fn(agent=stock["_id"])
    assert again["ok"] is True and again["agent"]["status"] == "draft" and "already off" in again["summary"]


async def test_deactivate_custom_agent_is_refused(mock_db, alice):
    await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_STOCK)
    await agent_store.update_agent(mock_db, ALICE, alice["_id"], {"status": "active"})
    out = await t.deactivate_agent.fn(agent=alice["_id"])
    assert out == {"ok": False, "error": "Only the Birdy Media Buying Agent can be turned off", "status": 409}
    assert (await agent_store.get_agent(mock_db, ALICE, alice["_id"]))["status"] == "active"


async def test_deactivate_other_users_stock_agent_unreachable(mock_db, set_current_user):
    set_current_user(ALICE)
    stock = await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_STOCK)
    await agent_store.update_agent(mock_db, ALICE, stock["_id"], {"status": "active"})
    set_current_user(BOB)
    with pytest.raises(ToolError):
        await t.deactivate_agent.fn(agent=stock["_id"])
    with pytest.raises(ToolError, match="don't have a media buying agent"):
        await t.deactivate_agent.fn()
    assert (await agent_store.get_agent(mock_db, ALICE, stock["_id"]))["status"] == "active"


async def _auto_suggestion(db, agent, sid="s1", applied_at=None, **kw):
    await db[sug_store.SUGGESTIONS].insert_one({
        "_id": sid, "id": sid, "user_id": agent["user_id"], "agent_id": agent["_id"], "status": sug_store.STATUS_APPLIED,
        "decided_by": {"actor": sug_store.ACTOR_BIRDY}, "applied_at": applied_at or datetime.utcnow(),
        "title": "Paused ad X", "client_name": "Acme", **kw})


async def test_undo_auto_action_two_step_source_mcp(mock_db, alice, monkeypatch):
    await _auto_suggestion(mock_db, alice)
    undo = AsyncMock(return_value={"outcome": "undone", "succeeded": ["a1"]})
    monkeypatch.setattr("ai.agents.custom_ops.sug_actions.undo_suggestion", undo)
    first = await t.undo_auto_action.fn(suggestion_id="s1", agent=alice["_id"])
    undo.assert_not_awaited()
    assert first["needs_confirmation"] and first["preview"]["title"] == "Paused ad X"
    out = await t.undo_auto_action.fn(suggestion_id="s1", agent=alice["_id"], confirm_code=first["confirm_code"])
    assert undo.await_args.kwargs["source"] == "mcp" and out["ok"] is True and out["succeeded"] == ["a1"]


async def test_undo_code_replayed_with_other_suggestion_is_refused(mock_db, alice, monkeypatch):
    await _auto_suggestion(mock_db, alice, "s1")
    await _auto_suggestion(mock_db, alice, "s2")
    undo = AsyncMock(return_value={"outcome": "undone"})
    monkeypatch.setattr("ai.agents.custom_ops.sug_actions.undo_suggestion", undo)
    first = await t.undo_auto_action.fn(suggestion_id="s1", agent=alice["_id"])
    out = await t.undo_auto_action.fn(suggestion_id="s2", agent=alice["_id"], confirm_code=first["confirm_code"])
    assert "error" in out and "Nothing was changed" in out["error"]
    undo.assert_not_awaited()


async def test_undo_auto_action_refusals_are_error_dicts(mock_db, alice, monkeypatch):
    undo = AsyncMock()
    monkeypatch.setattr("ai.agents.custom_ops.sug_actions.undo_suggestion", undo)
    await _auto_suggestion(mock_db, alice, "old", applied_at=datetime.utcnow() - timedelta(days=3))
    out = await t.undo_auto_action.fn(suggestion_id="old", agent=alice["_id"])
    assert out["ok"] is False and out["status"] == 410 and "24 hours" in out["error"] and "confirm_code" not in out
    await _auto_suggestion(mock_db, alice, "human", decided_by={"actor": sug_store.ACTOR_USER})
    out = await t.undo_auto_action.fn(suggestion_id="human", agent=alice["_id"])
    assert out["status"] == 409 and "ran on its own" in out["error"]
    undo.assert_not_awaited()
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.undo_auto_action.fn(suggestion_id="zzz", agent=alice["_id"])


# ---------------------------------------------------------------------------
# Stock agent hint, other agents
# ---------------------------------------------------------------------------

async def test_custom_tools_reject_stock_agent_with_hint(mock_db, set_current_user):
    set_current_user(ALICE)
    stock = await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_STOCK)
    calls = [t.get_agent_playbook.fn(agent=stock["_id"]), t.get_agent_proposals.fn(agent=stock["_id"]),
             t.accept_proposal.fn(proposal_id="p", agent=stock["_id"]),
             t.set_rule_param.fn(rule_kind="revive_winner", param="min_leads", value=3, agent=stock["_id"]),
             t.activate_agent.fn(agent=stock["_id"]),
             t.start_dry_run.fn(agent=stock["_id"])]
    for call in calls:
        with pytest.raises(ToolError, match="media buying agent tools"):
            await call
    with pytest.raises(ToolError, match="media buying agent tools"):  # the default agent is the stock one
        await t.get_agent_memory.fn()
    assert await mock_db[agent_store.AGENTS].count_documents({"status": "active"}) == 0


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

async def test_dry_run_job_poll(mock_db, alice, monkeypatch):
    async def fake_run(db, mongo_client, job_id, agent, *, end, deadline):
        await db[agent_store.JOBS].update_one({"_id": job_id}, {"$set": {"status": "done", "result": {"days": 30}}})
    monkeypatch.setattr(replay, "_run_dry_run_job", fake_run)
    out = await t.start_dry_run.fn(agent=alice["_id"])
    assert set(out) == {"job_id"}
    polled = await t.get_dry_run.fn(job_id=out["job_id"], agent=alice["_id"])
    assert polled == {"status": "done", "result": {"days": 30}}
    with pytest.raises(ToolError, match="Job not found"):
        await t.get_dry_run.fn(job_id="nope", agent=alice["_id"])


async def test_dry_run_failed_and_timed_out_jobs(mock_db, alice):
    old = datetime.utcnow() - replay.JOB_TIMEOUT - timedelta(minutes=1)
    await mock_db[agent_store.JOBS].insert_many([
        {"_id": "j1", "agent_id": alice["_id"], "user_id": ALICE, "status": "failed", "error": "boom", "created_at": datetime.utcnow()},
        {"_id": "j2", "agent_id": alice["_id"], "user_id": ALICE, "status": "running", "created_at": old}])
    assert await t.get_dry_run.fn(job_id="j1", agent=alice["_id"]) == {"status": "failed", "error": "boom"}
    assert await t.get_dry_run.fn(job_id="j2", agent=alice["_id"]) == {"status": "failed", "error": "timed_out"}


# ---------------------------------------------------------------------------
# Write tools reusing agent_tools
# ---------------------------------------------------------------------------

async def test_set_rule_param_target_override(mock_db, alice):
    out = await t.set_rule_param.fn(rule_kind="pause_over_target", param="cpl_multiple", value=2, agent=alice["_id"])
    assert out["ok"] is True and out["changed"]["after"] == 2
    assert (await _rule(mock_db, alice, "pause_over_target"))["params"]["cpl_multiple"] == 2
    with pytest.raises(ToolError):
        await t.set_rule_param.fn(rule_kind="pause_over_target", param="cpl_multiple", value=-1, agent=alice["_id"])
    out = await t.set_client_target_for_agent.fn(client_name="acme", cpl=12.5, agent=alice["_id"])
    assert out["changed"]["after"] == 12.5
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["targets"]["cpl"] == 12.5
    out = await t.set_client_override.fn(client_name="Acme", rule_kind="pause_over_target", param="cpl_multiple",
                                         value=3, agent=alice["_id"])
    assert out["changed"]["client"] == "Acme"
    assert (await _rule(mock_db, alice, "pause_over_target"))["client_overrides"]["g1"]["cpl_multiple"] == 3


async def test_ambiguous_client_returns_candidates_and_changes_nothing(mock_db, alice):
    await _group(mock_db, "g2", "Acme Law")
    with pytest.raises(ToolError, match="Acme, Acme Law|Acme Law"):
        await t.set_client_target_for_agent.fn(client_name="acm", cpl=9, agent=alice["_id"])
    assert "targets" not in (await mock_db["client_groups"].find_one({"id": "g1"}))


async def test_memory_tools(mock_db, alice):
    out = await t.add_agent_memory.fn(text="Clients dislike weekend pauses", agent=alice["_id"])
    assert out["ok"] is True
    out = await t.add_agent_exception.fn(text="Leave brand ads", client_name="Acme", ad_name_contains="brand",
                                         weekdays=[5, 6], agent=alice["_id"])
    assert out["changed"]["filter"]["client_group_id"] == "g1"
    rows = await t.get_agent_memory.fn(agent=alice["_id"])
    assert {r["text"] for r in rows} == {"Clients dislike weekend pauses", "Leave brand ads"}
    assert {r["source"] for r in rows} == {"mcp"}
    with pytest.raises(ToolError, match="at least one"):
        await t.add_agent_exception.fn(text="x", agent=alice["_id"])
    gone = await t.delete_agent_memory.fn(memory_id=rows[0]["id"], agent=alice["_id"])
    assert gone["ok"] is True and len(await t.get_agent_memory.fn(agent=alice["_id"])) == 1
    with pytest.raises(ToolError, match="Memory not found"):
        await t.delete_agent_memory.fn(memory_id="nope", agent=alice["_id"])


async def test_schedule_and_slack(mock_db, alice, monkeypatch):
    out = await t.set_agent_schedule.fn(time="7:30 pm", tz="Europe/Paris", agent=alice["_id"])
    assert out["schedule"] == {"time": "19:30", "tz": "Europe/Paris"}
    with pytest.raises(ToolError, match="time"):
        await t.set_agent_schedule.fn(time="whenever", agent=alice["_id"])
    from services import slack_bot_service
    monkeypatch.setattr(slack_bot_service, "find_channel", AsyncMock(return_value={"id": "C1", "name": "ads"}))
    out = await t.set_agent_slack_channel.fn(channel_id="ads", agent=alice["_id"])
    assert out["channel"] == {"id": "C1", "name": "ads"}
    monkeypatch.setattr(slack_bot_service, "find_channel", AsyncMock(return_value=None))
    with pytest.raises(ToolError, match="channel"):
        await t.set_agent_slack_channel.fn(channel_id="zzz", agent=alice["_id"])


async def test_graduate_rule_without_offer_is_error_dict(mock_db, alice):
    rule = await _rule(mock_db, alice, "pause_over_target")
    out = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"])
    assert out == {"ok": False, "error": "There is no offer to answer for this rule", "status": 409}
    with pytest.raises(ToolError, match="Rule not found"):
        await t.graduate_rule.fn(rule_id="zzz", choice="keep_asking", agent=alice["_id"])


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

async def test_read_tools(mock_db, alice, monkeypatch):
    play = await t.get_agent_playbook.fn(agent=alice["_id"])
    assert {r["kind"] for r in play["rules"]} >= {"pause_over_target"}
    assert (await t.get_agent_claims.fn(agent=alice["_id"]))["clients"][0]["name"] == "Acme"
    hist = await t.get_agent_history.fn(agent=alice["_id"])
    assert set(hist) == {"total", "ready", "clients"}
    act = await t.get_agent_activity.fn(agent=alice["_id"])
    assert act["items"] == [] and act["next_cursor"] is None
    assert isinstance(await t.get_agent_offers.fn(agent=alice["_id"]), list)
    _ready(monkeypatch)
    conf = await t.get_agent_confidence.fn(agent=alice["_id"])
    assert conf["ready"] is True
    assert (await agent_store.get_agent(mock_db, ALICE, alice["_id"]))["confidence"]["score"] == 80
    impact = await t.get_agent_impact.fn(agent=alice["_id"])
    assert impact
    with pytest.raises(ToolError, match="YYYY-MM"):
        await t.get_agent_impact.fn(month="nope", agent=alice["_id"])
    with pytest.raises(ToolError, match="cursor"):
        await t.get_agent_activity.fn(cursor="@@@", agent=alice["_id"])


# ---------------------------------------------------------------------------
# Cross-account and scope
# ---------------------------------------------------------------------------

async def test_other_users_agent_is_unreachable_and_nothing_changes(mock_db, alice, set_current_user):
    rule = await _rule(mock_db, alice)
    await _proposal(mock_db, alice, rule)
    await _auto_suggestion(mock_db, alice)
    job = {"_id": "jb", "agent_id": alice["_id"], "user_id": ALICE, "status": "done", "result": {"secret": 1},
           "created_at": datetime.utcnow()}
    await mock_db[agent_store.JOBS].insert_one(job)
    set_current_user(BOB)
    await _custom(mock_db, user=BOB, name="Bobs", gids=())
    aid = alice["_id"]
    # READ
    with pytest.raises(ToolError):
        await t.get_agent_playbook.fn(agent=aid)
    with pytest.raises(ToolError):
        await t.get_agent_proposals.fn(agent=aid)
    # job id of another user, even through bob's own agent
    bobs = (await agent_store.list_agents(mock_db, BOB))[0]["_id"]
    with pytest.raises(ToolError, match="Job not found"):
        await t.get_dry_run.fn(job_id="jb", agent=bobs)
    with pytest.raises(ToolError):
        await t.get_dry_run.fn(job_id="jb", agent=aid)
    # WRITE
    with pytest.raises(ToolError):
        await t.accept_proposal.fn(proposal_id="p1", agent=aid)
    with pytest.raises(ToolError, match="Proposal not found"):
        await t.accept_proposal.fn(proposal_id="p1", agent=bobs)
    with pytest.raises(ToolError):
        await t.set_rule_param.fn(rule_kind="pause_over_target", param="cpl_multiple", value=9, agent=aid)
    # LIVE
    with pytest.raises(ToolError):
        await t.activate_agent.fn(agent=aid)
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.undo_auto_action.fn(suggestion_id="s1", agent=bobs)
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    assert (await mock_db[agent_store.PROPOSALS].find_one({"_id": "p1"}))["status"] == "open"
    assert (await _rule(mock_db, alice, "pause_over_target"))["params"]["cpl_multiple"] == 1.5
    assert (await agent_store.get_agent(mock_db, ALICE, aid))["status"] != "active"
    assert (await mock_db[sug_store.SUGGESTIONS].find_one({"_id": "s1"}))["status"] == sug_store.STATUS_APPLIED


async def test_read_scope_cannot_call_write_or_live_tools(mcp_call, mock_db, alice):
    for tool, args in [("accept_proposal", {"proposal_id": "p"}), ("set_rule_param", {"rule_kind": "x", "param": "y", "value": 1}),
                       ("activate_agent", {}), ("start_dry_run", {}), ("deactivate_agent", {})]:
        res = await mcp_call(tool, {**args, "agent": alice["_id"]}, scope="birdy.read", token_type="mcp")
        assert res.is_error and "view-only" in res.text, tool


async def test_write_scope_live_tool_previews_over_the_wire(mcp_call, mock_db, alice, monkeypatch):
    _ready(monkeypatch)
    res = await mcp_call("activate_agent", {"agent": alice["_id"]}, scope="birdy.write", token_type="mcp")
    assert not res.is_error and "confirm_code" in res.text
    res = await mcp_call("get_agent_playbook", {"agent": alice["_id"]}, scope="birdy.read", token_type="mcp")
    assert not res.is_error


async def test_no_subscription_blocks_external_calls(mcp_call, no_subscription, alice):
    res = await mcp_call("get_agent_playbook", {"agent": alice["_id"]}, scope="birdy.read", token_type="mcp")
    assert res.is_error


# ---- I2: graduating a rule to auto is two-step -------------------------------------------------------------

async def _offer(db, rule):
    await db[agent_store.RULES].update_one({"_id": rule["_id"]}, {"$set": {"mode": "ask", "stats.streak": 10}})


async def test_graduate_rule_to_auto_needs_confirmation(mock_db, alice):
    rule = await _rule(mock_db, alice, "pause_over_target")
    await _offer(mock_db, rule)
    first = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"])
    assert first["needs_confirmation"] and "without asking" in first["preview"]["result"]
    assert (await mock_db[agent_store.RULES].find_one({"_id": rule["_id"]}))["mode"] == "ask"
    other = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_silent", agent=alice["_id"],
                                     confirm_code=first["confirm_code"])
    assert "Nothing was changed" in other["error"]
    first = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"])
    out = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"],
                                   confirm_code=first["confirm_code"])
    assert out["ok"] and out["rule"]["mode"] == "auto"


async def test_graduate_rule_keep_asking_is_one_step(mock_db, alice):
    rule = await _rule(mock_db, alice, "pause_over_target")
    await _offer(mock_db, rule)
    out = await t.graduate_rule.fn(rule_id=rule["_id"], choice="keep_asking", agent=alice["_id"])
    assert out["ok"] and out["rule"]["mode"] == "ask"
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0


async def test_graduate_rule_without_offer_issues_no_code(mock_db, alice):
    rule = await _rule(mock_db, alice, "pause_over_target")
    out = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_silent", agent=alice["_id"])
    assert out["ok"] is False and await mock_db["mcp_confirm_codes"].count_documents({}) == 0


async def test_graduate_rule_edited_after_preview_refused(mock_db, alice):
    rule = await _rule(mock_db, alice, "pause_over_target")
    await _offer(mock_db, rule)
    first = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"])
    await mock_db[agent_store.RULES].update_one({"_id": rule["_id"]}, {"$set": {"params.cpl_multiple": 9}})
    out = await t.graduate_rule.fn(rule_id=rule["_id"], choice="auto_notify", agent=alice["_id"],
                                   confirm_code=first["confirm_code"])
    assert "changed since the preview" in out["error"]
    assert (await mock_db[agent_store.RULES].find_one({"_id": rule["_id"]}))["mode"] == "ask"
