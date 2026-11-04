import json
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
    assert ad_calls[0][1]["fields"] == meta_state._AD_FIELDS
    assert "ARCHIVED" in json.loads(ad_calls[0][1]["effective_status"])  # Meta omits archived ads by default
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


async def test_refresh_stores_issues_review_feedback_and_creative(mock_db, mock_mongo_client, graph, monkeypatch):
    ad = {**_ad("WITH_ISSUES"),
          "issues_info": [{"error_summary": "Pixel", "error_message": "Pixel missing"}],
          "ad_review_feedback": {"global": {"reason": "text"}},
          "creative": {"id": "c9", "image_hash": "h1", "body": "top body", "title": "top title",
                       "object_story_spec": {"page_id": "p1", "link_data": {
                           "link": "https://x.test", "message": "msg", "name": "nm",
                           "call_to_action": {"value": {"lead_gen_form_id": "f1"}}}}}}

    async def fake_get(self, url, params=None, **kw):
        req = httpx.Request("GET", str(url))
        data = [ad] if "/ads" in str(url) and "/adsets" not in str(url) else []
        return httpx.Response(200, json={"data": data}, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 1))
    doc = await mock_db.meta_ad_state.find_one({"ad_id": "a1"})
    assert doc["issues"] == ad["issues_info"] and doc["review_feedback"] == ad["ad_review_feedback"]
    assert doc["creative"] == {"id": "c9", "image_hash": "h1", "video_id": None, "page_id": "p1",
                               "lead_form_id": "f1", "body": "msg", "title": "nm", "link": "https://x.test"}


async def test_refresh_without_creative_stores_empty_fields(mock_db, mock_mongo_client, graph):
    await refresh_meta_state(mock_db, mock_mongo_client, "u", GID, "act_1", today=date(2026, 10, 1))
    doc = await mock_db.meta_ad_state.find_one({"ad_id": "a1"})
    assert doc["issues"] == [] and doc["review_feedback"] == {}
    assert doc["creative"]["id"] is None and doc["creative"]["link"] is None


def _too_much():
    return {"error": {"code": 1, "message": "Please reduce the amount of data you're asking for, then retry your request"}}


async def test_fetch_all_halves_limit_on_reduce_data_error(monkeypatch):
    limits = []

    async def fake_get(self, url, params=None, **kw):
        limit = (params or {}).get("limit")
        limits.append(limit)
        req = httpx.Request("GET", str(url))
        if limit and limit > 50:
            return httpx.Response(500, json=_too_much(), request=req)
        return httpx.Response(200, json={"data": [{"id": "a"}]}, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as c:
        out = await meta_state._fetch_all(c, "act_1/ads", "id", "tok")
    assert out == [{"id": "a"}] and limits == [200, 100, 50]


async def test_fetch_all_rewrites_limit_in_next_url(monkeypatch):
    urls = []

    async def fake_get(self, url, params=None, **kw):
        urls.append(str(url))
        req = httpx.Request("GET", str(url))
        if "page2" in str(url) and "limit=200" in str(url):
            return httpx.Response(400, json=_too_much(), request=req)
        if "page2" in str(url):
            return httpx.Response(200, json={"data": [{"id": "b"}]}, request=req)
        return httpx.Response(200, json={"data": [{"id": "a"}], "paging": {
            "next": "https://graph.facebook.com/v25.0/act_1/ads?page2=1&limit=200&after=x"}}, request=req)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as c:
        out = await meta_state._fetch_all(c, "act_1/ads", "id", "tok")
    assert [o["id"] for o in out] == ["a", "b"]
    assert "limit=100" in urls[-1] and "after=x" in urls[-1]


async def test_fetch_all_gives_up_at_25(monkeypatch):
    limits = []

    async def fake_get(self, url, params=None, **kw):
        limits.append((params or {}).get("limit"))
        return httpx.Response(500, json=_too_much(), request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    async with httpx.AsyncClient() as c:
        with pytest.raises(RuntimeError):
            await meta_state._fetch_all(c, "act_1/ads", "id", "tok")
    assert limits == [200, 100, 50, 25]


def _limited(monkeypatch, code, subcode=None, headers=None):
    async def fake_get(self, url, params=None, **kw):
        err = {"code": code, "message": "slow", **({"error_subcode": subcode} if subcode else {})}
        return httpx.Response(400, json={"error": err}, headers=headers or {}, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def _limit_from_fetch():
    async with httpx.AsyncClient() as c:
        with pytest.raises(meta_state.MetaRateLimit) as e:
            await meta_state._fetch_all(c, "act_1/ads", "id", "tok")
    return e.value


@pytest.mark.parametrize("code,subcode,scope", [
    (80000, None, "account"), (80003, None, "account"), (80004, None, "account"), (80014, None, "account"),
    (80000, 2446079, "account"), (17, 2446079, "account"),
    (4, None, "app"), (17, None, "app"), (32, None, "app"), (613, None, "app")])
async def test_fetch_all_gives_each_rate_limit_code_its_scope(monkeypatch, code, subcode, scope):
    _limited(monkeypatch, code, subcode)
    assert (await _limit_from_fetch()).scope == scope


async def test_fetch_all_other_errors_are_not_rate_limits(monkeypatch):
    _limited(monkeypatch, 100)
    async with httpx.AsyncClient() as c:
        with pytest.raises(RuntimeError) as e:
            await meta_state._fetch_all(c, "act_1/ads", "id", "tok")
    assert not isinstance(e.value, meta_state.MetaRateLimit)


@pytest.mark.parametrize("header,seconds", [
    (json.dumps({"1": [{"type": "ads_management", "estimated_time_to_regain_access": 19}]}), 19 * 60),
    (json.dumps({"1": [{"estimated_time_to_regain_access": 2}], "2": [{"estimated_time_to_regain_access": 7},
                                                                     {"estimated_time_to_regain_access": 0}]}), 7 * 60),
    (json.dumps({"1": [{"estimated_time_to_regain_access": 0}]}), None),
    (json.dumps({"1": [{"type": "ads_management"}]}), None),
    (json.dumps(["odd"]), None), ("not json", None), (None, None)])
async def test_fetch_all_reads_the_time_to_regain_access_in_minutes(monkeypatch, header, seconds):
    _limited(monkeypatch, 80004, headers={"X-Business-Use-Case-Usage": header} if header else None)
    assert (await _limit_from_fetch()).retry_after_s == seconds


def test_rate_limit_defaults_to_the_wider_scope():
    e = meta_state.MetaRateLimit("slow")
    assert e.scope == "app" and e.retry_after_s is None
