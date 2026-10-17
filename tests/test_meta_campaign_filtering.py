"""Campaign-scoped preset fetches: filtering params on /campaigns and /insights."""

import json
import urllib.parse
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from services import meta_service
from services.meta_campaign_scope import campaign_filter


@pytest.fixture
def graph(monkeypatch):
    """Route all httpx.AsyncClient traffic in meta_service to a recording mock."""
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if request.method == "POST":
            n = len(json.loads(dict(urllib.parse.parse_qsl(request.content.decode()))["batch"]))
            return httpx.Response(200, json=[
                {"code": 200, "body": json.dumps({"data": []})} for _ in range(n)
            ])
        return httpx.Response(200, json={"data": []})

    real = httpx.AsyncClient

    def factory(*_a, **kw):
        return real(transport=httpx.MockTransport(handler), **{
            k: v for k, v in kw.items() if k != "timeout"
        })

    monkeypatch.setattr("services.meta_service.httpx.AsyncClient", factory)
    return seen


def _qs(req):
    return parse_qs(urlparse(str(req.url)).query)


async def test_single_preset_sends_id_filter_on_campaigns_and_campaign_id_on_insights(graph):
    await meta_service._fetch_meta_campaigns_for_preset(
        "act_1", "T", "last_7d", creatives_by_ad={}, campaign_ids=["c1"]
    )
    camp = next(r for r in graph if r.url.path.endswith("/campaigns"))
    ins = next(r for r in graph if r.url.path.endswith("/insights"))
    assert json.loads(_qs(camp)["filtering"][0]) == [
        {"field": "id", "operator": "IN", "value": ["c1"]}
    ]
    assert json.loads(_qs(ins)["filtering"][0]) == [
        {"field": "campaign.id", "operator": "IN", "value": ["c1"]}
    ]


async def test_single_preset_no_filter_when_none(graph):
    await meta_service._fetch_meta_campaigns_for_preset(
        "act_1", "T", "last_7d", creatives_by_ad={}
    )
    assert graph
    assert all("filtering" not in str(r.url) for r in graph)


async def test_batch_relative_urls_carry_urlencoded_filter(graph):
    ids = [f"{i:018d}" for i in range(300)]
    await meta_service._fetch_meta_presets_batch(
        "act_1", "T", ["last_7d", "today"], creatives_by_ad={}, campaign_ids=ids
    )
    posts = [r for r in graph if r.method == "POST"]
    assert len(posts) == 1
    batch = json.loads(dict(urllib.parse.parse_qsl(posts[0].content.decode()))["batch"])
    encoded = urllib.parse.quote(campaign_filter(ids))
    camp = [b for b in batch if "/campaigns" in b["relative_url"]]
    ins = [b for b in batch if "/insights" in b["relative_url"]]
    assert camp and ins
    for b in camp:
        assert encoded.replace("campaign.id", "id") in b["relative_url"]
    for b in ins:
        assert encoded in b["relative_url"]
    for b in batch:
        tail = b["relative_url"].split("filtering=")[1]
        assert "[" not in tail and '"' not in tail


async def test_batch_no_filter_when_none(graph):
    await meta_service._fetch_meta_presets_batch("act_1", "T", ["today"], creatives_by_ad={})
    posts = [r for r in graph if r.method == "POST"]
    assert "filtering" not in posts[0].content.decode()


async def test_empty_list_makes_no_graph_call_and_returns_zero_preset(graph):
    single = await meta_service._fetch_meta_campaigns_for_preset(
        "act_1", "T", "last_7d", creatives_by_ad={}, campaign_ids=[]
    )
    batch = await meta_service._fetch_meta_presets_batch(
        "act_1", "T", ["last_7d"], creatives_by_ad={}, campaign_ids=[]
    )
    assert graph == []
    for res in (single, batch["last_7d"]):
        assert res["metrics"]["insights"]["spend"] == 0
        assert res["metrics"]["insights"]["results"] == 0
        assert res["campaigns"] == []


async def test_all_presets_reads_selection_from_group(graph, mock_mongo_client, monkeypatch):
    from core.database import DB_NAME

    await mock_mongo_client[DB_NAME]["client_groups"].insert_one({
        "id": "g1", "user_id": "u1", "meta_campaign_ids": ["c1"],
        "ad_account_currency": "USD",
    })

    async def fake_token(*_a, **_k):
        return {"access_token": "T"}

    async def fake_creatives(*_a, **_k):
        return {}

    monkeypatch.setattr(meta_service, "get_facebook_token", fake_token)
    monkeypatch.setattr(meta_service, "_fetch_ad_creatives", fake_creatives)

    await meta_service.fetch_meta_all_presets_for_group(
        "g1", "act_1", "u1", mock_mongo_client, presets=["today"]
    )
    posts = [r for r in graph if r.method == "POST"]
    assert posts
    batch = json.loads(dict(urllib.parse.parse_qsl(posts[0].content.decode()))["batch"])
    assert all(urllib.parse.quote(campaign_filter(["c1"], f)) in b["relative_url"]
               for b, f in [(batch[0], "id"), (batch[1], "campaign.id")])


# ---------------------------------------------------------------- Task 4

from datetime import datetime, timedelta

from core.database import DB_NAME
from services import meta_campaign_scope
from integrations.facebook_utils import facebook_leads


def _patch_httpx(monkeypatch, target, handler):
    real = httpx.AsyncClient

    def factory(*_a, **kw):
        return real(transport=httpx.MockTransport(handler), **{
            k: v for k, v in kw.items() if k != "timeout"
        })

    monkeypatch.setattr(f"{target}.httpx.AsyncClient", factory)


async def test_stage1_ads_filtered_by_campaign(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "a1"}]})

    _patch_httpx(monkeypatch, "integrations.facebook_utils.facebook_leads", handler)
    ids = await facebook_leads.stage1_get_all_ad_ids("act_1", "T", campaign_ids=["c1"])
    assert ids == ["a1"]
    assert json.loads(_qs(seen[0])["filtering"][0]) == [
        {"field": "campaign.id", "operator": "IN", "value": ["c1"]}
    ]


async def test_stage1_no_filter_when_none_and_empty_list_makes_no_call(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "a1"}]})

    _patch_httpx(monkeypatch, "integrations.facebook_utils.facebook_leads", handler)
    assert await facebook_leads.stage1_get_all_ad_ids("act_1", "T") == ["a1"]
    assert "filtering" not in str(seen[0].url)
    seen.clear()
    assert await facebook_leads.stage1_get_all_ad_ids("act_1", "T", campaign_ids=[]) == []
    assert seen == []


async def test_account_campaigns_follows_paging(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if "after=2" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "c2", "name": "B", "effective_status": "PAUSED"}]})
        return httpx.Response(200, json={
            "data": [{"id": "c1", "name": "A", "effective_status": "ACTIVE"}],
            "paging": {"next": "https://graph.facebook.com/v25.0/act_1/campaigns?after=2&access_token=T"},
        })

    _patch_httpx(monkeypatch, "services.meta_campaign_scope", handler)
    out = await meta_campaign_scope.fetch_account_campaigns("act_1", "T")
    assert [c["id"] for c in out] == ["c1", "c2"]
    qs = _qs(seen[0])
    assert qs["fields"] == ["id,name,effective_status"]
    assert qs["limit"] == ["500"]
    assert json.loads(qs["effective_status"][0]) == [
        "ACTIVE", "PAUSED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES", "CAMPAIGN_PAUSED"
    ]

    _patch_httpx(monkeypatch, "services.meta_campaign_scope",
                 lambda r: httpx.Response(500, json={"error": {"message": "x"}}))
    assert await meta_campaign_scope.fetch_account_campaigns("act_1", "T") is None


async def _seed_groups(mock_mongo_client, **extra):
    col = mock_mongo_client[DB_NAME]["client_groups"]
    await col.insert_many([
        {"id": "g1", "user_id": "u1", "meta_ad_account_id": "act_1", **extra},
        {"id": "g2", "user_id": "u1", "meta_ad_account_id": "act_1"},
        {"id": "g3", "user_id": "u1", "meta_ad_account_id": "act_2"},
    ])
    return col


async def test_refresh_account_campaigns_writes_all_siblings_once(mock_mongo_client, monkeypatch):
    col = await _seed_groups(mock_mongo_client)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "c1", "name": "A", "effective_status": "ACTIVE"}]})

    _patch_httpx(monkeypatch, "services.meta_campaign_scope", handler)
    db = mock_mongo_client[DB_NAME]
    await meta_campaign_scope.refresh_account_campaigns(db, "u1", "act_1", "T")
    for gid in ("g1", "g2"):
        doc = await col.find_one({"id": gid})
        assert doc["meta_account_campaigns"] == [{"id": "c1", "name": "A", "effective_status": "ACTIVE"}]
        assert doc["meta_account_campaigns_at"]
    assert "meta_account_campaigns" not in await col.find_one({"id": "g3"})
    assert len(seen) == 1

    await meta_campaign_scope.refresh_account_campaigns(db, "u1", "act_1", "T")
    assert len(seen) == 1  # fresh: no second request


async def test_refresh_account_campaigns_failure_keeps_previous(mock_mongo_client, monkeypatch):
    old = (datetime.utcnow() - timedelta(minutes=60)).isoformat()
    col = await _seed_groups(
        mock_mongo_client,
        meta_account_campaigns=[{"id": "old", "name": "Old", "effective_status": "ACTIVE"}],
        meta_account_campaigns_at=old,
    )
    _patch_httpx(monkeypatch, "services.meta_campaign_scope",
                 lambda r: httpx.Response(500, json={"error": {"message": "x"}}))
    await meta_campaign_scope.refresh_account_campaigns(
        mock_mongo_client[DB_NAME], "u1", "act_1", "T"
    )
    doc = await col.find_one({"id": "g1"})
    assert doc["meta_account_campaigns"][0]["id"] == "old"
    assert doc["meta_account_campaigns_at"] == old
