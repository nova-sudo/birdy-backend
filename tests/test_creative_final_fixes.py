"""Final whole-branch review fix wave (C1, I1-I6, M1-M14), backend side."""

from datetime import date, datetime, timedelta

import pytest

from ai.suggestions import creative_findings as cf
from jobs import creative_tracking as ct
from services.creatives import alerts, posting, store
from services.creatives.store import (
    TEST_ADS, TESTS, add_test_ads, create_test, get_test, list_test_ads, update_test,
)

U = "u1"
TODAY = date(2026, 11, 3)
TARGETS = {"mode": "thresholds", "metrics": {"cpl": {"op": "lte", "value": 35}}}


def day(ad_id, leads, spend=200, d="2026-10-30", views=None):
    actions = [{"action_type": "lead", "value": str(leads)}]
    if views is not None:
        actions.append({"action_type": "video_view", "value": str(views)})
    return {"ad_id": ad_id, "date_start": d, "spend": str(spend), "impressions": "10000",
            "clicks": "300", "inline_link_clicks": "250", "actions": actions}


@pytest.fixture(autouse=True)
def _patches(monkeypatch):
    from utils.currency_exchange import CurrencyService

    async def cur(_uid):
        return "GBP"

    monkeypatch.setattr(CurrencyService, "get_user_currency", staticmethod(cur))

    async def token(uid, client):
        return {"access_token": "tok"}

    monkeypatch.setattr(ct, "get_facebook_token", token)

    import services.slack_suggestion_notifier as n

    async def quiet(db, user_id, docs):
        return 0

    monkeypatch.setattr(n, "post_new_suggestions", quiet)
    monkeypatch.setattr(n, "update_suggestions", quiet)


class Meta:
    """Fake Meta reads for track_test."""

    def __init__(self, monkeypatch):
        self.leads, self.status, self.spend, self.views = {}, {}, {}, {}
        monkeypatch.setattr(ct, "fetch_tracked_ad_days", self.days)
        monkeypatch.setattr(ct, "fetch_ad_status", self.stat)

        async def acct(*a, **k):
            return []

        monkeypatch.setattr(ct, "fetch_account_daily", acct)

    async def days(self, acct, token, ad_ids, since, until):
        return [day(a, self.leads[a], self.spend.get(a, 200), views=self.views.get(a))
                for a in ad_ids if a in self.leads]

    async def stat(self, ad_id, token):
        return self.status.get(ad_id, "ACTIVE")


class Status:
    def __init__(self, monkeypatch):
        self.calls = []

        async def set_status(user_id, object_id, object_type, status, client):
            self.calls.append((object_id, status))

        monkeypatch.setattr(posting, "set_object_status", set_status)


async def make_test(db, n=3, targets=TARGETS, creative=None, currencies=None, status="testing",
                    on_verdict=None):
    t = await create_test(db, U, name="Hook", origin="new",
                          creative=creative if creative is not None else {"format": "video"},
                          targets=targets, on_verdict=on_verdict)
    await update_test(db, U, t["id"], {"status": status, "started_at": datetime(2026, 10, 28, 9)})
    rows = []
    for i in range(n):
        rows.append({"ad_account_id": f"act_{i}", "client_group_id": f"g{i}", "adset_id": f"as{i}",
                     "post_state": "active", "meta_status": "ACTIVE", "meta": {"ad_id": f"ad{i}"}})
        await db["client_groups"].insert_one({
            "id": f"g{i}", "user_id": U, "name": f"Client {i}", "meta_ad_account_id": f"act_{i}",
            "ad_account_currency": (currencies or {}).get(i, "GBP")})
    await add_test_ads(db, U, t["id"], rows)
    return await get_test(db, U, t["id"])


async def rows(db, t):
    return {a["meta"]["ad_id"]: a for a in await list_test_ads(db, t["id"], user_id=U)}


# ── C1: pause uses Meta's fresh status ──────────────────────────────────────

async def test_track_stores_fresh_effective_status(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"ad0": 10, "ad1": 10, "ad2": 10}
    m.status = {"ad1": "PAUSED", "ad2": "ADSET_PAUSED"}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    got = await rows(mock_db, t)
    assert [got[a]["meta_status"] for a in ("ad0", "ad1", "ad2")] == ["ACTIVE", "PAUSED", "ADSET_PAUSED"]


async def test_reenabled_in_meta_is_paused_again(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    st = Status(monkeypatch)
    t = await make_test(mock_db, n=2)
    await posting.pause_ads(mock_db, mock_mongo_client, U, t["id"], None)
    assert sorted(st.calls) == [("ad0", "PAUSED"), ("ad1", "PAUSED")]
    # Someone sets ad0 live again in Ads Manager; the next track reads ACTIVE.
    m.leads = {"ad0": 1, "ad1": 1}
    m.status = {"ad0": "ACTIVE", "ad1": "PAUSED"}
    await ct.track_test(mock_db, mock_mongo_client, await get_test(mock_db, U, t["id"]), today=TODAY)
    assert (await rows(mock_db, t))["ad0"]["meta_status"] == "ACTIVE"
    st.calls.clear()
    res = await posting.pause_ads(mock_db, mock_mongo_client, U, t["id"], None)
    assert ("ad0", "PAUSED") in st.calls and res["paused"] >= 1


async def test_pause_always_writes_even_if_cached_paused(mock_db, mock_mongo_client, monkeypatch):
    st = Status(monkeypatch)
    t = await make_test(mock_db, n=1)
    await mock_db[TEST_ADS].update_many({}, {"$set": {"meta_status": "PAUSED"}})
    res = await posting.pause_ads(mock_db, mock_mongo_client, U, t["id"], None)
    assert st.calls == [("ad0", "PAUSED")] and res["paused"] == 1


async def test_failing_uses_fresh_status_after_reenable(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=2)
    await mock_db[TEST_ADS].update_many({}, {"$set": {"meta_status": "PAUSED"}})
    m.leads = {"ad0": 1, "ad1": 1}  # both fail CPL 35 (200 spend / 1 lead)
    m.status = {"ad0": "ACTIVE", "ad1": "PAUSED"}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    failing = cf._failing(await list_test_ads(mock_db, t["id"], user_id=U))
    assert [a["meta"]["ad_id"] for a in failing] == ["ad0"]


async def test_auto_pause_includes_reenabled_ad(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    st = Status(monkeypatch)
    t = await make_test(mock_db, n=2, on_verdict={"auto_pause_losers": True})
    await mock_db[TEST_ADS].update_many({}, {"$set": {"meta_status": "PAUSED"}})
    m.leads = {"ad0": 1, "ad1": 1}
    m.status = {"ad0": "ACTIVE", "ad1": "ACTIVE"}  # both re-enabled in Meta
    out = await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    assert out["verdict"] == "loser"
    assert sorted(st.calls) == [("ad0", "PAUSED"), ("ad1", "PAUSED")]


# ── I1: tracking never un-archives ──────────────────────────────────────────

async def test_archive_between_load_and_write_stays_archived(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"ad0": 10, "ad1": 10, "ad2": 10}
    loaded = await get_test(mock_db, U, t["id"])
    await update_test(mock_db, U, t["id"], {"status": "archived"})  # user archives mid-tick
    await ct.track_test(mock_db, mock_mongo_client, loaded, today=TODAY)
    after = await get_test(mock_db, U, t["id"])
    assert after["status"] == "archived" and after.get("verdict") is None
    from ai.suggestions import store as sstore
    assert await mock_db[sstore.SUGGESTIONS].count_documents({}) == 0


async def test_verdict_written_only_for_live_status(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    t = await make_test(mock_db)
    m.leads = {"ad0": 10, "ad1": 10, "ad2": 10}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    assert (await get_test(mock_db, U, t["id"]))["status"] == "winner"


@pytest.mark.parametrize("status", ["archived", "draft"])
async def test_refresh_rejects_archived_and_draft(mock_db, mock_mongo_client, monkeypatch, status):
    import contextlib

    from fastapi.testclient import TestClient

    import routers.creatives as r
    from dependencies import get_current_user
    from main import app

    t = await make_test(mock_db, status=status)

    @contextlib.asynccontextmanager
    async def client_cm():
        yield mock_mongo_client

    monkeypatch.setattr(r, "get_mongo_client", client_cm)
    called = []

    async def fake_track(*a, **k):
        called.append(1)
        return {}

    monkeypatch.setattr(r, "track_test", fake_track)
    app.dependency_overrides[get_current_user] = lambda: U
    try:
        res = TestClient(app).post(f"/api/creatives/{t['id']}/refresh")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert res.status_code == 409 and called == []


# ── M8: refresh maps Meta read errors to 502 ────────────────────────────────

async def test_refresh_meta_error_is_502(mock_db, mock_mongo_client, monkeypatch):
    import contextlib

    from fastapi.testclient import TestClient

    import routers.creatives as r
    from dependencies import get_current_user
    from integrations.facebook_utils.meta_creative_reader import MetaReadError
    from main import app

    t = await make_test(mock_db)

    @contextlib.asynccontextmanager
    async def client_cm():
        yield mock_mongo_client

    async def boom(*a, **k):
        raise MetaReadError("Meta is down", code=2)

    monkeypatch.setattr(r, "get_mongo_client", client_cm)
    monkeypatch.setattr(r, "track_test", boom)
    app.dependency_overrides[get_current_user] = lambda: U
    try:
        res = TestClient(app).post(f"/api/creatives/{t['id']}/refresh")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert res.status_code == 502 and "Meta is down" in res.text


# ── M5: one format default ──────────────────────────────────────────────────

def test_format_default_is_image():
    assert store.creative_format({}) == "image"
    assert store.creative_format({"creative": {"format": None}}) == "image"
    assert store.creative_format({"creative": {"format": "video"}}) == "video"


async def test_track_without_format_treats_creative_as_image(mock_db, mock_mongo_client, monkeypatch):
    m = Meta(monkeypatch)
    targets = {"mode": "thresholds", "metrics": {"hook_rate": {"op": "gte", "value": 50}}}
    t = await make_test(mock_db, n=1, targets=targets, creative={})
    m.leads = {"ad0": 10}
    m.views = {"ad0": 10}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    # hook rate is skipped for images, so no checks: still scoring, never a video fail
    assert (await rows(mock_db, t))["ad0"]["score"]["state"] == "scoring"


# ── M12: one live-status constant ───────────────────────────────────────────

def test_single_live_status_constant():
    assert store.LIVE_STATUSES == ("testing", "winner", "loser", "inconclusive")
    assert posting.LIVE_STATES is store.LIVE_STATUSES
    assert alerts.LIVE_TEST_STATUSES is store.LIVE_STATUSES
    assert cf._LIVE is store.LIVE_STATUSES


# ── M9: post jobs are looked up by test_ad_id ───────────────────────────────

async def test_post_jobs_index_on_test_ad_id(mock_mongo_client):
    from core.database import DB_NAME
    await store.create_creative_indexes(mock_mongo_client)
    info = await mock_mongo_client[DB_NAME][store.POST_JOBS].index_information()
    assert info["test_ad_id"]["key"] == [("test_ad_id", 1)]
