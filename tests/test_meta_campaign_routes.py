"""
tests/test_meta_campaign_routes.py
----------------------------------
HTTP layer for sharing one Meta ad account between client groups: the
campaign picker list, saving a selection, creating a group on a shared
account, the scoped list/detail payloads and deleting a sharer.

Router functions are called directly with get_mongo_client pointed at the
in-memory mock (same pattern as tests/test_client_targets.py). Graph is never
reached: fetch_account_campaigns and the token lookup are monkeypatched.
"""

import contextlib

import pytest
from fastapi import BackgroundTasks, HTTPException

import routers.client_groups as cg
import routers.meta as meta
import services.meta_campaign_scope as scope
from core.models import ClientGroupRequest, MetaCampaignsRequest

USER = "user@example.com"
OTHER = "someone@else.com"
ACT = "act_1"

LIVE = [
    {"id": "c1", "name": "Spring", "effective_status": "ACTIVE"},
    {"id": "c2", "name": "Summer", "effective_status": "PAUSED"},
    {"id": "c3", "name": "Autumn", "effective_status": "ACTIVE"},
    {"id": "c4", "name": "Old", "effective_status": "ARCHIVED"},
]


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def token(user_id, mongo_client):
        return {"access_token": "tok"}

    async def live(ad_account_id, access_token):
        return [dict(c) for c in LIVE]

    async def no_limit(user, mongo_client):
        return True

    for mod in (cg, meta):
        monkeypatch.setattr(mod, "get_mongo_client", fake_client)
        monkeypatch.setattr(mod, "get_facebook_token", token)
    monkeypatch.setattr(scope, "fetch_account_campaigns", live)
    monkeypatch.setattr(cg, "check_client_limit", no_limit)
    return cg


async def a_group(db, gid, ids=None, user=USER, act=ACT, created="2026-01-01", **extra):
    doc = {"id": gid, "user_id": user, "name": gid.upper(), "meta_ad_account_id": act,
           "created_at": created, "facebook_cache": {}, **extra}
    if ids != "missing":
        doc["meta_campaign_ids"] = ids
    await db["client_groups"].insert_one(doc)


def scheduled(bg: BackgroundTasks):
    return [(t.func.__name__, t.args) for t in bg.tasks]


# ── PATCH /api/client-groups/{id}/meta-campaigns ──────────────────────────


@pytest.mark.asyncio
async def test_patch_saves_and_returns_scope(api, mock_db):
    await a_group(mock_db, "g1", ["c1"], created="2026-01-01")
    await a_group(mock_db, "g2", None, created="2026-02-01")
    bg = BackgroundTasks()

    out = await cg.update_meta_campaigns("g1", MetaCampaignsRequest(campaign_ids=["c1", "c2"]),
                                         bg, current_user=USER)

    assert out["meta_campaign_ids"] == ["c1", "c2"]
    assert out["converted"] == [{"id": "g2", "name": "G2"}]
    assert out["meta_scope"]["mode"] == "selected"
    assert out["meta_scope"]["selected"] == 2
    assert out["meta_scope"]["total"] == 4
    assert out["meta_scope"]["shared"] is True
    assert out["meta_scope"]["unassigned"] == []

    g2 = await mock_db["client_groups"].find_one({"id": "g2"})
    assert g2["meta_campaign_ids"] == ["c3", "c4"]
    for gid in ("g1", "g2"):
        doc = await mock_db["client_groups"].find_one({"id": gid})
        assert doc["meta_refresh_status"] == "running"
        assert [c["id"] for c in doc["meta_account_campaigns"]] == ["c1", "c2", "c3", "c4"]
        assert isinstance(doc["meta_account_campaigns_at"], str)

    runs = scheduled(bg)
    assert sorted(a[0] for _, a in runs) == ["g1", "g2"]
    assert all(name == "_run_refresh" and a[1] == "meta" and a[2] == USER for name, a in runs)
    passed = {a[0]: a[3] for _, a in runs}
    assert passed["g2"]["meta_campaign_ids"] == ["c3", "c4"]


@pytest.mark.asyncio
async def test_patch_conflict_is_409_with_message(api, mock_db):
    await a_group(mock_db, "g1", ["c1"])
    await a_group(mock_db, "g2", ["c2"])

    with pytest.raises(HTTPException) as e:
        await cg.update_meta_campaigns("g1", MetaCampaignsRequest(campaign_ids=["c1", "c2"]),
                                       BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 409
    assert "G2" in e.value.detail and "Summer" in e.value.detail
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["meta_campaign_ids"] == ["c1"]


@pytest.mark.asyncio
async def test_patch_other_users_group_is_404(api, mock_db):
    await a_group(mock_db, "g1", None, user=OTHER)

    with pytest.raises(HTTPException) as e:
        await cg.update_meta_campaigns("g1", MetaCampaignsRequest(campaign_ids=["c1"]),
                                       BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_patch_graph_failure_is_502(api, mock_db, monkeypatch):
    await a_group(mock_db, "g1", None)

    async def down(ad_account_id, access_token):
        return None

    monkeypatch.setattr(scope, "fetch_account_campaigns", down)
    with pytest.raises(HTTPException) as e:
        await cg.update_meta_campaigns("g1", MetaCampaignsRequest(campaign_ids=["c1"]),
                                       BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_patch_group_without_meta_is_400(api, mock_db):
    await a_group(mock_db, "g1", None, act=None)
    with pytest.raises(HTTPException) as e:
        await cg.update_meta_campaigns("g1", MetaCampaignsRequest(campaign_ids=["c1"]),
                                       BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 400


def test_meta_campaigns_request_requires_the_key():
    with pytest.raises(Exception):
        MetaCampaignsRequest()
    assert MetaCampaignsRequest(campaign_ids=None).campaign_ids is None


# ── POST /api/client-groups ───────────────────────────────────────────────


def create_req(**kw):
    base = dict(name="New", ghl_location_id=None, meta_ad_account_id=ACT,
                hotprospector_group_id=None, ad_account_currency="USD")
    base.update(kw)
    return ClientGroupRequest(**base)


@pytest.mark.asyncio
async def test_create_same_meta_different_ghl_with_list_is_allowed(api, mock_db):
    await a_group(mock_db, "g1", None, ghl_location_id="loc1")
    bg = BackgroundTasks()

    out = await cg.create_client_group_optimized(
        create_req(meta_campaign_ids=["c2"]), bg, current_user=USER)

    new = out["client_group"]
    assert new["meta_campaign_ids"] == ["c2"]
    assert out["converted"] == [{"id": "g1", "name": "G1"}]
    g1 = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g1["meta_campaign_ids"] == ["c1", "c3", "c4"]
    assert g1["meta_refresh_status"] == "running"
    assert [c["id"] for c in g1["meta_account_campaigns"]] == ["c1", "c2", "c3", "c4"]

    names = [t[0] for t in scheduled(bg)]
    assert "_initial_client_data_load" in names
    refreshed = [a[0] for n, a in scheduled(bg) if n == "_run_refresh"]
    assert refreshed == ["g1"]


@pytest.mark.asyncio
async def test_create_same_meta_without_list_when_shared_is_409(api, mock_db):
    await a_group(mock_db, "g1", None)

    with pytest.raises(HTTPException) as e:
        await cg.create_client_group_optimized(create_req(), BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 409
    assert await mock_db["client_groups"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_create_claiming_an_owned_campaign_is_409_before_insert(api, mock_db):
    await a_group(mock_db, "g1", ["c1"])

    with pytest.raises(HTTPException) as e:
        await cg.create_client_group_optimized(
            create_req(meta_campaign_ids=["c1"]), BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 409
    assert "Spring" in e.value.detail
    assert await mock_db["client_groups"].count_documents({}) == 1


@pytest.mark.asyncio
async def test_create_unshared_account_without_list_stores_null(api, mock_db, monkeypatch):
    called = []

    async def spy(ad_account_id, access_token):
        called.append(ad_account_id)
        return LIVE

    monkeypatch.setattr(scope, "fetch_account_campaigns", spy)
    out = await cg.create_client_group_optimized(create_req(), BackgroundTasks(), current_user=USER)

    assert out["client_group"]["meta_campaign_ids"] is None
    assert out["converted"] == []
    assert called == [], "a lone group with all campaigns needs no Graph call"


@pytest.mark.asyncio
async def test_create_exact_pair_duplicate_still_409(api, mock_db):
    await a_group(mock_db, "g1", None, ghl_location_id="loc1")
    with pytest.raises(HTTPException) as e:
        await cg.create_client_group_optimized(
            create_req(ghl_location_id="loc1", meta_campaign_ids=["c2"]),
            BackgroundTasks(), current_user=USER)
    assert e.value.status_code == 409


# ── list + detail payloads ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_payload_has_meta_scope(api, mock_db):
    await a_group(mock_db, "g1", ["c1"], meta_account_campaigns=LIVE)
    await a_group(mock_db, "g2", ["c2"], meta_account_campaigns=LIVE)
    await a_group(mock_db, "g3", "missing", act="act_9")
    await a_group(mock_db, "g4", None, act=None)

    out = await cg.get_client_groups(date_preset="maximum", include_daily=False, current_user=USER)
    by_id = {g["id"]: g for g in out["client_groups"]}

    g1 = by_id["g1"]
    assert g1["meta_campaign_ids"] == ["c1"]
    assert g1["meta_scope"] == {
        "mode": "selected", "selected": 1, "total": 4, "shared": True,
        "unassigned": [{"id": "c3", "name": "Autumn"}],
    }
    assert "meta_account_campaigns" not in g1
    assert by_id["g3"]["meta_campaign_ids"] is None
    assert by_id["g3"]["meta_scope"]["mode"] == "all"
    assert by_id["g3"]["meta_scope"]["shared"] is False
    assert by_id["g4"]["meta_scope"] is None


@pytest.mark.asyncio
async def test_detail_payload_has_meta_scope(api, mock_db, monkeypatch):
    await a_group(mock_db, "g1", ["c1"], meta_account_campaigns=LIVE,
                  facebook_cache={"maximum": {"campaigns": [{"id": "c1"}], "metrics": {}}})
    await a_group(mock_db, "g2", ["c2"], meta_account_campaigns=LIVE)

    out = await cg.get_client_group_comprehensive("g1", current_user=USER)
    info = out["data"]["group_info"]
    assert info["meta_campaign_ids"] == ["c1"]
    assert info["meta_scope"]["shared"] is True
    assert info["meta_scope"]["unassigned"] == [{"id": "c3", "name": "Autumn"}]
    assert "meta_account_campaigns" not in info


@pytest.mark.asyncio
async def test_detail_scoped_group_with_empty_cache_does_not_call_graph(api, mock_db, monkeypatch):
    await a_group(mock_db, "g1", ["c1"], facebook_cache={})

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("Graph must not be called for a scoped group")

    async def no_account_data(*a, **k):
        raise AssertionError("account_data must not be read for a scoped group")

    monkeypatch.setattr(cg.httpx, "AsyncClient", Boom)
    monkeypatch.setattr(cg, "get_facebook_data", no_account_data)

    out = await cg.get_client_group_comprehensive("g1", current_user=USER)
    block = out["data"]["meta_data"]
    assert block == {
        "ad_account_id": ACT, "campaigns": [], "adsets": [], "ads": [], "leads": [],
        "summary": {}, "from_cache": True, "syncing": True,
    }
    assert out["data"]["marketing"]["campaigns"] == []


@pytest.mark.asyncio
async def test_detail_scoped_group_with_stale_cache_serves_its_own_cache(api, mock_db, monkeypatch):
    """No fresh-cache clock (last_ghl_refresh missing) must still not go account-wide."""
    await a_group(mock_db, "g1", ["c1"],
                  facebook_cache={"maximum": {"campaigns": [{"id": "c1"}], "metrics": {}}})

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("Graph must not be called for a scoped group")

    monkeypatch.setattr(cg.httpx, "AsyncClient", Boom)
    out = await cg.get_client_group_comprehensive("g1", current_user=USER)
    block = out["data"]["meta_data"]
    assert block["campaigns"] == [{"id": "c1"}]
    assert block["from_cache"] is True
    assert "syncing" not in block


# ── GET /api/facebook/adaccounts/{id}/campaigns ───────────────────────────


@pytest.mark.asyncio
async def test_campaigns_endpoint_marks_claimed_by(api, mock_db, monkeypatch):
    await a_group(mock_db, "g1", ["c1"])
    await a_group(mock_db, "g2", None)
    await a_group(mock_db, "g3", ["c2"], user=OTHER)

    out = await meta.get_ad_account_campaigns(ACT, current_user=USER)
    rows = out["campaigns"]
    assert [r["id"] for r in rows] == ["c3", "c1", "c2", "c4"]  # ACTIVE by name, PAUSED, others
    by_id = {r["id"]: r for r in rows}
    assert by_id["c1"]["claimed_by"] == {"group_id": "g1", "name": "G1"}
    assert by_id["c2"]["claimed_by"] is None  # another user's group does not claim
    assert by_id["c3"]["claimed_by"] is None  # a null-list group claims nothing
    assert set(by_id["c1"]) == {"id", "name", "effective_status", "claimed_by"}


@pytest.mark.asyncio
async def test_campaigns_endpoint_graph_failure_is_502(api, mock_db, monkeypatch):
    async def down(ad_account_id, access_token):
        return None

    monkeypatch.setattr(scope, "fetch_account_campaigns", down)
    with pytest.raises(HTTPException) as e:
        await meta.get_ad_account_campaigns(ACT, current_user=USER)
    assert e.value.status_code == 502


@pytest.mark.asyncio
async def test_campaigns_endpoint_without_token_is_400(api, mock_db, monkeypatch):
    async def none(user_id, mongo_client):
        return None

    monkeypatch.setattr(meta, "get_facebook_token", none)
    with pytest.raises(HTTPException) as e:
        await meta.get_ad_account_campaigns(ACT, current_user=USER)
    assert e.value.status_code == 400


# ── DELETE ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_group_reassigns_account_leads(api, mock_db):
    await a_group(mock_db, "g1", ["c1"])
    await a_group(mock_db, "g2", ["c2"])
    await mock_db["facebook_leads"].insert_many([
        {"user_id": USER, "ad_account_id": ACT, "lead_data": {"campaign_id": "c1"},
         "client_group_id": "g1", "client_group_name": "G1"},
        {"user_id": USER, "ad_account_id": ACT, "lead_data": {"campaign_id": "c2"},
         "client_group_id": "g2", "client_group_name": "G2"},
    ])

    await cg.delete_client_group("g2", current_user=USER)

    leads = {l["lead_data"]["campaign_id"]: l
             async for l in mock_db["facebook_leads"].find({})}
    assert leads["c1"]["client_group_id"] == "g1"
    assert leads["c2"]["client_group_id"] is None
    assert (await mock_db["client_groups"].find_one({"id": "g1"}))["meta_campaign_ids"] == ["c1"]
