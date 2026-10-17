"""
tests/test_meta_live_scope.py
-----------------------------
Ask Birdy's live Meta tools call Graph directly, so a group that owns only some
campaigns of a shared ad account must send a campaign filter (`id` on
/campaigns, `campaign.id` on /insights and /ads) or the answer is whole-account
spend. campaign_ids None sends no filter; [] never reaches Graph.
"""

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import ai.mcp.meta_live_mcp as mcp_mod
import ai.tools.meta_live_tools as tools_mod
import ai.tools.multi_window_tools as mw_mod
from core.database import DB_NAME

USER = "u@example.com"


def _filter(url):
    q = parse_qs(urlparse(url).query)
    return json.loads(q["filtering"][0]) if "filtering" in q else None


@pytest.fixture
def graph(monkeypatch):
    """Record every Graph URL for the given modules; answer with empty data."""
    seen = []

    def install(*modules):
        real = httpx.AsyncClient

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": []})

        def factory(*_a, **kw):
            return real(transport=httpx.MockTransport(handler),
                        **{k: v for k, v in kw.items() if k != "timeout"})

        for m in modules:
            monkeypatch.setattr(m.httpx, "AsyncClient", factory)

            async def token(*_a, **_k):
                return {"access_token": "T"}
            monkeypatch.setattr(m, "get_facebook_token", token)
    install.seen = seen
    return install


async def _seed(db, ids):
    doc = {"id": "g1", "name": "Acme", "user_id": USER, "meta_ad_account_id": "act_1"}
    if ids != "missing":
        doc["meta_campaign_ids"] = ids
    await db["client_groups"].insert_one(doc)


@pytest.fixture
def db(mock_mongo_client):
    return mock_mongo_client[DB_NAME]


# (label, caller) — the same tool in the registry module and its MCP twin.
async def _insights_tools(db, **kw):
    return await tools_mod.get_meta_insights_live(db, USER, "2026-01-01", "2026-01-31", **kw)


async def _insights_mcp(db, **kw):
    return await mcp_mod.get_meta_insights_live.fn("2026-01-01", "2026-01-31", **kw)


async def _monthly_tools(db):
    return await tools_mod.get_meta_insights_monthly(db, USER, 2026)


async def _monthly_mcp(db):
    return await mcp_mod.get_meta_insights_monthly.fn(2026)


async def _leads_tools(db):
    return await tools_mod.get_meta_leads_live(db, USER, "2026-01-01", "2026-01-31")


async def _leads_mcp(db):
    return await mcp_mod.get_meta_leads_live.fn("2026-01-01", "2026-01-31")


async def _window(db):
    return await mw_mod.get_metrics_by_day_windows(db, USER, [30], mongo_client=None)


MODS = (tools_mod, mcp_mod, mw_mod)


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_insights_tools, _insights_mcp])
async def test_insights_live_filters_campaigns_by_id(call, db, graph, set_current_user):
    set_current_user(USER)
    graph(*MODS)
    await _seed(db, ["c1", "c2"])
    await call(db)
    assert _filter(graph.seen[0]) == [{"field": "id", "operator": "IN", "value": ["c1", "c2"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_monthly_tools, _monthly_mcp, _window])
async def test_insights_edge_filters_by_campaign_dot_id(call, db, graph, set_current_user):
    set_current_user(USER)
    graph(*MODS)
    await _seed(db, ["c1"])
    await call(db)
    assert _filter(graph.seen[0]) == [{"field": "campaign.id", "operator": "IN", "value": ["c1"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [_leads_tools, _leads_mcp])
async def test_leads_ads_edge_filters_by_campaign_dot_id(call, db, graph, set_current_user):
    set_current_user(USER)
    graph(*MODS)
    await _seed(db, ["c1"])
    await call(db)
    assert "/ads" in graph.seen[0]
    assert _filter(graph.seen[0]) == [{"field": "campaign.id", "operator": "IN", "value": ["c1"]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [None, "missing"])
@pytest.mark.parametrize(
    "call", [_insights_tools, _insights_mcp, _monthly_tools, _monthly_mcp,
             _leads_tools, _leads_mcp, _window])
async def test_unscoped_group_sends_no_filter(call, ids, db, graph, set_current_user):
    set_current_user(USER)
    graph(*MODS)
    await _seed(db, ids)
    await call(db)
    assert graph.seen and all(_filter(u) is None for u in graph.seen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call", [_insights_tools, _insights_mcp, _monthly_tools, _monthly_mcp,
             _leads_tools, _leads_mcp, _window])
async def test_empty_selection_skips_graph_and_says_so(call, db, graph, set_current_user):
    set_current_user(USER)
    graph(*MODS)
    await _seed(db, [])
    result = await call(db)
    assert graph.seen == []
    assert "no campaigns are selected" in json.dumps(result).lower()
