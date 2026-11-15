"""Call outcome reporting: two_step, gate refusals and result measuring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai.mcp import access
from ai.mcp.outcome import current_activity_id, get_outcome, is_error_result, reset_outcome, response_chars, start_activity
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
    assert get_outcome() == {"kind": "live_executed", "confirmed_after": None}


async def test_execute_confirms_after_the_preview_activity(mock_db):
    start_activity()
    preview_id = current_activity_id()
    code = await _issue(mock_db)
    start_activity()  # the execute call gets its own id
    assert current_activity_id() != preview_id
    await two_step(mock_db, "alice", "t", ARGS, code, AsyncMock(), AsyncMock(return_value={"ok": True}))
    assert get_outcome()["confirmed_after"] == preview_id


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


async def test_outcome_set_in_a_tool_is_visible_to_the_middleware(mcp_call, mock_db, monkeypatch):
    """Real round trip: a tool runs two_step, and the middleware sees its outcome after call_next."""
    from ai.mcp import mcp

    seen = {}
    real = access.BirdyAccessMiddleware.on_call_tool

    async def spy(self, context, call_next):
        result = await real(self, context, call_next)
        seen["outcome"] = get_outcome()
        seen["activity_id"] = current_activity_id()
        return result

    monkeypatch.setattr(access.BirdyAccessMiddleware, "on_call_tool", spy)

    @mcp.tool(name="zz_outcome_probe")
    async def probe() -> dict:
        return await two_step(mock_db, "alice", "zz_outcome_probe", {}, None, AsyncMock(return_value={}), AsyncMock())

    try:
        res = await mcp_call("zz_outcome_probe", {}, scope="birdy.write", token_type="mcp")
    finally:
        mcp.remove_tool("zz_outcome_probe")
    assert not res.is_error
    assert seen["outcome"] == {"kind": "preview"}
    # The preview stamped the middleware's pre-allocated id on its confirm code.
    doc = await mock_db["mcp_confirm_codes"].find_one({"tool": "zz_outcome_probe"})
    assert doc["activity_id"] == seen["activity_id"] and seen["activity_id"]
