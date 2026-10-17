"""
tests/test_meta_scope_final_fixes.py
------------------------------------
Final whole-branch review fixes for Meta campaign scoping:

  C1  incremental leads carry campaign ids; reassignment resolves rows without
      one through the cached ad -> campaign map and never unassigns a row it
      cannot place
  I1  the startup cache filler leaves scoped groups to the scoped pipeline
  M2  the AI tools do not fall back to account-wide legacy insights for a
      scoped group
  Router: a failed lead reassignment does not 500 a successful delete, and
      the create response does not carry the account campaign list
"""

import contextlib

import pytest
from fastapi import BackgroundTasks

import integrations.facebook_utils.meta_incremental_refresh as inc
from services.meta_campaign_scope import apply_selection, reassign_account_leads

USER = "A"
ACT = "act_1"


def _group(gid, name, ids="__missing__", ads=None, **extra):
    doc = {"id": gid, "user_id": USER, "name": name, "meta_ad_account_id": ACT,
           "created_at": "2026-01-01T00:00:00", **extra}
    if ids != "__missing__":
        doc["meta_campaign_ids"] = ids
    if ads is not None:
        doc["facebook_cache"] = {"entities": {
            "campaigns": [], "adsets": [],
            "ads": [{"id": a, "campaign_id": c} for a, c in ads.items()],
        }}
    return doc


def _row(lid, lead_data, owner="g1", owner_name="Plush Body"):
    return {"user_id": USER, "ad_account_id": ACT, "client_group_id": owner,
            "client_group_name": owner_name, "lead_id": lid, "lead_data": lead_data}


async def _rows(db):
    return {r["lead_id"]: r async for r in db.facebook_leads.find({}, {"_id": 0})}


# ── C1 (a): incremental leads keep campaign/adset ids ──────────────────────


async def test_incremental_lead_request_asks_for_campaign_fields(monkeypatch):
    seen = {}

    async def fake_get(client, url, params):
        seen["params"] = params
        return {"name": "Ad One", "leads": {"data": [{
            "id": "L1", "created_time": "2026-10-16T10:00:00+0000",
            "campaign_id": "c1", "campaign_name": "Spring",
            "adset_id": "as1", "adset_name": "Set A",
            "field_data": [{"name": "email", "values": ["a@b.c"]}],
        }]}}

    monkeypatch.setattr(inc, "_api_get_with_retry", fake_get)
    raw, _ = await inc._get_todays_leads_for_ad("ad1", "T", None, None)

    fields = seen["params"]["fields"]
    for f in ("campaign_id", "campaign_name", "adset_id", "adset_name"):
        assert f in fields, f

    row = inc._normalize_leads(raw, USER, ACT, "g1", "Plush Body")[0]
    assert row["campaign_id"] == "c1"
    assert row["campaign_name"] == "Spring"
    assert row["adset_id"] == "as1"
    assert row["adset_name"] == "Set A"
    assert row["ad_id"] == "ad1"
    assert row["email"] == "a@b.c"


async def test_incremental_path_passes_group_selection_to_stage1(
        mock_mongo_client, mock_db, monkeypatch):
    import integrations.facebook_utils.facebook as fb
    import integrations.facebook_utils.facebook_leads as fl
    from utils.currency_exchange import CurrencyService

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1", "c3"]))
    seen = []

    async def token(*_a, **_k):
        return {"access_token": "T"}

    async def currency(*_a, **_k):
        return "USD"

    async def stage1(ad_account_id, access_token, campaign_ids=None):
        seen.append(campaign_ids)
        return []

    monkeypatch.setattr(fb, "get_facebook_token", token)
    monkeypatch.setattr(CurrencyService, "get_user_currency", staticmethod(currency))
    monkeypatch.setattr(fl, "stage1_get_all_ad_ids", stage1)

    await fl.fetch_and_cache_facebook_leads_FIXED(
        "USD", "g1", ACT, USER, mock_mongo_client, is_initial_load=False)

    assert seen == [["c1", "c3"]]


# ── C1 (b): reassignment resolves rows with no campaign id ─────────────────


async def test_rows_without_campaign_id_resolve_through_cached_ads(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"], ads={"ad1": "c1"}),
        _group("g2", "Plush Face", ["c2"], ads={"ad2": "c2"}),
    ])
    await mock_db.facebook_leads.insert_many([
        _row("l1", {"ad_id": "ad1"}),                      # incremental shape
        _row("l2", {"ad_id": "ad2"}),                      # belongs to g2
        _row("l3", {"ad_id": "ad2", "campaign_id": ""}),   # empty string too
    ])

    await reassign_account_leads(mock_db, USER, ACT)

    rows = await _rows(mock_db)
    assert rows["l1"]["client_group_id"] == "g1"
    assert rows["l2"]["client_group_id"] == "g2"
    assert rows["l2"]["client_group_name"] == "Plush Face"
    assert rows["l3"]["client_group_id"] == "g2"
    # Backfilled, so later passes do not need the cache.
    assert rows["l1"]["lead_data"]["campaign_id"] == "c1"
    assert rows["l2"]["lead_data"]["campaign_id"] == "c2"
    assert rows["l3"]["lead_data"]["campaign_id"] == "c2"


async def test_unresolvable_rows_keep_their_owner(mock_db):
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ["c1"], ads={"ad1": "c1"}),
        _group("g2", "Plush Face", ["c2"]),
    ])
    await mock_db.facebook_leads.insert_many([
        _row("unknown_ad", {"ad_id": "adX"}),
        _row("no_ad", {}),
        _row("blank", {"ad_id": "adY", "campaign_id": ""}, owner="g2", owner_name="Plush Face"),
        _row("null", {"campaign_id": None}),
        _row("unclaimed", {"campaign_id": "c9"}),
    ])

    await reassign_account_leads(mock_db, USER, ACT)

    rows = await _rows(mock_db)
    assert rows["unknown_ad"]["client_group_id"] == "g1"
    assert rows["no_ad"]["client_group_id"] == "g1"
    assert rows["blank"]["client_group_id"] == "g2"
    assert rows["null"]["client_group_id"] == "g1"
    assert rows["unclaimed"]["client_group_id"] is None


async def test_apply_selection_resolves_with_the_cache_it_is_about_to_clear(mock_db):
    # g1 owns everything and its cache is the only ad map; apply_selection
    # clears that cache, so the map has to be read before the write.
    await mock_db.client_groups.insert_many([
        _group("g1", "Plush Body", ads={"ad1": "c1", "ad2": "c2"}),
        _group("g2", "Plush Face", []),
    ])
    await mock_db.facebook_leads.insert_many([
        _row("l1", {"ad_id": "ad1"}), _row("l2", {"ad_id": "ad2"})])

    await apply_selection(mock_db, USER, "g2", ["c2"], [
        {"id": "c1", "name": "Spring", "effective_status": "ACTIVE"},
        {"id": "c2", "name": "Face", "effective_status": "ACTIVE"},
    ])

    rows = await _rows(mock_db)
    assert rows["l1"]["client_group_id"] == "g1"
    assert rows["l2"]["client_group_id"] == "g2"


# ── I1: startup cache fill skips scoped groups ─────────────────────────────


async def test_startup_cache_fill_skips_scoped_groups(mock_mongo_client, mock_db, monkeypatch):
    import jobs.cache_jobs as cj

    await mock_db.client_groups.insert_many([
        {"id": "all", "user_id": USER, "meta_ad_account_id": ACT, "facebook_cache": {}},
        {"id": "listed", "user_id": USER, "meta_ad_account_id": ACT, "facebook_cache": {},
         "meta_campaign_ids": ["c1"]},
        {"id": "none", "user_id": USER, "meta_ad_account_id": ACT, "facebook_cache": {},
         "meta_campaign_ids": []},
    ])
    fetched = []

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def meta(group_id, *_a, **_k):
        fetched.append(group_id)

    async def nothing(*_a, **_k):
        return None

    monkeypatch.setattr(cj, "get_mongo_client", fake_client)
    monkeypatch.setattr(cj, "fetch_and_cache_meta_data", meta)
    monkeypatch.setattr(cj, "get_subaccount_tokens", nothing)
    monkeypatch.setattr(cj, "get_facebook_data", nothing)
    monkeypatch.setattr(cj, "get_hotprospector_credentials", nothing)

    await cj.populate_cache_for_existing_groups()

    assert fetched == ["all"]


# ── M2: no account-wide legacy fallback for scoped groups ──────────────────


async def _legacy_setup(db):
    await db.client_groups.insert_many([
        {"id": "scoped", "user_id": USER, "name": "Scoped", "meta_campaign_ids": ["c1"],
         "facebook_cache": {}},
        {"id": "plain", "user_id": USER, "name": "Plain", "facebook_cache": {}},
    ])
    for coll in ("facebook_campaign_insights", "facebook_adset_insights", "facebook_ad_insights"):
        await db[coll].insert_many([
            {"user_id": USER, "client_group_id": "scoped", "campaign_id": "c2",
             "date_start": "2026-10-01", "insight_data": {"spend": "99"}},
            {"user_id": USER, "client_group_id": "plain", "campaign_id": "c7",
             "date_start": "2026-10-01", "insight_data": {"spend": "5"}},
        ])


@pytest.mark.parametrize("fn", ["get_campaign_insights", "get_adset_insights", "get_ad_insights"])
async def test_ai_tools_skip_legacy_fallback_for_scoped_group(mock_db, fn):
    from ai.tools import meta_tools

    await _legacy_setup(mock_db)
    out = await getattr(meta_tools, fn)(mock_db, USER, group_ids=["scoped"])
    assert out["insights"] == [] and out["total"] == 0

    every = await getattr(meta_tools, fn)(mock_db, USER)
    assert [r["client_group_id"] for r in every["insights"]] == ["plain"]


@pytest.mark.parametrize("fn", ["get_campaign_insights", "get_adset_insights", "get_ad_insights"])
async def test_mcp_tools_skip_legacy_fallback_for_scoped_group(mock_db, set_current_user, fn):
    from ai.mcp import meta_mcp

    set_current_user(USER)
    await _legacy_setup(mock_db)
    out = await getattr(meta_mcp, fn).fn(group_ids=["scoped"])
    assert out["insights"] == [] and out["total"] == 0

    every = await getattr(meta_mcp, fn).fn()
    assert [r["client_group_id"] for r in every["insights"]] == ["plain"]


# ── Router must-fixes ──────────────────────────────────────────────────────


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    import routers.client_groups as cg
    import services.meta_campaign_scope as scope

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def token(*_a, **_k):
        return {"access_token": "tok"}

    async def live(*_a, **_k):
        return [{"id": "c1", "name": "Spring", "effective_status": "ACTIVE"},
                {"id": "c2", "name": "Summer", "effective_status": "ACTIVE"}]

    async def no_limit(*_a, **_k):
        return True

    monkeypatch.setattr(cg, "get_mongo_client", fake_client)
    monkeypatch.setattr(cg, "get_facebook_token", token)
    monkeypatch.setattr(scope, "fetch_account_campaigns", live)
    monkeypatch.setattr(cg, "check_client_limit", no_limit)
    return cg


async def test_delete_succeeds_when_lead_reassignment_fails(api, mock_db, monkeypatch):
    import services.meta_campaign_scope as scope

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ["c1"]))

    async def boom(*_a, **_k):
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(scope, "on_group_deleted", boom)
    out = await api.delete_client_group("g1", current_user=USER)

    assert out["success"] is True
    assert await mock_db.client_groups.count_documents({"id": "g1"}) == 0


async def test_create_response_omits_account_campaign_list(api, mock_db):
    from core.models import ClientGroupRequest

    await mock_db.client_groups.insert_one(_group("g1", "Plush Body", ghl_location_id="loc1"))
    out = await api.create_client_group_optimized(
        ClientGroupRequest(name="New", ghl_location_id=None, meta_ad_account_id=ACT,
                           hotprospector_group_id=None, ad_account_currency="USD",
                           meta_campaign_ids=["c2"]),
        BackgroundTasks(), current_user=USER)

    assert out["client_group"]["meta_campaign_ids"] == ["c2"]
    assert "meta_account_campaigns" not in out["client_group"]
