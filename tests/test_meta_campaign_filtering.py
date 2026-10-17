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
