"""Dashboard suggestion MCP tools (ai/mcp/suggestions_mcp.py)."""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ai.agents.defaults import DENY_REASONS
from ai.mcp import suggestions_mcp as t
from ai.suggestions import store as sug_store

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio

TARGETS = [{"object_id": "ad1", "object_type": "ad", "name": "Spring Promo"},
           {"object_id": "ad2", "object_type": "ad", "name": "Old Video"}]


async def _sug(db, sid="s1", user=ALICE, status="open", **kw):
    doc = {"_id": sid, "id": sid, "user_id": user, "status": status, "client_name": "Acme", "title": "Pause 2 ads",
           "description": "Two ads are over target", "severity": "high", "created_at": datetime.utcnow(),
           "stats": [{"label": "CPL", "value": "$42"}],
           "action": {"type": "pause_ads", "targets": TARGETS, "params": {}}, **kw}
    await db[sug_store.SUGGESTIONS].insert_one(doc)
    return doc


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


def _svc(monkeypatch, name, ret):
    m = AsyncMock(return_value=ret)
    monkeypatch.setattr(f"ai.suggestions.actions.{name}", m)
    return m


async def test_tool_tags_match_the_spec():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    assert tools["get_suggestions"].tags == {"read"}
    assert tools["deny_suggestion"].tags == {"write"}
    for name in ("apply_suggestion", "undo_suggestion"):
        assert tools[name].tags == {"write", "live"}
        assert "Show the preview to the user and only call again with confirm_code after they say yes." in tools[name].description
    for r in DENY_REASONS:
        assert r in tools["deny_suggestion"].description


async def test_get_suggestions_lists_by_status(mock_db, alice):
    await _sug(mock_db, "open1")
    await _sug(mock_db, "app1", status="applied", applied_at=datetime.utcnow())
    await _sug(mock_db, "bobs", user=BOB)
    out = await t.get_suggestions.fn()
    assert [s["id"] for s in out] == ["open1"] and out[0]["has_action"] is True
    out = await t.get_suggestions.fn(status="applied")
    assert [s["id"] for s in out] == ["app1"]
    assert {s["id"] for s in await t.get_suggestions.fn(status="all")} == {"open1", "app1"}


async def test_apply_suggestion_two_step(mock_db, alice, monkeypatch):
    await _sug(mock_db)
    svc = _svc(monkeypatch, "apply_suggestion", {"ok": True, "outcome": "applied", "succeeded": ["ad1", "ad2"], "failed": []})
    first = await t.apply_suggestion.fn(suggestion_id="s1")
    assert first["needs_confirmation"] and first["confirm_code"]
    p = first["preview"]
    assert p["action"] == "pause_ads" and p["title"] == "Pause 2 ads"
    assert [x["name"] for x in p["touches"]] == ["Spring Promo", "Old Video"]
    assert p["stats"] == [{"label": "CPL", "value": "$42"}]
    svc.assert_not_awaited()
    out = await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=first["confirm_code"])
    assert out["ok"] is True and out["succeeded"] == ["ad1", "ad2"]
    svc.assert_awaited_once()
    assert svc.await_args.kwargs["actor"] == sug_store.ACTOR_USER and svc.await_args.kwargs["source"] == "mcp"
    assert svc.await_args.args[2:4] == (ALICE, "s1")
    again = await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=first["confirm_code"])
    assert "Nothing was changed" in again["error"]
    assert svc.await_count == 1


async def test_apply_code_replayed_for_other_suggestion_is_refused(mock_db, alice, monkeypatch):
    await _sug(mock_db, "s1")
    await _sug(mock_db, "s2")
    svc = _svc(monkeypatch, "apply_suggestion", {"ok": True, "outcome": "applied"})
    first = await t.apply_suggestion.fn(suggestion_id="s1")
    out = await t.apply_suggestion.fn(suggestion_id="s2", confirm_code=first["confirm_code"])
    assert "Nothing was changed" in out["error"]
    svc.assert_not_awaited()


async def test_apply_outcomes_become_error_dicts(mock_db, alice, monkeypatch):
    await _sug(mock_db)
    code = (await t.apply_suggestion.fn(suggestion_id="s1"))["confirm_code"]
    _svc(monkeypatch, "apply_suggestion", {"ok": False, "outcome": "failed", "detail": "Meta said no"})
    out = await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=code)
    assert out == {"ok": False, "outcome": "failed", "error": "Meta said no"}
    code = (await t.apply_suggestion.fn(suggestion_id="s1"))["confirm_code"]
    _svc(monkeypatch, "apply_suggestion", {"ok": False, "outcome": "blocked", "blocked_reason": {"code": "x", "message": "Daily cap hit"}, "detail": "Daily cap hit"})
    out = await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=code)
    assert out["ok"] is False and out["error"] == "Daily cap hit"


async def test_closed_suggestion_returns_conflict_message(mock_db, alice, monkeypatch):
    await _sug(mock_db, "gone", status="dismissed")
    svc = _svc(monkeypatch, "apply_suggestion", {})
    out = await t.apply_suggestion.fn(suggestion_id="gone")
    assert out["ok"] is False and out["outcome"] == "closed" and "already closed" in out["error"]
    assert "confirm_code" not in out and await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    svc.assert_not_awaited()
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.apply_suggestion.fn(suggestion_id="nope")
    # closed between preview and confirm: the service still says so
    await _sug(mock_db, "s1")
    code = (await t.apply_suggestion.fn(suggestion_id="s1"))["confirm_code"]
    _svc(monkeypatch, "apply_suggestion", {"ok": False, "outcome": "closed", "detail": "This item was already closed", "status": "dismissed"})
    out = await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=code)
    assert out["outcome"] == "closed" and out["error"] == "This item was already closed"


async def test_deny_rejects_unknown_reason(mock_db, alice, monkeypatch):
    await _sug(mock_db)
    svc = _svc(monkeypatch, "dismiss_suggestion", {"ok": True, "outcome": "dismissed"})
    with pytest.raises(ToolError, match="still_learning"):
        await t.deny_suggestion.fn(suggestion_id="s1", reason_code="because")
    svc.assert_not_awaited()


async def test_deny_dismisses_with_reason_and_source(mock_db, alice, monkeypatch):
    await _sug(mock_db)
    svc = _svc(monkeypatch, "dismiss_suggestion", {"ok": True, "outcome": "dismissed", "memory": {"_id": "m", "text": "Keep on", "kind": "exception", "filter": {}}})
    out = await t.deny_suggestion.fn(suggestion_id="s1", reason_code="client_wants_on", note="Client asked")
    assert out["ok"] is True and out["memory"]["text"] == "Keep on"
    kw = svc.await_args.kwargs
    assert kw["reason_code"] == "client_wants_on" and kw["note"] == "Client asked" and kw["source"] == "mcp"
    assert kw["actor"] == sug_store.ACTOR_USER


async def test_deny_busy_and_closed(mock_db, alice, monkeypatch):
    await _sug(mock_db)
    await _sug(mock_db, "zz")
    _svc(monkeypatch, "dismiss_suggestion", {"ok": False, "outcome": "busy"})
    out = await t.deny_suggestion.fn(suggestion_id="s1", reason_code="other")
    assert out["ok"] is False and "Applying right now" in out["error"]
    _svc(monkeypatch, "dismiss_suggestion", {"ok": False, "outcome": "already", "status": "applied", "detail": "Already applied — undo it instead"})
    out = await t.deny_suggestion.fn(suggestion_id="s1", reason_code="other")
    assert out["ok"] is False and "undo it instead" in out["error"]
    _svc(monkeypatch, "dismiss_suggestion", {"ok": False, "outcome": "not_found"})
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.deny_suggestion.fn(suggestion_id="zz", reason_code="other")
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.deny_suggestion.fn(suggestion_id="missing", reason_code="other")


async def test_undo_suggestion_two_step(mock_db, alice, monkeypatch):
    await _sug(mock_db, status="applied", applied_at=datetime.utcnow(), applied_action_type="pause_ads", applied_targets=TARGETS)
    svc = _svc(monkeypatch, "undo_suggestion", {"ok": True, "outcome": "undone", "succeeded": ["ad1", "ad2"]})
    first = await t.undo_suggestion.fn(suggestion_id="s1")
    assert first["needs_confirmation"]
    assert first["preview"]["action"] == "enable_ads" and [x["name"] for x in first["preview"]["touches"]] == ["Spring Promo", "Old Video"]
    svc.assert_not_awaited()
    out = await t.undo_suggestion.fn(suggestion_id="s1", confirm_code=first["confirm_code"])
    assert out["ok"] is True and out["outcome"] == "undone"
    assert svc.await_args.kwargs["source"] == "mcp" and svc.await_args.kwargs["actor"] == sug_store.ACTOR_USER
    assert "Nothing was changed" in (await t.undo_suggestion.fn(suggestion_id="s1", confirm_code=first["confirm_code"]))["error"]


async def test_undo_code_replayed_for_other_suggestion_is_refused(mock_db, alice, monkeypatch):
    for sid in ("s1", "s2"):
        await _sug(mock_db, sid, status="applied", applied_at=datetime.utcnow(), applied_action_type="pause_ads", applied_targets=TARGETS)
    svc = _svc(monkeypatch, "undo_suggestion", {"ok": True, "outcome": "undone"})
    first = await t.undo_suggestion.fn(suggestion_id="s1")
    out = await t.undo_suggestion.fn(suggestion_id="s2", confirm_code=first["confirm_code"])
    assert "Nothing was changed" in out["error"]
    svc.assert_not_awaited()


async def test_undo_not_undoable_and_not_applied_get_no_code(mock_db, alice, monkeypatch):
    svc = _svc(monkeypatch, "undo_suggestion", {})
    await _sug(mock_db, "cr", status="applied", applied_at=datetime.utcnow(), applied_action_type="scale_creative", applied_targets=[])
    out = await t.undo_suggestion.fn(suggestion_id="cr")
    assert out["ok"] is False and out["outcome"] == "not_undoable" and out["error"] == "Nothing to undo" and "confirm_code" not in out
    await _sug(mock_db, "op")  # open: nothing is applied
    out = await t.undo_suggestion.fn(suggestion_id="op")
    assert out["ok"] is True and out["outcome"] == "noop" and "confirm_code" not in out
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.undo_suggestion.fn(suggestion_id="zz")
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0
    svc.assert_not_awaited()
    # service refusals at confirm time still come back as errors
    await _sug(mock_db, "s9", status="applied", applied_at=datetime.utcnow(), applied_action_type="pause_ads", applied_targets=TARGETS)
    code = (await t.undo_suggestion.fn(suggestion_id="s9"))["confirm_code"]
    _svc(monkeypatch, "undo_suggestion", {"ok": False, "outcome": "failed", "detail": "Meta down"})
    assert (await t.undo_suggestion.fn(suggestion_id="s9", confirm_code=code))["error"] == "Meta down"


async def test_other_users_suggestion_is_unreachable_and_nothing_changes(mock_db, alice, set_current_user, monkeypatch):
    await _sug(mock_db, "s1")
    await _sug(mock_db, "a1", status="applied", applied_at=datetime.utcnow(), applied_action_type="pause_ads", applied_targets=TARGETS)
    apply_, dismiss, undo = (_svc(monkeypatch, n, {"ok": True}) for n in ("apply_suggestion", "dismiss_suggestion", "undo_suggestion"))
    set_current_user(BOB)
    assert await t.get_suggestions.fn(status="all") == []
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.deny_suggestion.fn(suggestion_id="s1", reason_code="other")
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.apply_suggestion.fn(suggestion_id="s1")
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.undo_suggestion.fn(suggestion_id="a1")
    # an alice code cannot be spent by bob
    set_current_user(ALICE)
    code = (await t.apply_suggestion.fn(suggestion_id="s1"))["confirm_code"]
    set_current_user(BOB)
    with pytest.raises(ToolError, match="Suggestion not found"):
        await t.apply_suggestion.fn(suggestion_id="s1", confirm_code=code)
    from ai.mcp import confirm
    assert await confirm.consume_code(mock_db, BOB, "apply_suggestion", {"suggestion_id": "s1"}, code) is False
    for m in (apply_, dismiss, undo):
        m.assert_not_awaited()
    assert (await mock_db[sug_store.SUGGESTIONS].find_one({"_id": "s1"}))["status"] == "open"


async def test_read_scope_cannot_deny_or_apply(mcp_call, mock_db, alice):
    await _sug(mock_db)
    for tool, args in [("deny_suggestion", {"suggestion_id": "s1", "reason_code": "other"}),
                       ("apply_suggestion", {"suggestion_id": "s1"}), ("undo_suggestion", {"suggestion_id": "s1"})]:
        res = await mcp_call(tool, args, scope="birdy.read", token_type="mcp")
        assert res.is_error and "view-only" in res.text, tool
    assert (await mock_db[sug_store.SUGGESTIONS].find_one({"_id": "s1"}))["status"] == "open"
    res = await mcp_call("get_suggestions", {}, scope="birdy.read", token_type="mcp")
    assert not res.is_error
    res = await mcp_call("apply_suggestion", {"suggestion_id": "s1"}, scope="birdy.write", token_type="mcp")
    assert not res.is_error and "confirm_code" in res.text


async def test_apply_flag_only_item_previews_then_acknowledges(mock_db, alice):
    await _sug(mock_db, "flag", action=None)
    first = await t.apply_suggestion.fn(suggestion_id="flag")
    assert first["needs_confirmation"] and first["preview"]["summary"] == "Acknowledge: nothing changes in Meta"
    assert first["preview"]["touches"] == []
    out = await t.apply_suggestion.fn(suggestion_id="flag", confirm_code=first["confirm_code"])
    assert out["ok"] is True and out["succeeded"] == [] and out["failed"] == []
    doc = await mock_db[sug_store.SUGGESTIONS].find_one({"_id": "flag"})
    assert doc["status"] == "applied" and not doc.get("applied_targets")


async def test_undo_unknown_applied_type_is_refused_without_code(mock_db, alice):
    await _sug(mock_db, "u", status="applied", applied_at=datetime.utcnow(), applied_action_type="mystery", applied_targets=TARGETS)
    out = await t.undo_suggestion.fn(suggestion_id="u")
    assert out["ok"] is False and out["outcome"] == "not_undoable" and "confirm_code" not in out
    await _sug(mock_db, "e", status="applied", applied_at=datetime.utcnow(), applied_action_type="enable_ads", applied_targets=TARGETS)
    assert (await t.undo_suggestion.fn(suggestion_id="e"))["preview"]["action"] == "pause_ads"
