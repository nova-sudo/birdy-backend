"""Credits for external MCP requests: pricing, the out-of-credits gate and the post-call charge."""

import asyncio

import pytest

import credits
from fastmcp.server.dependencies import get_access_token
from ai.mcp import mcp
from ai.mcp.access import LIVE, READ
from ai.mcp.confirm import two_step
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import mcp_billing
from services.mcp_billing import charge_mcp_call, has_credits, mcp_charge_amount

OUT_MSG = "You're out of Birdy credits. Top up in Settings → Billing to keep using Birdy here."
USER = "alice@example.com"
DEFAULTS = {"mcp_base_fee": 0.2, "mcp_per_10k_chars": 1.0, "mcp_live_fee": 1.0, "mcp_max_per_call": 10.0}


@pytest.fixture(autouse=True)
def fresh_settings_cache():
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


async def _give(db, *, topup=100.0):
    """Seed a user with a purchased top-up balance (no plan allowance: the allowance is derived from the subscription)."""
    c, _ = credits._effective_credits({})
    c.update(topup_balance=topup)
    await db["users"].update_one({"user_id": USER}, {"$set": {"credits": c}}, upsert=True)


async def _enforce(db, on=True):
    await credits.set_credits_settings(db, enforce=on)


def test_amount_formula():
    assert mcp_charge_amount(DEFAULTS, 0, False) == 0.2
    assert mcp_charge_amount(DEFAULTS, 15000, False) == 1.7
    assert mcp_charge_amount(DEFAULTS, 10000, True) == 2.2
    assert mcp_charge_amount(DEFAULTS, 500000, False) == 10.0
    assert mcp_charge_amount({**DEFAULTS, "markup": 4.0}, 15000, False) == 1.7


@pytest.mark.asyncio
async def test_breakdown_sums_to_total_even_when_capped(mock_db):
    await _give(mock_db)
    r = await charge_mcp_call(mock_db, USER, tool="t", grant_id="g", chars=15000, live=True)
    assert r == {"request": 0.2, "data": 1.5, "live": 1.0, "total": 2.7}
    r = await charge_mcp_call(mock_db, USER, tool="t", grant_id="g", chars=500000, live=True)
    assert r["total"] == 10.0
    assert round(r["request"] + r["data"] + r["live"], 2) == 10.0
    assert r["data"] < 50 and r["request"] < 0.2
    row = await mock_db["ai_usage"].find_one({"credits": 10.0})
    assert (row["source"], row["feature"], row["tool"], row["grant_id"]) == ("mcp", "mcp_request", "t", "g")


@pytest.mark.asyncio
async def test_settings_roundtrip_and_validation(mock_db):
    s = await credits.get_credits_settings(mock_db, fresh=True)
    for k, v in DEFAULTS.items():
        assert s[k] == v
    s = await credits.set_credits_settings(mock_db, mcp_base_fee=0.5, mcp_max_per_call=3)
    assert s["mcp_base_fee"] == 0.5 and s["mcp_max_per_call"] == 3.0 and s["mcp_live_fee"] == 1.0
    with pytest.raises(ValueError):
        await credits.set_credits_settings(mock_db, mcp_base_fee=-1)
    with pytest.raises(ValueError):
        await credits.set_credits_settings(mock_db, mcp_live_fee=101)


@pytest.mark.asyncio
async def test_has_credits(mock_db):
    await _give(mock_db, topup=0)
    assert await has_credits(mock_db, USER)  # enforce off
    await _enforce(mock_db)
    assert not await has_credits(mock_db, USER)
    await _give(mock_db, topup=1)
    assert await has_credits(mock_db, USER)


@pytest.mark.asyncio
async def test_out_of_credits_blocks_when_enforced(mcp_call, mock_db):
    await _give(mock_db, topup=0)
    await _enforce(mock_db)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert res.is_error and OUT_MSG in res.text
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_not_blocked_when_enforce_off(mcp_call, mock_db):
    await _give(mock_db, topup=0)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error
    assert await mock_db["ai_usage"].count_documents({"source": "mcp"}) == 1


@pytest.mark.asyncio
async def test_external_call_is_charged(mcp_call, mock_db):
    await _give(mock_db)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error
    row = await mock_db["ai_usage"].find_one({"source": "mcp"})
    assert row["tool"] == "get_alerts" and row["grant_id"] and row["credits"] >= 0.2
    user = await mock_db["users"].find_one({"user_id": USER})
    assert user["credits"]["topup_balance"] == round(100 - row["credits"], 4)


@pytest.mark.asyncio
async def test_internal_call_not_charged(mcp_call, mock_db):
    await _give(mock_db, topup=0)
    await _enforce(mock_db)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="access")
    assert not res.is_error
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_view_only_refusal_is_free(mcp_call, mock_db):
    await _give(mock_db)
    res = await mcp_call("create_alert", {"name": "x", "metric": "spend", "operator": "gt", "value": 1},
                         user=USER, scope="birdy.read", token_type="mcp")
    assert res.is_error
    assert await mock_db["ai_usage"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_charge_failure_still_returns_result(mcp_call, mock_db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("mongo down")
    monkeypatch.setattr(mcp_billing, "charge_mcp_call", boom)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error


@pytest.mark.asyncio
async def test_charge_timeout_still_returns_result(mcp_call, mock_db, monkeypatch):
    async def slow(*a, **k):
        await asyncio.sleep(5)
    monkeypatch.setattr(mcp_billing, "charge_mcp_call", slow)
    monkeypatch.setattr("ai.mcp.access._CHARGE_TIMEOUT_SECONDS", 0.05)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert not res.is_error


@pytest.mark.asyncio
async def test_parallel_calls_near_zero_then_blocked(mcp_call, mock_db):
    await _give(mock_db, topup=1)  # one call's worth, five racing calls
    await _enforce(mock_db)
    results = await asyncio.gather(*[mcp_call("get_alerts", {}, user=USER, token_type="mcp") for _ in range(5)])
    assert all(not r.is_error or OUT_MSG in r.text for r in results)  # never a crash
    nxt = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    assert nxt.is_error and OUT_MSG in nxt.text
    user = await mock_db["users"].find_one({"user_id": USER})
    assert user["credits"]["topup_balance"] >= 0


@pytest.fixture
def temp_tools():
    """Throwaway tools: a live two-step tool (state can be flipped) and a read tool returning an error dict."""
    state = {"v": 1}

    @mcp.tool(name="zz_live_two_step", **LIVE)
    async def zz_live_two_step(confirm_code: str | None = None) -> dict:
        """temp"""
        user_id = get_access_token().claims["sub"]
        db = get_db(get_shared_mongo_client())

        async def preview():
            return {"action": "go"}

        async def execute():
            return {"ok": True, "done": True}

        return await two_step(db, user_id, "zz_live_two_step", {}, confirm_code, preview, execute, state=dict(state))

    @mcp.tool(name="zz_error_result", **READ)
    async def zz_error_result(kind: str = "error") -> dict:
        """temp"""
        return {"error": "nope"} if kind == "error" else {"ok": False, "detail": "x"}

    yield state
    mcp.remove_tool("zz_live_two_step")
    mcp.remove_tool("zz_error_result")


async def _charges(db):
    return await db["ai_usage"].find({"source": "mcp"}).to_list(None)


@pytest.mark.asyncio
async def test_error_result_costs_nothing(mcp_call, mock_db, temp_tools):
    await _give(mock_db)
    await mcp_call("zz_error_result", {"kind": "error"}, user=USER, token_type="mcp")
    await mcp_call("zz_error_result", {"kind": "ok_false"}, user=USER, token_type="mcp")
    assert await _charges(mock_db) == []


@pytest.mark.asyncio
async def test_preview_charges_base_only_and_live_execute_adds_live_fee(mcp_call, mock_db, temp_tools):
    await _give(mock_db)
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope="birdy.read birdy.write", token_type="mcp")
    code = __import__("json").loads(pre.text)["confirm_code"]
    rows = await _charges(mock_db)
    assert len(rows) == 1 and rows[0]["credits"] < 1.0  # preview: no live fee
    done = await mcp_call("zz_live_two_step", {"confirm_code": code}, user=USER,
                          scope="birdy.read birdy.write", token_type="mcp")
    assert not done.is_error
    rows = await _charges(mock_db)
    assert len(rows) == 2
    assert max(r["credits"] for r in rows) >= 1.2  # base 0.2 + live 1.0 + data


@pytest.mark.asyncio
async def test_bad_code_costs_nothing(mcp_call, mock_db, temp_tools):
    await _give(mock_db)
    res = await mcp_call("zz_live_two_step", {"confirm_code": "wrong"}, user=USER,
                         scope="birdy.read birdy.write", token_type="mcp")
    assert "error" in res.text
    assert await _charges(mock_db) == []


@pytest.mark.asyncio
async def test_state_changed_costs_nothing(mcp_call, mock_db, temp_tools):
    await _give(mock_db)
    scope = "birdy.read birdy.write"
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope=scope, token_type="mcp")
    code = __import__("json").loads(pre.text)["confirm_code"]
    before = len(await _charges(mock_db))
    temp_tools["v"] = 2  # the world moved on after the preview
    res = await mcp_call("zz_live_two_step", {"confirm_code": code}, user=USER, scope=scope, token_type="mcp")
    assert "error" in res.text
    assert len(await _charges(mock_db)) == before
