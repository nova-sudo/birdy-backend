"""Server-enforced confirm codes for tools that change live ads."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from ai.mcp import confirm
from ai.mcp.confirm import consume_code, issue_code, two_step

ARGS = {"card_id": "c1", "line_ids": ["a"]}


@pytest.fixture
def freeze_time_plus_601s(monkeypatch):
    """Call the returned function to jump the confirm clock 601s ahead."""
    real = confirm._now

    def jump():
        monkeypatch.setattr(confirm, "_now", lambda: real() + timedelta(seconds=601))

    return jump


async def test_first_call_returns_preview_and_code_without_executing(mock_db):
    preview = AsyncMock(return_value={"will": "pause 1 ad"})
    execute = AsyncMock(return_value={"ok": True})
    out = await two_step(mock_db, "alice", "apply_card", ARGS, None, preview, execute)
    assert out["needs_confirmation"] is True
    assert out["preview"] == {"will": "pause 1 ad"}
    assert out["confirm_code"]
    assert out["expires_in_seconds"] == 600
    assert "confirm_code" in out["next"]
    execute.assert_not_awaited()


async def test_valid_code_executes_once(mock_db):
    preview = AsyncMock(return_value={})
    execute = AsyncMock(return_value={"ok": True})
    first = await two_step(mock_db, "alice", "apply_card", ARGS, None, preview, execute)
    code = first["confirm_code"]
    assert await two_step(mock_db, "alice", "apply_card", ARGS, code, preview, execute) == {"ok": True}
    again = await two_step(mock_db, "alice", "apply_card", ARGS, code, preview, execute)
    assert "invalid, expired" in again["error"]
    assert execute.await_count == 1


async def test_confirm_code_key_is_ignored_in_args_hash(mock_db):
    code = await issue_code(mock_db, "alice", "apply_card", ARGS)
    assert await consume_code(mock_db, "alice", "apply_card", {**ARGS, "confirm_code": code}, code)


async def test_code_with_different_args_is_refused(mock_db):
    code = await issue_code(mock_db, "alice", "apply_card", {"card_id": "c1", "line_ids": ["a"]})
    assert not await consume_code(mock_db, "alice", "apply_card", {"card_id": "c1", "line_ids": ["a", "b"]}, code)


async def test_refused_replay_with_different_args_executes_nothing(mock_db):
    preview = AsyncMock(return_value={})
    execute = AsyncMock(return_value={"ok": True})
    first = await two_step(mock_db, "alice", "apply_card", ARGS, None, preview, execute)
    out = await two_step(mock_db, "alice", "apply_card", {**ARGS, "line_ids": ["a", "b"]}, first["confirm_code"], preview, execute)
    assert "error" in out
    execute.assert_not_awaited()


async def test_code_for_other_user_or_tool_is_refused(mock_db):
    code = await issue_code(mock_db, "alice", "apply_card", ARGS)
    assert not await consume_code(mock_db, "bob", "apply_card", ARGS, code)
    assert not await consume_code(mock_db, "alice", "other_tool", ARGS, code)
    assert await consume_code(mock_db, "alice", "apply_card", ARGS, code)


async def test_expired_code_is_refused(mock_db, freeze_time_plus_601s):
    code = await issue_code(mock_db, "alice", "apply_card", ARGS)
    freeze_time_plus_601s()
    assert not await consume_code(mock_db, "alice", "apply_card", ARGS, code)
