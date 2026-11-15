"""Call outcome reporting: two_step, gate refusals and result measuring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.mcp import access
from ai.mcp.outcome import get_outcome, is_error_result, reset_outcome, response_chars
from ai.mcp.confirm import two_step

ARGS = {"card_id": "c1"}


@pytest.fixture(autouse=True)
def _clean_outcome():
    reset_outcome()
    yield
    reset_outcome()


async def _issue(db):
    out = await two_step(db, "alice", "t", ARGS, None, AsyncMock(return_value={}), AsyncMock())
    return out["confirm_code"]


async def test_two_step_sets_preview(mock_db):
    await two_step(mock_db, "alice", "t", ARGS, None, AsyncMock(return_value={}), AsyncMock())
    assert get_outcome() == {"kind": "preview"}


async def test_two_step_sets_live_executed(mock_db):
    code = await _issue(mock_db)
    reset_outcome()
    await two_step(mock_db, "alice", "t", ARGS, code, AsyncMock(), AsyncMock(return_value={"ok": True}))
    assert get_outcome() == {"kind": "live_executed", "activity_id": None}


async def test_live_executed_carries_activity_id(mock_db):
    from ai.mcp.confirm import issue_code

    code = await issue_code(mock_db, "alice", "t", ARGS, activity_id="act1")
    await two_step(mock_db, "alice", "t", ARGS, code, AsyncMock(), AsyncMock(return_value={"ok": True}))
    assert get_outcome()["activity_id"] == "act1"


async def test_failed_execute_leaves_no_outcome(mock_db):
    code = await _issue(mock_db)
    reset_outcome()
    with pytest.raises(RuntimeError):
        await two_step(mock_db, "alice", "t", ARGS, code, AsyncMock(), AsyncMock(side_effect=RuntimeError))
    assert get_outcome() is None


async def test_bad_code_sets_bad_code(mock_db):
    await two_step(mock_db, "alice", "t", ARGS, "nope", AsyncMock(), AsyncMock())
    assert get_outcome() == {"kind": "bad_code"}


async def test_state_change_sets_state_changed(mock_db):
    out = await two_step(mock_db, "alice", "t", ARGS, None, AsyncMock(return_value={}), AsyncMock(), state="a")
    await two_step(mock_db, "alice", "t", ARGS, out["confirm_code"], AsyncMock(), AsyncMock(), state="b")
    assert get_outcome() == {"kind": "state_changed"}


def test_is_error_result():
    assert is_error_result({"error": "x"}) is True
    assert is_error_result({"ok": False}) is True
    assert is_error_result({"ok": True}) is False
    assert is_error_result([]) is False
    assert is_error_result({"errors": []}) is False


def test_response_chars_prefers_structured():
    result = SimpleNamespace(
        structured_content={"a": 1},
        content=[SimpleNamespace(text="x" * 500)],
    )
    assert response_chars(result) == len('{"a": 1}')
    assert response_chars(SimpleNamespace(structured_content=None, content=[SimpleNamespace(text="abc"), SimpleNamespace(text="de")])) == 5


async def test_view_only_refusal_sets_outcome(monkeypatch):
    seen = {}
    monkeypatch.setattr(access, "get_access_token", lambda: SimpleNamespace(claims={"sub": "a", "type": "mcp", "scope": "birdy.read"}))
    tool = SimpleNamespace(tags={"write"})
    fastmcp = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
    context = SimpleNamespace(fastmcp_context=SimpleNamespace(fastmcp=fastmcp), message=SimpleNamespace(name="t"))

    async def call_next(_):
        seen["called"] = True

    from fastmcp.exceptions import ToolError

    # Stale outcome from a previous call must be cleared first.
    from ai.mcp.outcome import set_outcome
    set_outcome("preview")
    with pytest.raises(ToolError):
        await access.BirdyAccessMiddleware().on_call_tool(context, call_next)
    assert get_outcome() == {"kind": "refused", "reason": "view_only"}
    assert "called" not in seen
