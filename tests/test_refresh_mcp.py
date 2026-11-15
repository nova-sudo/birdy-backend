"""Data refresh MCP tools (ai/mcp/refresh_mcp.py) and the services/client_refresh.py they share with the route."""

import asyncio
from datetime import datetime, timedelta

import pytest
from fastmcp.exceptions import ToolError

from ai.mcp import refresh_mcp as t
from services import client_refresh

ALICE, BOB = "alice@example.com", "bob@example.com"

pytestmark = pytest.mark.asyncio


async def _group(db, gid, name, user=ALICE, **kw):
    doc = {"id": gid, "user_id": user, "name": name, **kw}
    await db["client_groups"].insert_one(doc)
    return doc


async def _both(db, gid="g1", name="Acme Dental", user=ALICE, **kw):
    return await _group(db, gid, name, user, meta_ad_account_id="act_1", ad_account_currency="USD",
                        ghl_location_id="loc1", **kw)


@pytest.fixture
def alice(set_current_user):
    set_current_user(ALICE)


@pytest.fixture
def ghl_calls(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw)

    monkeypatch.setattr(client_refresh, "fetch_and_cache_ghl_data_optimized", fake)
    return calls


async def _drain():
    await asyncio.gather(*list(client_refresh._tasks))


async def test_tool_tags_and_docstrings():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    assert tools["refresh_client_data"].tags == {"write"}
    assert tools["refresh_status"].tags == {"read"}
    for name in ("refresh_client_data", "refresh_status"):
        assert len(tools[name].description) > 60
    assert "refresh_status" in tools["refresh_client_data"].description
    assert "minutes" in tools["refresh_client_data"].description


async def test_refresh_starts_only_requested_integration(mock_db, alice, ghl_calls):
    await _both(mock_db)
    out = await t.refresh_client_data.fn(client="Acme", integration="meta")
    await _drain()
    assert out == {"client": {"id": "g1", "name": "Acme Dental"}, "started": ["meta"], "already_running": [],
                   "check_with": "refresh_status"}
    jobs = await mock_db["meta_refresh_jobs"].find({"group_id": "g1"}).to_list(None)
    assert len(jobs) == 1 and jobs[0]["status"] == "pending" and jobs[0]["user_id"] == ALICE
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["meta_refresh_status"] == "running"
    assert "ghl_refresh_status" not in g
    assert ghl_calls == []


async def test_refresh_ghl_only_runs_the_ghl_load(mock_db, alice, ghl_calls):
    await _both(mock_db)
    out = await t.refresh_client_data.fn(client="g1", integration="ghl")
    assert out["started"] == ["ghl"]
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["ghl_refresh_status"] == "running" and g["_ghl_claimed_at"]
    await _drain()
    assert await mock_db["meta_refresh_jobs"].count_documents({}) == 0
    assert len(ghl_calls) == 1 and ghl_calls[0]["ghl_location_id"] == "loc1" and ghl_calls[0]["user_id"] == ALICE
    assert ghl_calls[0]["is_initial_load"] is True
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["ghl_refresh_status"] == "complete" and g["last_ghl_refresh"] and "_ghl_claimed_at" not in g


async def test_refresh_all_starts_both(mock_db, alice, ghl_calls):
    await _both(mock_db)
    out = await t.refresh_client_data.fn(client="Acme")
    await _drain()
    assert out["started"] == ["meta", "ghl"]
    assert await mock_db["meta_refresh_jobs"].count_documents({"group_id": "g1"}) == 1
    assert len(ghl_calls) == 1


async def test_refresh_all_skips_unconnected_and_says_so(mock_db, alice, ghl_calls):
    await _group(mock_db, "g1", "Acme", meta_ad_account_id="act_1", ad_account_currency="USD")
    out = await t.refresh_client_data.fn(client="Acme")
    assert out["started"] == ["meta"] and out["not_connected"] == ["ghl"]
    assert ghl_calls == []


async def test_unconnected_integration_is_a_clear_error(mock_db, alice, ghl_calls):
    await _group(mock_db, "g1", "Acme", meta_ad_account_id="act_1")
    with pytest.raises(ToolError, match="GoHighLevel is not connected"):
        await t.refresh_client_data.fn(client="Acme", integration="ghl")
    await _group(mock_db, "g2", "Birch")
    with pytest.raises(ToolError, match="Meta is not connected"):
        await t.refresh_client_data.fn(client="Birch", integration="meta")
    with pytest.raises(ToolError, match="no connected integrations"):
        await t.refresh_client_data.fn(client="Birch")
    assert await mock_db["meta_refresh_jobs"].count_documents({}) == 0
    assert ghl_calls == []
    assert "ghl_refresh_status" not in await mock_db["client_groups"].find_one({"id": "g1"})


async def test_already_running_is_not_doubled(mock_db, alice, ghl_calls):
    await _both(mock_db)
    first = await t.refresh_client_data.fn(client="Acme")
    again = await t.refresh_client_data.fn(client="Acme")
    await _drain()
    assert first["started"] == ["meta", "ghl"]
    assert again["started"] == [] and again["already_running"] == ["meta", "ghl"]
    assert await mock_db["meta_refresh_jobs"].count_documents({"group_id": "g1"}) == 1
    assert len(ghl_calls) == 1


async def test_a_stale_ghl_claim_is_not_a_running_refresh(mock_db, alice, ghl_calls):
    await _both(mock_db, ghl_refresh_status="running",
                _ghl_claimed_at=datetime.utcnow() - timedelta(minutes=client_refresh.GHL_CLAIM_HELD_MINUTES + 1))
    out = await t.refresh_client_data.fn(client="Acme", integration="ghl")
    await _drain()
    assert out["started"] == ["ghl"] and len(ghl_calls) == 1


async def test_ghl_failure_is_recorded_not_raised(mock_db, alice, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("ghl down")

    monkeypatch.setattr(client_refresh, "fetch_and_cache_ghl_data_optimized", boom)
    await _both(mock_db)
    await t.refresh_client_data.fn(client="Acme", integration="ghl")
    await _drain()
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["ghl_refresh_status"] == "error" and "ghl down" in g["ghl_refresh_error"]


async def test_refresh_other_users_client_not_found(mock_db, alice, ghl_calls):
    await _both(mock_db, gid="gb", name="Bob Plumbing", user=BOB)
    with pytest.raises(ToolError, match="No client matches"):
        await t.refresh_client_data.fn(client="Bob Plumbing")
    with pytest.raises(ToolError, match="No client matches"):
        await t.refresh_client_data.fn(client="gb")
    with pytest.raises(ToolError, match="No client matches"):
        await t.refresh_status.fn(client="gb")
    assert await mock_db["meta_refresh_jobs"].count_documents({}) == 0
    assert ghl_calls == []
    assert "meta_refresh_status" not in await mock_db["client_groups"].find_one({"id": "gb"})


async def test_ambiguous_client_returns_candidates_and_starts_nothing(mock_db, alice, ghl_calls):
    await _both(mock_db, "g1", "Acme Dental")
    await _both(mock_db, "g2", "Acme Law")
    with pytest.raises(ToolError, match="Several clients match 'Acme': Acme Dental, Acme Law"):
        await t.refresh_client_data.fn(client="Acme")
    with pytest.raises(ToolError, match="Several clients match"):
        await t.refresh_status.fn(client="Acme")
    assert await mock_db["meta_refresh_jobs"].count_documents({}) == 0
    assert ghl_calls == []


async def test_refresh_status_idle_then_running_with_progress(mock_db, alice, ghl_calls):
    await _both(mock_db)
    idle = await t.refresh_status.fn(client="Acme")
    assert idle["client"] == {"id": "g1", "name": "Acme Dental"}
    assert idle["meta_refresh_status"] == "idle" and idle["ghl_refresh_status"] == "idle"
    assert idle["meta_refresh_progress"] is None
    await t.refresh_client_data.fn(client="Acme", integration="meta")
    running = await t.refresh_status.fn(client="Acme")
    assert running["meta_refresh_status"] == "running"
    assert running["meta_refresh_progress"]["status"] == "pending"
    assert running["meta_refresh_progress"]["presets_done"] == 0


async def test_service_status_matches_route_body(mock_mongo_client, mock_db, alice):
    await _both(mock_db, meta_refresh_status="complete", ghl_refresh_error="x", meta_token_error=True)
    body = await client_refresh.get_refresh_status(mock_mongo_client, "g1", ALICE)
    assert body == {"meta_refresh_status": "complete", "ghl_refresh_status": "idle", "meta_refresh_error": None,
                    "ghl_refresh_error": "x", "meta_token_error": True, "meta_refresh_progress": None}
    assert await client_refresh.get_refresh_status(mock_mongo_client, "g1", BOB) is None


# --- scopes ---------------------------------------------------------------

async def test_read_scope_refused_for_refresh_client_data(mcp_call, mock_db, ghl_calls):
    await _both(mock_db)
    res = await mcp_call("refresh_client_data", {"client": "Acme"}, user=ALICE, scope="birdy.read", token_type="mcp")
    assert res.is_error and "view-only" in res.text
    assert await mock_db["meta_refresh_jobs"].count_documents({}) == 0
    ok = await mcp_call("refresh_status", {"client": "Acme"}, user=ALICE, scope="birdy.read", token_type="mcp")
    assert not ok.is_error
    started = await mcp_call("refresh_client_data", {"client": "Acme", "integration": "meta"}, user=ALICE,
                             scope="birdy.read birdy.write", token_type="mcp")
    assert not started.is_error


async def test_fresh_ghl_claim_is_held_and_not_restarted(mock_db, alice, ghl_calls):
    await _both(mock_db, ghl_refresh_status="running", _ghl_claimed_at=datetime.utcnow() - timedelta(minutes=2))
    out = await t.refresh_client_data.fn(client="Acme", integration="ghl")
    await _drain()
    assert out["started"] == [] and out["already_running"] == ["ghl"] and ghl_calls == []
    status = await t.refresh_status.fn(client="Acme")
    assert status["ghl_refresh_status"] == "running" and "ghl_refresh_hint" not in status


async def test_concurrent_calls_start_one_ghl_load(mock_db, alice, ghl_calls):
    await _both(mock_db)
    outs = await asyncio.gather(*[t.refresh_client_data.fn(client="Acme", integration="ghl") for _ in range(3)])
    await _drain()
    assert sum(o["started"] == ["ghl"] for o in outs) == 1
    assert len(ghl_calls) == 1


async def test_stale_ghl_claim_is_reported_stale_with_a_hint(mock_db, alice):
    await _both(mock_db, ghl_refresh_status="running",
                _ghl_claimed_at=datetime.utcnow() - timedelta(minutes=client_refresh.GHL_CLAIM_HELD_MINUTES + 5))
    status = await t.refresh_status.fn(client="Acme")
    assert status["ghl_refresh_status"] == "stale"
    assert "call refresh_client_data again" in status["ghl_refresh_hint"]
    assert status["meta_refresh_status"] == "idle"
