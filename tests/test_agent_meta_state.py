from datetime import date

import httpx
import pytest

from ai.agents import meta_state
from ai.agents.meta_state import MetaTokenError, backfill_paused_at, refresh_meta_state
from ai.agents.store import AD_DAYS, create_agent_indexes
from agent_history_helpers import seed_ad_days

GID = "cg1"


def _ad(status):
    return {"id": "a1", "name": "Ad", "adset_id": "s1", "campaign_id": "c1",
            "effective_status": status, "created_time": "2026-09-01T10:00:00+0000"}


def _adset():
    return {"id": "s1", "name": "S", "campaign_id": "c1", "effective_status": "ACTIVE",
            "daily_budget": "2000", "campaign": {"effective_status": "ACTIVE"}}


@pytest.fixture
def graph(monkeypatch):
    """Fake Graph: state['ad_status'] drives the ad; ads are served over two pages."""
    state = {"ad_status": "ACTIVE", "calls": [], "error": None}

    async def fake_token(user_id, mongo_client):
        return {"access_token": "tok"}

    async def fake_get(self, url, params=None, **kw):
        state["calls"].append((str(url), dict(params or {})))
        req = httpx.Request("GET", str(url))
        if state["error"]:
            return httpx.Response(400, json={"error": state["error"]}, request=req)
        if "/adsets" in str(url):
            return httpx.Response(200, json={"data": [_adset()]}, request=req)
        if "page2" in str(url):
            return httpx.Response(200, json={"data": []}, request=req)
        return httpx.Response(200, json={"data": [_ad(state["ad_status"])],
                                         "paging": {"next": "https://graph.facebook.com/v25.0/act_1/ads?page2"}},
                              request=req)

    monkeypatch.setattr(meta_state, "get_facebook_token", fake_token)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return state


async def test_refresh_sets_paused_at_on_transition_and_clears_on_reactivate(mock_db, mock_mongo_client, graph):
    r = await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 1))
    assert r == {"ads": 1, "adsets": 1}
    doc = await mock_db.meta_ad_state.find_one({"client_group_id": GID, "ad_id": "a1"})
    assert doc["paused_at"] is None and doc["effective_status"] == "ACTIVE"
    adset = await mock_db.meta_adset_state.find_one({"client_group_id": GID, "adset_id": "s1"})
    assert adset["daily_budget"] == 20.0 and adset["campaign_status"] == "ACTIVE"

    graph["ad_status"] = "PAUSED"
    await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 5))
    doc = await mock_db.meta_ad_state.find_one({"ad_id": "a1"})
    assert doc["paused_at"] == "2026-10-05"

    # still paused later: paused_at is not moved
    await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 9))
    assert (await mock_db.meta_ad_state.find_one({"ad_id": "a1"}))["paused_at"] == "2026-10-05"

    graph["ad_status"] = "ACTIVE"
    await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 12))
    assert (await mock_db.meta_ad_state.find_one({"ad_id": "a1"}))["paused_at"] is None
    assert await mock_db.meta_ad_state.count_documents({}) == 1  # upsert, no dupes

    ad_calls = [c for c in graph["calls"] if "/ads" in c[0] and "/adsets" not in c[0]]
    assert ad_calls[0][1]["fields"] == "id,name,adset_id,campaign_id,effective_status,created_time"
    assert any("page2" in c[0] for c in ad_calls)  # paged


async def test_refresh_token_error_raises_MetaTokenError(mock_db, mock_mongo_client, graph):
    graph["error"] = {"code": 190, "message": "Error validating access token"}
    with pytest.raises(MetaTokenError):
        await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 1))


async def test_refresh_no_token_raises_MetaTokenError(mock_db, mock_mongo_client, monkeypatch):
    async def none(*a, **k):
        return None
    monkeypatch.setattr(meta_state, "get_facebook_token", none)
    with pytest.raises(MetaTokenError):
        await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 1))


async def test_backfill_paused_at_from_last_spend_day(mock_db):
    await mock_db.meta_ad_state.insert_many([
        {"client_group_id": GID, "ad_id": "a1", "effective_status": "PAUSED", "paused_at": None},
        {"client_group_id": GID, "ad_id": "a2", "effective_status": "PAUSED", "paused_at": "2026-09-01"},
        {"client_group_id": GID, "ad_id": "a3", "effective_status": "ACTIVE", "paused_at": None},
        {"client_group_id": GID, "ad_id": "a4", "effective_status": "PAUSED", "paused_at": None},
    ])
    def row(ad, day, spend, user="u"):
        return {"user_id": user, "client_group_id": GID, "ad_id": ad, "date_start": day,
                "insight_data": {"spend": spend}}
    await seed_ad_days(mock_db, [
        row("a1", "2026-10-01", "5.0"), row("a1", "2026-10-03", "2.0"), row("a1", "2026-10-04", "0"),
        row("a4", "2026-10-02", "0"),
        row("a1", "2026-10-09", "9.0", user="someone_else"),   # another user's row is never read
    ])
    n = await backfill_paused_at(mock_db, "u", GID)
    assert n == 1
    assert (await mock_db.meta_ad_state.find_one({"ad_id": "a1"}))["paused_at"] == "2026-10-04"
    assert (await mock_db.meta_ad_state.find_one({"ad_id": "a2"}))["paused_at"] == "2026-09-01"
    assert (await mock_db.meta_ad_state.find_one({"ad_id": "a4"}))["paused_at"] is None


async def test_state_indexes_created(mock_mongo_client, mock_db):
    await create_agent_indexes(mock_mongo_client)
    for coll in ("meta_ad_state", "meta_adset_state"):
        infos = await mock_db[coll].index_information()
        assert any(i.get("unique") for i in infos.values()), coll


async def test_ad_days_indexes_created(mock_mongo_client, mock_db):
    await create_agent_indexes(mock_mongo_client)
    infos = (await mock_db[AD_DAYS].index_information()).values()
    keys = {tuple(k for k, _ in i["key"]): bool(i.get("unique")) for i in infos}
    assert keys[("user_id", "client_group_id", "ad_id", "date_start")] is True
    assert ("client_group_id", "ad_id", "date_start") in keys
