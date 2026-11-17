"""Final-review fixes: live fee only for LIVE-tagged tools, refusals rate-limited, index, created_at, code masking."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastmcp.server.dependencies import get_access_token

import credits
from ai.mcp import mcp
from ai.mcp.access import MCP_RATE_LIMIT_PER_MINUTE, WRITE
from ai.mcp.confirm import two_step
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import mcp_activity

USER = "alice@example.com"
RW = "birdy.read birdy.write"


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


@pytest.fixture
def write_two_step():
    @mcp.tool(name="zz_write_two_step", **WRITE)
    async def zz_write_two_step(confirm_code: str | None = None) -> dict:
        """temp"""
        user_id = get_access_token().claims["sub"]
        db = get_db(get_shared_mongo_client())

        async def preview():
            return {"action": "go"}

        async def execute():
            return {"ok": True}

        return await two_step(db, user_id, "zz_write_two_step", {}, confirm_code, preview, execute)

    yield
    mcp.remove_tool("zz_write_two_step")


async def _give(db, topup=100.0):
    c, _ = credits._effective_credits({})
    c.update(topup_balance=topup)
    await db["users"].update_one({"user_id": USER}, {"$set": {"credits": c}}, upsert=True)


@pytest.mark.asyncio
async def test_non_live_two_step_execute_is_a_normal_charged_change(mcp_call, mock_db, write_two_step, monkeypatch):
    sent = []
    monkeypatch.setattr("services.posthog_events.capture", lambda uid, ev, props=None, **k: sent.append(ev))
    await _give(mock_db)
    pre = await mcp_call("zz_write_two_step", {}, user=USER, scope=RW, token_type="mcp")
    code = json.loads(pre.text)["confirm_code"]
    done = await mcp_call("zz_write_two_step", {"confirm_code": code}, user=USER, scope=RW, token_type="mcp")
    assert not done.is_error
    rows = await mock_db["mcp_activity"].find({}).sort("created_at", 1).to_list(None)
    preview_row, exec_row = rows
    assert exec_row["kind"] == "change" and exec_row["credits"]["live"] == 0
    assert exec_row["confirmed_after"] == preview_row["id"]
    assert "mcp_live_confirmed" not in sent
    charges = await mock_db["ai_usage"].find({"source": "mcp"}).to_list(None)
    assert len(charges) == 2 and max(c["credits"] for c in charges) < 1.0  # no live fee


@pytest.mark.asyncio
async def test_refusals_are_rate_limited_too(mcp_call, mock_db, monkeypatch):
    monkeypatch.setattr("ai.mcp.access._minute", lambda: 28_333_333)
    await _give(mock_db, topup=0)
    await credits.set_credits_settings(mock_db, enforce=True)
    for _ in range(MCP_RATE_LIMIT_PER_MINUTE + 10):
        await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    rows = await mock_db["mcp_activity"].find({}).to_list(None)
    assert len(rows) == MCP_RATE_LIMIT_PER_MINUTE + 1
    assert sum(1 for r in rows if r["refusal_reason"] == "out_of_credits") == MCP_RATE_LIMIT_PER_MINUTE
    [limited] = [r for r in rows if r["refusal_reason"] == "rate_limit"]
    assert limited["count"] == 10


@pytest.mark.asyncio
async def test_activity_index_covers_list_sort(mock_mongo_client):
    await mcp_activity.create_mcp_activity_indexes(mock_mongo_client)
    info = await mock_mongo_client[mcp_activity.DB_NAME][mcp_activity.COLLECTION].index_information()
    assert [("user_id", 1), ("created_at", -1), ("id", -1)] in [v["key"] for v in info.values()]


@pytest.mark.asyncio
async def test_created_at_is_call_start_time(mock_db):
    start = datetime(2026, 11, 15, 12, 0, 59, tzinfo=timezone.utc)
    await mcp_activity.record_call(
        mock_db, user_id=USER, grant_id="g", app_name="Claude", app_kind="claude", access="read", tool="get_alerts",
        kind="lookup", refusal_reason=None, args={}, result={}, result_status="ok", credits=None, chars=0,
        duration_ms=1, confirmed_after=None, created_at=start)
    await mcp_activity.record_rate_limited(mock_db, USER, "g", "Claude", created_at=start + timedelta(seconds=30))
    rows = await mock_db["mcp_activity"].find({}).to_list(None)
    call = next(r for r in rows if r["kind"] == "lookup")
    limited = next(r for r in rows if r["kind"] == "refused")
    assert call["created_at"].replace(tzinfo=timezone.utc) == start
    assert limited["created_at"].replace(tzinfo=timezone.utc) == start + timedelta(seconds=30)
    assert limited["minute"].replace(tzinfo=timezone.utc) == datetime(2026, 11, 15, 12, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [
    '{"preview": {}, "confirm_code": "abc123XYZ", "next": "x"}',
    "confirm_code: abc123XYZ",
    "confirm_code='abc123XYZ'",
])
def test_confirm_code_masked_in_text_excerpt(text):
    out = mcp_activity._excerpt(text)
    assert "abc123XYZ" not in out and "confirm_code" in out and "••••••" in out
