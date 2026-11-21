"""
tests/test_team_mcp.py
----------------------
The MCP asks (ai/mcp/team_mcp.py): ask_media_buyer and ask_sales_coach run the agent's own chat reply
through the web chat's path (routers/chat.run_chat_reply), billed once and archived under that agent.
Everything runs on mongomock with scripted providers: no model calls, no live DB, no external calls.
"""

import asyncio
import json
import types

import pytest
from fastmcp.exceptions import ToolError

import credits
from ai.mcp import team_mcp as t
from ai.mcp.outcome import CHAT_BILLED, get_outcome, reset_outcome
from ai.providers.base import ProviderResponse, Usage
from core.config import FRONTEND_URL
from tests.test_agent_team import G1, G2, U, TeamProvider, call, registry, say  # noqa: F401 (fixture)


@pytest.fixture(autouse=True)
def isolate(monkeypatch, mock_db):
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    credits._settings_cache["data"] = None
    reset_outcome()
    yield
    credits._settings_cache["data"] = None


@pytest.fixture
def owner(set_current_user):
    set_current_user(U)


@pytest.fixture
def use(monkeypatch, registry):
    """use(provider): the chat path answers with this scripted provider and the stub tools; no credit gate."""
    import routers.chat as chat_router

    async def no_gate(user, client):
        return None

    monkeypatch.setattr(chat_router, "check_credits", no_gate)
    monkeypatch.setattr(chat_router, "registry", registry)

    def _use(provider):
        async def get_provider(user, db):
            return provider
        monkeypatch.setattr(chat_router, "get_provider_for_user", get_provider)
        return provider

    return _use


@pytest.fixture
async def groups(mock_db):
    await mock_db["client_groups"].insert_many([{"user_id": U, "id": G1, "name": "Aura"},
                                                {"user_id": U, "id": G2, "name": "Beaulea"},
                                                {"user_id": "someone@else.com", "id": "grp-x", "name": "Aura"}])


async def _fund(db, amount=1000.0):
    await db["users"].insert_one({"user_id": U, "credits": {**credits._effective_credits({})[0],
                                                            "topup_balance": amount}})


async def _log(db, **match):
    return await db["ai_conversation_log"].find({"user_id": U, **match}).sort("created_at", 1).to_list(None)


# ── answer and trail ──────────────────────────────────────────────────────────

async def test_media_buyer_answer_then_trail(mock_db, owner, use, registry):
    use(TeamProvider({"media_buyer": [call("get_campaign_insights", date_preset="last_7d"),
                                      say("**Aura** is fine: CPL held at £12.")]}))
    text = await t.ask_media_buyer.fn(question="How is Aura doing?")
    answer, trail, convo = text.split("\n\n")
    assert answer == "**Aura** is fine: CPL held at £12."
    lines = trail.split("\n")
    assert lines[0] == "Route:"
    assert lines[1].startswith("1. Media Buyer, asked by you · used Campaign figures (last_7d) · ")
    assert lines[1].endswith(" credits") and " s · " in lines[1]
    assert lines[2].startswith("Total: ") and lines[2].endswith(" credits")
    sid = convo.split()[2]
    assert convo == (f"Conversation id: {sid} (pass it as conversation_id to ask_media_buyer "
                     "to continue this conversation)")
    assert "Proposals" not in text
    assert registry.seen["get_campaign_insights"]


async def test_trail_names_who_asked_whom(mock_db, owner, use):
    use(TeamProvider({
        "media_buyer": [call("ask_birdy", question="Are the calls converting for Aura?", client="agency"),
                        say("Ads are fine; the calls are the problem.")],
        "birdy": [call("ask_sales_coach", question="Aura call conversion last 7 days?", client="agency"),
                  say("3 of 40 calls booked.")],
        "sales_coach": [call("get_call_center_stats"), say("3 of 40 calls booked.\nSUMMARY: 40 calls checked")],
    }))
    text = await t.ask_media_buyer.fn(question="Why are leads not closing?")
    trail = text.split("\n\n")[1].split("\n")
    assert trail[1].startswith("1. Media Buyer, asked by you · used Asked Birdy (Are the calls converting for Aura?)")
    assert trail[2].startswith('2. Birdy, asked by Media Buyer ("Are the calls converting for Aura?") · used Asked Sales Coach')
    assert trail[3].startswith('3. Sales Coach, asked by Birdy ("Aura call conversion last 7 days?") · used Call stats · ')


def test_format_result_exact_text():
    route = {"status": "done", "seconds": 21.4, "credits": 1.234, "limit_hit": None, "steps": [
        {"agent": "media_buyer", "name": "Media Buyer", "asked_by": None, "question": None, "status": "done",
         "tools": [{"name": "get_campaign_insights", "label": "Campaign figures", "detail": "last_7d"}],
         "seconds": 21.4, "credits": 0.9},
        {"agent": "birdy", "name": "Birdy", "asked_by": "media_buyer", "question": "Calls for Aura?",
         "status": "done", "tools": [], "seconds": 4.0, "credits": 0.334}]}
    proposals = [{"id": "p_1", "agent": "media_buyer", "title": "Pause the tired ad", "detail": "Ad 12: active → paused",
                  "kind": "pause_creative_ads", "status": "pending"},
                 {"id": "p_2", "agent": "sales_coach", "title": "Analyze 10 calls", "detail": "",
                  "kind": "analyze_call_recordings", "status": "pending"}]
    url = f"{FRONTEND_URL}/media-buyer/chat?c=s1"
    text = t.format_result("media_buyer", {"reply": "Pause ad 12.", "route": route, "proposals": proposals,
                                           "session_id": "s1"}, url)
    assert text == (
        "Pause ad 12.\n\n"
        "Route:\n"
        "1. Media Buyer, asked by you · used Campaign figures (last_7d) · 21.4 s · 0.9 credits\n"
        '2. Birdy, asked by Media Buyer ("Calls for Aura?") · used no tools · 4 s · 0.334 credits\n'
        "Total: 21.4 s · 1.234 credits\n\n"
        "Proposals waiting for the owner's approval in Birdy. Nothing has changed: only the owner can approve "
        f"or decline them, at {url}\n"
        "1. Pause the tired ad: Ad 12: active → paused (Media Buyer would make it once approved)\n"
        "2. Analyze 10 calls: kind analyze call recordings, no details given (Sales Coach would make it once approved)\n\n"
        "Conversation id: s1 (pass it as conversation_id to ask_media_buyer to continue this conversation)"
    )


# ── billing ───────────────────────────────────────────────────────────────────

async def test_billed_once_like_a_web_chat_reply(mock_db, owner, use):
    await _fund(mock_db)
    use(TeamProvider({
        "media_buyer": [call("ask_birdy", question="Calls?", client="agency"), say("Fine.")],
        "birdy": [say("Calls are fine.")],
    }))
    text = await t.ask_media_buyer.fn(question="Anything wrong?")
    rows = await mock_db["ai_usage"].find({}).to_list(None)
    assert len(rows) == 1
    row = rows[0]
    # the web chat's ledger row: source birdy, the addressed agent at depth 0, every agent in the breakdown
    assert row["source"] == "birdy" and row["feature"] == "ask_birdy"
    assert row["agent"] == "media_buyer" and row["depth"] == 0
    assert [a["agent"] for a in row["agents"]] == ["media_buyer", "birdy"]
    assert row["session_id"] == text.rsplit("Conversation id: ", 1)[1].split()[0]
    outcome = get_outcome()
    assert outcome["kind"] == CHAT_BILLED and outcome["credits"] == row["credits"] > 0
    assert outcome["link"] == f"/media-buyer/chat?c={row['session_id']}"
    user = await mock_db["users"].find_one({"user_id": U})
    assert user["credits"]["topup_balance"] == pytest.approx(1000.0 - row["credits"])


async def test_external_call_adds_no_mcp_fee_and_signs_off_with_the_chat_cost(mcp_call, mock_db, use):
    await _fund(mock_db, 100.0)
    use(TeamProvider({"sales_coach": [call("get_call_center_stats"), say("212 calls, 30 booked.")]}))
    res = await mcp_call("ask_sales_coach", {"question": "How did calls go?"}, user=U,
                         scope="birdy.read birdy.write", token_type="mcp")
    assert not res.is_error and res.text.startswith("212 calls, 30 booked.")
    await asyncio.sleep(0)   # let the shielded charge-and-record task finish
    rows = await mock_db["ai_usage"].find({}).to_list(None)
    assert [r["source"] for r in rows] == ["birdy"]          # the reply's charge, no MCP per-call fee
    sid = res.text.rsplit("Conversation id: ", 1)[1].split()[0]
    assert res.signoff == (f"Birdy: see it in Birdy {FRONTEND_URL}/sales-coach/chat?c={sid} · "
                           f"this request cost {rows[0]['credits']:g} credits ({100.0 - rows[0]['credits']:,.1f} left)")
    activity = await mock_db["mcp_activity"].find_one({"tool": "ask_sales_coach"})
    assert activity["credits"]["total"] == round(rows[0]["credits"], 4)


async def test_view_only_connection_cannot_ask(mcp_call, mock_db, use):
    await _fund(mock_db)
    p = use(TeamProvider({}))
    res = await mcp_call("ask_media_buyer", {"question": "q"}, user=U, scope="birdy.read", token_type="mcp")
    assert res.is_error and "view-only" in res.text
    assert p.calls == [] and await mock_db["ai_usage"].count_documents({}) == 0


async def test_out_of_credits_is_refused_before_the_agent_runs(mock_db, owner, use, monkeypatch):
    from fastapi import HTTPException
    import routers.chat as chat_router

    async def out(user, client):
        raise HTTPException(status_code=402, detail={"code": "OUT_OF_CREDITS"})

    p = use(TeamProvider({}))
    monkeypatch.setattr(chat_router, "check_credits", out)
    with pytest.raises(ToolError, match="out of Birdy credits"):
        await t.ask_media_buyer.fn(question="q")
    assert p.calls == [] and await mock_db["ai_conversation_log"].count_documents({}) == 0


# ── client scoping ────────────────────────────────────────────────────────────

async def test_client_by_name_scopes_the_run(mock_db, owner, use, groups, registry):
    p = use(TeamProvider({"media_buyer": [call("get_campaign_insights"), say("Aura is fine.")]}))
    text = await t.ask_media_buyer.fn(question="How is it doing?", client="aura")
    system = p.calls[0][2][0]["content"]
    assert "Aura" in system and G1 in system
    assert all(r["client_group_id"] == G1 for r in await _log(mock_db))
    # the one-client run pins the client on its tool calls
    assert registry.seen["get_campaign_insights"][0].get("group_id") == G1 or \
        G1 in json.dumps(registry.seen["get_campaign_insights"][0])
    sid = text.rsplit("Conversation id: ", 1)[1].split()[0]
    assert get_outcome()["link"] == f"/media-buyer/chat?client={G1}&c={sid}"


async def test_ambiguous_client_returns_candidates_and_asks_nothing(mock_db, owner, use, groups):
    await mock_db["client_groups"].insert_one({"user_id": U, "id": "grp-aura2", "name": "Aura Dental"})
    p = use(TeamProvider({}))
    with pytest.raises(ToolError) as e:
        await t.ask_media_buyer.fn(question="q", client="aur")
    assert "Several clients match" in str(e.value) and "Aura" in str(e.value) and "Aura Dental" in str(e.value)
    assert p.calls == [] and await mock_db["ai_conversation_log"].count_documents({}) == 0


async def test_another_users_client_is_never_found(mock_db, owner, use, groups):
    use(TeamProvider({}))
    with pytest.raises(ToolError, match="No client matches"):
        await t.ask_sales_coach.fn(question="q", client="grp-x")


# ── proposals ─────────────────────────────────────────────────────────────────

async def test_proposals_are_listed_and_never_applied(mock_db, owner, use, registry):
    use(TeamProvider({
        "media_buyer": [call("ask_birdy", question="Should we look at Aura's calls?", client="agency"),
                        say("Ads are fine. Sales Coach proposes analysing the calls.")],
        "birdy": [call("ask_sales_coach", question="Analyse Aura's calls?", client="agency"),
                  say("Sales Coach proposed it.")],
        "sales_coach": [call("propose_action", title="Analyze 10 Aura calls", detail="10 of 38 recorded calls",
                             kind="analyze_call_recordings", payload={"limit": 10}),
                        say("Proposed.")],
    }))
    text = await t.ask_media_buyer.fn(question="Why is Aura not closing?")
    sid = text.rsplit("Conversation id: ", 1)[1].split()[0]
    url = f"{FRONTEND_URL}/media-buyer/chat?c={sid}"
    assert ("Proposals waiting for the owner's approval in Birdy. Nothing has changed: only the owner can approve "
            f"or decline them, at {url}\n"
            "1. Analyze 10 Aura calls: 10 of 38 recorded calls (Sales Coach would make it once approved)") in text
    assert "analyze_call_recordings" not in registry.seen
    # still pending on the archived reply; no apply turn ran (one question, one answer)
    archived = await _log(mock_db, role="assistant")
    assert len(archived) == 1 and archived[0]["proposals"][0]["status"] == "pending"
    assert await mock_db["ai_conversation_log"].count_documents({}) == 2


def test_no_proposal_tool_is_reachable_from_the_ask_module():
    import inspect
    src = inspect.getsource(t)
    for name in ("claim(", "apply_proposal", "dismiss_proposal", "set_fields", "accept_proposal"):
        assert name not in src


# ── logged like a web chat with that agent ────────────────────────────────────

async def test_conversation_is_logged_under_the_agent_and_listed(mock_db, mock_mongo_client, owner, use, monkeypatch):
    use(TeamProvider({"sales_coach": [say("212 calls.")]}))
    text = await t.ask_sales_coach.fn(question="How many calls this week?")
    sid = text.rsplit("Conversation id: ", 1)[1].split()[0]
    rows = await _log(mock_db)
    assert [(r["role"], r["agent"], r["page"], r["source"], r["session_id"]) for r in rows] == [
        ("user", "sales_coach", "call_center", "birdy", sid), ("assistant", "sales_coach", "call_center", "birdy", sid)]
    assert rows[0]["content"] == "How many calls this week?" and rows[1]["route"]["steps"][0]["agent"] == "sales_coach"

    import contextlib
    import routers.chat as chat_router

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    listed = await chat_router.list_conversations(agent="sales_coach", current_user=U)
    assert [c["session_id"] for c in listed["conversations"]] == [sid]
    assert (await chat_router.list_conversations(agent="media_buyer", current_user=U))["conversations"] == []


async def test_continue_a_conversation(mock_db, owner, use, groups):
    p = use(TeamProvider({"media_buyer": [say("Aura is fine."), say("Still fine.")]}))
    first = await t.ask_media_buyer.fn(question="How is Aura?", client="Aura")
    sid = first.rsplit("Conversation id: ", 1)[1].split()[0]
    second = await t.ask_media_buyer.fn(question="And now?", conversation_id=sid)
    assert second.startswith("Still fine.") and f"Conversation id: {sid} " in second
    # the second turn saw the first, and kept the conversation's client
    msgs = p.calls[-1][2]
    assert any(m["role"] == "user" and m["content"] == "How is Aura?" for m in msgs)
    assert all(r["client_group_id"] == G1 for r in await _log(mock_db))
    assert await mock_db["ai_conversation_log"].count_documents({"session_id": sid}) == 4


async def test_continue_refuses_another_agents_or_unknown_conversation(mock_db, owner, use):
    use(TeamProvider({"sales_coach": [say("212 calls.")]}))
    text = await t.ask_sales_coach.fn(question="Calls?")
    sid = text.rsplit("Conversation id: ", 1)[1].split()[0]
    with pytest.raises(ToolError, match="not a chat with Media Buyer. It is with Sales Coach: use ask_sales_coach"):
        await t.ask_media_buyer.fn(question="q", conversation_id=sid)
    with pytest.raises(ToolError, match="No conversation nope was found"):
        await t.ask_media_buyer.fn(question="q", conversation_id="nope")
    await mock_db["ai_conversation_log"].update_many({}, {"$set": {"user_id": "someone@else.com"}})
    with pytest.raises(ToolError, match="No conversation"):
        await t.ask_sales_coach.fn(question="q", conversation_id=sid)


# ── time limit and guards ─────────────────────────────────────────────────────

async def test_timeout_answers_clearly_and_the_reply_still_finishes(mock_db, owner, use, monkeypatch):
    await _fund(mock_db)
    monkeypatch.setattr(t, "ASK_LIMIT_SECONDS", 0.05)
    use(TeamProvider({"media_buyer": [say("Late answer.")]}, delays={"media_buyer": 0.3}))
    with pytest.raises(ToolError) as e:
        await t.ask_media_buyer.fn(question="Slow one")
    assert "did not finish within 0.05 seconds" in str(e.value)
    assert f"{FRONTEND_URL}/media-buyer/chat" in str(e.value)
    assert get_outcome() is None    # nothing billed through the MCP for it
    await asyncio.gather(*list(t._RUNNING))
    assert [r["content"] for r in await _log(mock_db, role="assistant")] == ["Late answer."]
    assert await mock_db["ai_usage"].count_documents({}) == 1    # still billed once, by the chat


async def test_limit_fits_the_deployment():
    from ai.team import route as rt
    # the reply's own budget answers first; the backstop sits under Vercel's 300 s request limit
    assert rt.TURN_BUDGET_SECONDS < t.ASK_LIMIT_SECONDS < 300


async def test_blank_question_is_refused(owner):
    with pytest.raises(ToolError, match="Give the question"):
        await t.ask_sales_coach.fn(question="  ")


async def test_internal_callers_are_refused(monkeypatch, use):
    p = use(TeamProvider({}))
    token = types.SimpleNamespace(claims={"sub": U, "type": "access"})
    monkeypatch.setattr("ai.mcp.server.get_access_token", lambda: token)
    with pytest.raises(ToolError, match="for assistants outside Birdy"):
        await t.ask_media_buyer.fn(question="q")
    assert p.calls == []


async def test_birdys_own_chat_is_never_offered_the_asks():
    from ai import mcp_client

    class FakeClient:
        async def list_tools(self):
            return [types.SimpleNamespace(name=n, description="d", inputSchema={"type": "object"})
                    for n in ("get_alerts", "ask_media_buyer", "ask_sales_coach")]

    schemas = await mcp_client.get_mcp_schemas(FakeClient(), None)
    assert [s["function"]["name"] for s in schemas] == ["get_alerts"]


async def test_descriptions_say_when_to_ask_and_what_it_costs():
    from ai.mcp import mcp
    from ai.mcp.hello_mcp import CAN_DO
    from ai.mcp.voice import INSTRUCTIONS
    tools = await mcp.get_tools()
    for name in ("ask_media_buyer", "ask_sales_coach"):
        d = tools[name].description
        assert tools[name].tags == {"write"}
        assert "judgement" in d and "plain numbers" in d and "costs credits like a chat reply" in d
        assert "cannot approve or decline" in d and "conversation_id" in d
        assert tools[name].parameters["required"] == ["question"]
    assert tools["ask_media_buyer"].title == "Asking Media Buyer"
    assert "ask_media_buyer" in INSTRUCTIONS and "ask_sales_coach" in INSTRUCTIONS
    assert any("Sales Coach" in c and "credits" in c for c in CAN_DO)


async def test_no_answer_text_falls_back(mock_db, owner, use):
    use(TeamProvider({"sales_coach": [ProviderResponse(content="", usage=Usage(1, 1))]}))
    text = await t.ask_sales_coach.fn(question="q")
    assert text.split("\n\n")[0]   # never an empty first block


# ── read and propose only (ai/team/propose_only.py) ───────────────────────────

@pytest.fixture
def more_tools(registry):
    """Stubs for write and lesson tools the agents own, so the offer shows whether they are given."""
    for name in ("save_lesson", "edit_lesson", "remove_lesson", "list_lessons", "render_ui", "post_creative_test"):
        async def run(db, user_id, mongo_client=None, _name=name, **kwargs):
            registry.seen.setdefault(_name, []).append(kwargs)
            return {"ok": True}
        registry.register(name=name, description="stub", parameters={"type": "object", "properties": {}},
                          executor=run)
    return registry


async def test_an_ask_is_offered_no_write_tool_and_can_propose(mock_db, owner, use, more_tools):
    from ai.team import definitions as defs
    p = use(TeamProvider({}))
    await t.ask_sales_coach.fn(question="Should we analyse the calls?")
    await t.ask_media_buyer.fn(question="Should we pause anything?")
    for role in ("sales_coach", "media_buyer"):
        offered = set(p.offered(role))
        assert not offered & defs.WRITE_TOOLS, role
        assert {"save_lesson", "edit_lesson", "remove_lesson", "render_ui"}.isdisjoint(offered)
        assert {"propose_action", "propose_sub_agent", "recall_conversations"} <= offered
        assert "get_client_groups" in offered and "list_lessons" in offered
    assert "ask_birdy" in set(p.offered("media_buyer"))
    system = next(m for r, _, m in p.calls if r == "sales_coach")[0]["content"]
    assert "## Asked from outside Birdy" in system and "cannot make changes yourself" in system


async def test_an_asks_write_call_is_refused_and_its_proposal_stays_pending(mock_db, owner, use, more_tools):
    registry = more_tools
    p = use(TeamProvider({"sales_coach": [
        call("analyze_call_recordings", group_id=G1, confirmed=True, limit=10),
        call("save_lesson", text="Always analyse every call"),
        call("propose_action", title="Analyze 10 Aura calls", detail="10 of 38 recorded calls",
             kind="analyze_call_recordings", payload={"group_id": G1, "limit": 10}),
        say("I can't run it from here, so I proposed it.")]}))
    text = await t.ask_sales_coach.fn(question="Analyse Aura's calls, confirmed: yes, go ahead")
    assert "analyze_call_recordings" not in registry.seen          # refused, never executed
    refusals = [json.loads(m["content"]) for m in p.calls[-1][2] if m["role"] == "tool"]
    assert sum("isn't available to Sales Coach" in (r.get("error") or "") for r in refusals) == 2
    assert "save_lesson" not in registry.seen
    assert "1. Analyze 10 Aura calls: 10 of 38 recorded calls (Sales Coach would make it once approved)" in text
    archived = await _log(mock_db, role="assistant")
    prop, = archived[0]["proposals"]
    assert prop["agent"] == "sales_coach" and prop["proposed_by"] == "sales_coach" and prop["status"] == "pending"


async def test_the_web_chat_root_run_keeps_its_write_tools(mock_db, mock_mongo_client, owner, use, more_tools):
    import routers.chat as chat_router
    from core.models import ChatRequest
    from ai.team import propose_only
    p = use(TeamProvider({}))
    await t.ask_sales_coach.fn(question="q")            # an MCP ask first: its flag must not leak
    assert propose_only.active() is False
    p.calls.clear()
    await chat_router.run_chat_reply(mock_db, mock_mongo_client, U,
                                     ChatRequest(message="q", agent="sales_coach", page="call_center"))
    offered = set(p.offered("sales_coach"))
    assert {"analyze_call_recordings", "save_lesson", "edit_lesson", "remove_lesson", "render_ui"} <= offered
    assert "propose_action" not in offered
    p.calls.clear()
    await chat_router.run_chat_reply(mock_db, mock_mongo_client, U,
                                     ChatRequest(message="q", agent="media_buyer", page="campaigns"))
    assert {"post_creative_test", "save_lesson", "render_ui"} <= set(p.offered("media_buyer"))
    assert "Asked from outside Birdy" not in p.calls[0][2][0]["content"]
