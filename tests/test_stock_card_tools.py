"""The per-client card chat's tools (BirdyHub redesign, spec section 3.2): every action goes through the
same apply / dismiss / edit / lesson code as the dashboard, refuses on a card that was decided meanwhile,
and never reaches another user's card."""

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

import routers.chat as chat_router
from ai.agents.stock import apply as stock_apply
from ai.agents.stock import cards as stock_cards
from ai.agents.store import CARDS, MEMORY
from ai.tools import stock_card_tools as t
from dependencies import get_current_user
from main import app
from test_agent_learning import FakeProvider, use_provider
from test_stock_router import OTHER, _seed_card, _seed_run
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture


@pytest.fixture(autouse=True)
def calm_ai(monkeypatch):
    use_provider(monkeypatch, FakeProvider(error=RuntimeError("no network in tests")))

    async def not_blocked(db, user_id):
        return False

    monkeypatch.setattr("credits.is_blocked", not_blocked)


@pytest.fixture(autouse=True)
def _reset_card():
    token = t.current_stock_card.set(None)
    ro_token = t.current_card_read_only.set(False)
    yield
    t.current_card_read_only.reset(ro_token)
    t.current_stock_card.reset(token)


async def _card(db, cid="c1", **kw):
    agent = await _setup(db, "g1")
    await _seed_run(db, agent, rid="r1")
    await _seed_card(db, agent, cid, "r1", client_name="Client g1", **kw)
    t.current_stock_card.set(cid)
    return agent


async def test_apply_all_lines_calls_apply_card(mock_db, monkeypatch):
    await _card(mock_db)
    calls = []

    async def fake_apply(db, mongo_client, user_id, card_id, **kw):
        calls.append((user_id, card_id, kw))
        card = await db[CARDS].find_one({"_id": card_id})
        lines = [{"id": ln["id"], "state": "applied", "error": None} for ln in card["lines"]]
        return {"ok": True, "outcome": "applied", "reason": None, "card": card, "lines": lines}

    monkeypatch.setattr(stock_apply, "apply_card", fake_apply)
    out = await t.apply_card_lines(mock_db, USER, mongo_client="mc")
    assert calls == [(USER, "c1", {"by": USER, "source": "chat", "line_ids": None, "budget_s": 20})]
    assert out["ok"] is True and out["changed"] == "applied" and out["count"] == 2
    assert [ln["state"] for ln in out["lines"]] == ["applied", "applied"]
    await t.apply_card_lines(mock_db, USER, line_ids=["l2"])
    assert calls[-1][2]["line_ids"] == ["l2"]


@pytest.mark.parametrize("status,preview", [("applied", False), ("dismissed", False), ("superseded", False),
                                            ("open", True)])
async def test_apply_on_closed_card_refuses(mock_db, monkeypatch, status, preview):
    await _card(mock_db, status=status, preview=preview)

    async def never(*a, **k):
        raise AssertionError("apply_card must not run on a closed card")

    monkeypatch.setattr(stock_apply, "apply_card", never)
    out = await t.apply_card_lines(mock_db, USER)
    assert out["ok"] is False and out["error"] == "card_closed"
    assert (await t.dismiss_card(mock_db, USER, reason="x"))["error"] == "card_closed"
    assert (await t.edit_card(mock_db, USER, next_steps="x"))["error"] == "card_closed"


async def test_apply_on_card_closed_meanwhile_refuses(mock_db):
    """The card was open when the chat opened, then a newer run replaced it: apply_card's own refusal wins."""
    agent = await _card(mock_db)
    await _seed_card(mock_db, agent, "c2", "r1")
    await mock_db[CARDS].update_one({"_id": "c2"}, {"$set": {"created_at": (await mock_db[CARDS].find_one(
        {"_id": "c1"}))["created_at"].replace(hour=9)}})
    out = await t.apply_card_lines(mock_db, USER)
    assert out["ok"] is False and out["error"] == "card_closed" and "newer" in out["reason"]
    assert "changed" not in out


async def test_apply_flag_line_reports_not_applied(mock_db):
    await _card(mock_db, lines=[{"id": "l1", "kind": "remove", "ad_name": "A", "meta_ids": ["a1"], "mode": "flag",
                                 "state": "flagged"}])
    out = await t.apply_card_lines(mock_db, USER)   # the real apply_card: it never writes a Flag line
    # ok is False: nothing went to Meta, so the result can't license "Done, I've applied it"
    assert out["ok"] is False and out["error"] == "nothing_to_apply" and out["outcome"] == "noop"
    assert "changed" not in out and out["count"] == 0
    assert out["lines"] == [{"id": "l1", "state": "flagged", "error": None, "reason_code": None, "note": None}]
    assert t.change_entry("apply_card_lines", out) is None   # nothing for the chat to show as applied


async def test_apply_lines_already_decided_is_not_ok(mock_db):
    await _card(mock_db, status="partly_applied", lines=[
        {"id": "l1", "kind": "remove", "ad_name": "A", "meta_ids": ["a1"], "mode": "ask", "state": "applied"}])
    out = await t.apply_card_lines(mock_db, USER, line_ids=["l1"])
    assert out["ok"] is False and out["error"] == "nothing_to_apply" and out["count"] == 0
    assert "changed" not in out


@pytest.mark.parametrize("status", ["open", "flagged", "partly_applied"])
async def test_read_only_chat_refuses_every_card_action(mock_db, monkeypatch, status):
    """A past run, a month, or a client outside today's batch: the dialog says nothing changes, so the
    tools refuse even when the card itself is still open."""
    await _card(mock_db, status=status)
    t.current_card_read_only.set(True)

    async def never(*a, **k):
        raise AssertionError("a read-only chat must not change the card")

    monkeypatch.setattr(stock_apply, "apply_card", never)
    monkeypatch.setattr(stock_apply, "dismiss_card", never)
    monkeypatch.setattr(stock_cards, "edit_card", never)
    for tool, kw in ((t.apply_card_lines, {}), (t.dismiss_card, {"reason": "x"}), (t.edit_card, {"next_steps": "x"})):
        out = await tool(mock_db, USER, **kw)
        assert out["ok"] is False and out["error"] == "card_closed" and out["read_only"] is True, tool.__name__
        assert t.change_entry(tool.__name__, out) == {"tool": tool.__name__, "changed": "stale"}
    got = await t.get_card(mock_db, USER)
    assert got["card"]["can_act"] is False and got["card"]["read_only"] is True
    assert (await t.prompt_context(mock_db, USER))["card"]["can_act"] is False


def test_refusals_tell_the_sheet_to_re_read():
    """card_closed and conflict mean the sheet is stale: they become a "stale" change for the FE."""
    assert t.change_entry("apply_card_lines", {"ok": False, "error": "card_closed"}) == {
        "tool": "apply_card_lines", "changed": "stale"}
    assert t.change_entry("edit_card", {"ok": False, "error": "conflict"})["changed"] == "stale"
    assert t.change_entry("apply_card_lines", {"ok": False, "error": "nothing_to_apply"}) is None
    assert t.change_entry("add_lesson", {"ok": False, "error": "empty_lesson"}) is None


async def test_dismiss_with_reason_makes_lesson(mock_db):
    await _card(mock_db)
    out = await t.dismiss_card(mock_db, USER, reason="We never pause the video ads")
    assert out == {"ok": True, "changed": "dismissed", "lesson_saved": True}
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert card["status"] == "dismissed" and card["decided_by"]["source"] == "chat"
    [lesson] = await mock_db[MEMORY].find({}).to_list(None)
    assert lesson["source"] == "dismiss" and lesson["source_ref"] == "c1" and lesson["client_group_id"] == "g1"
    assert (await t.dismiss_card(mock_db, USER))["error"] == "card_closed"


async def test_edit_card_conflict_returns_conflict(mock_db, monkeypatch):
    await _card(mock_db)

    async def lost(db, card, fields, **kw):
        return False

    monkeypatch.setattr(stock_cards, "guarded_update", lost)
    out = await t.edit_card(mock_db, USER, keep_line_ids=["l1"])
    assert out["ok"] is False and out["error"] == "conflict"


async def test_edit_card_keeps_lines_and_learns(mock_db):
    await _card(mock_db)
    out = await t.edit_card(mock_db, USER, keep_line_ids=["l2"], next_steps="Check on Monday")
    assert out["ok"] is True and out["changed"] == "edited"
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert [ln["id"] for ln in card["lines"]] == ["l2"] and card["next_steps"] == "Check on Monday"
    assert (await t.edit_card(mock_db, USER, keep_line_ids=["nope"]))["error"] == "bad_lines"
    assert await mock_db[MEMORY].count_documents({"source": "edit", "source_ref": "c1"}) == 1


async def test_add_lesson_scoped_to_client(mock_db):
    await _card(mock_db, status="applied")   # a lesson can come from a decided card too
    one = await t.add_lesson(mock_db, USER, text="Never pause video ads for this client")
    assert one["ok"] is True and one["changed"] == "lesson" and one["lesson"]["client_group_id"] == "g1"
    every = await t.add_lesson(mock_db, USER, text="Always keep 3 ads live", scope="all")
    assert every["lesson"]["client_group_id"] is None
    rows = await mock_db[MEMORY].find({}).to_list(None)
    assert {r["source"] for r in rows} == {"chat"} and {r["source_ref"] for r in rows} == {"c1"}
    assert (await t.add_lesson(mock_db, USER, text="  "))["error"] == "empty_lesson"
    assert (await t.add_lesson(mock_db, USER, text="x", scope="bogus"))["error"] == "bad_scope"


async def test_get_card_and_draft(mock_db):
    await _card(mock_db, spend=27.5, leads=0, currency="GBP")
    got = await t.get_card(mock_db, USER)
    assert got["ok"] and got["card"]["id"] == "c1" and got["card"]["can_act"] is True
    assert [ln["id"] for ln in got["card"]["lines"]] == ["l1", "l2"]
    draft = await t.draft_client_message(mock_db, USER, tone="formal")
    assert draft["ok"] and draft["facts"]["spend"] == 27.5 and draft["facts"]["tone"] == "formal"
    assert "changed" not in draft


async def test_tools_other_users_card_not_found(mock_db, monkeypatch):
    await _card(mock_db)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"user_id": OTHER}})

    async def never(*a, **k):
        raise AssertionError("must not reach another user's card")

    monkeypatch.setattr(stock_apply, "apply_card", never)
    monkeypatch.setattr(stock_apply, "dismiss_card", never)
    for tool, kw in ((t.get_card, {}), (t.apply_card_lines, {}), (t.dismiss_card, {}), (t.edit_card, {"next_steps": "x"}),
                     (t.add_lesson, {"text": "x"}), (t.draft_client_message, {})):
        assert await tool(mock_db, USER, **kw) == {"ok": False, "error": "not_found"}, tool.__name__
    t.current_stock_card.set(None)
    assert (await t.get_card(mock_db, USER))["error"] == "not_found"


async def test_load_chat_card_checks_agent(mock_db):
    agent = await _card(mock_db)
    assert (await t.load_chat_card(mock_db, USER, "c1", agent["_id"]))["_id"] == "c1"
    assert await t.load_chat_card(mock_db, USER, "c1", "another-agent") is None
    assert await t.load_chat_card(mock_db, OTHER, "c1") is None
    assert await t.load_chat_card(mock_db, USER, None) is None


async def test_chat_route_404_for_foreign_card(mock_db, mock_mongo_client, monkeypatch):
    await _card(mock_db)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"user_id": OTHER}})

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def run_chat(**kw):
        raise AssertionError("must not run the chat")

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "run_chat", run_chat)
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        r = TestClient(app).post("/api/chat", json={"message": "do it", "page": "stock_card", "card_id": "c1"})
        assert r.status_code == 404 and r.json()["detail"] == "card_not_found"
        r = TestClient(app).post("/api/chat", json={"message": "do it", "page": "stock_card"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_chat_route_sets_the_card_for_the_tools(mock_db, mock_mongo_client, monkeypatch):
    agent = await _card(mock_db)
    t.current_stock_card.set(None)
    seen = {}

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def ok(*a, **k):
        return None

    async def provider(user, db):
        return object()

    async def run_chat(**kw):
        seen.update(card=t.current_stock_card.get(), card_id=kw.get("card_id"), agent_id=kw.get("agent_id"))
        return {"reply": "hi", "tools_used": [], "session_id": "s1"}

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", ok)
    monkeypatch.setattr(chat_router, "get_provider_for_user", provider)
    monkeypatch.setattr(chat_router, "run_chat", run_chat)
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        r = TestClient(app).post("/api/chat", json={"message": "why?", "page": "stock_card", "card_id": "c1",
                                                    "agent_id": agent["_id"]})
        assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert seen == {"card": "c1", "card_id": "c1", "agent_id": agent["_id"]}
    assert t.current_stock_card.get() is None


async def test_chat_route_passes_read_only_to_the_tools(mock_db, mock_mongo_client, monkeypatch):
    agent = await _card(mock_db)
    t.current_stock_card.set(None)
    seen = []

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def ok(*a, **k):
        return None

    async def provider(user, db):
        return object()

    async def run_chat(**kw):
        seen.append(t.current_card_read_only.get())
        return {"reply": "hi", "tools_used": [], "session_id": "s1"}

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", ok)
    monkeypatch.setattr(chat_router, "get_provider_for_user", provider)
    monkeypatch.setattr(chat_router, "run_chat", run_chat)
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        body = {"message": "do it", "page": "stock_card", "card_id": "c1", "agent_id": agent["_id"]}
        assert TestClient(app).post("/api/chat", json={**body, "card_read_only": True}).status_code == 200
        assert TestClient(app).post("/api/chat", json=body).status_code == 200
        # only the stock_card page reads it
        assert TestClient(app).post("/api/chat", json={"message": "hi", "card_read_only": True}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert seen == [True, False, False]
    assert t.current_card_read_only.get() is False


async def test_run_chat_records_card_changes(mock_db, monkeypatch):
    from ai.orchestrator import run_chat
    from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
    from ai.tools.registry import ToolRegistry

    await _card(mock_db)

    class Scripted(BaseLLMProvider):
        def __init__(self, responses):
            self._r, self.model = list(responses), "stub-model"

        async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
            self.tools = tools
            return self._r.pop(0)

    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    reg = ToolRegistry()
    t.register_stock_card_tools(reg)
    provider = Scripted([
        ProviderResponse(content=None, finish_reason="tool_calls",
                         tool_calls=[ToolCall(id="a", name="dismiss_card", arguments=json.dumps({}))]),
        ProviderResponse(content="Skipped."),
    ])
    out = await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id=USER, message="skip it",
                         page="stock_card", card_id="c1")
    assert out["agent_changes"] == [{"tool": "dismiss_card", "changed": "dismissed"}]
    assert {s["function"]["name"] for s in provider.tools} == set(t.STOCK_CARD_TOOL_NAMES)
    session = await mock_db["ai_chat_sessions"].find_one({"_id": out["session_id"]})
    assert "Client g1" in session["messages"][0]["content"]   # the card's own prompt
    log = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert log["agent_changes"] == out["agent_changes"] and log["card_id"] == "c1"


def test_card_tools_hidden_from_general_chat():
    from ai.orchestrator import _PAGE_ONLY_TOOLS, _PAGE_TOOLS
    assert set(t.STOCK_CARD_TOOL_NAMES) <= _PAGE_ONLY_TOOLS
    assert _PAGE_TOOLS["stock_card"] == list(t.STOCK_CARD_TOOL_NAMES)
