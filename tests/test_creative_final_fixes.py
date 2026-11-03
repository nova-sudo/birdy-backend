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
async def _patches(monkeypatch, mock_db):
    # The user's currency comes from the app db (users.default_currency).
    await mock_db["users"].insert_one({"user_id": "u1", "default_currency": "GBP"})

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


# ── I2: a video with no thumbnail takes Meta's own ──────────────────────────

BLOB_VIDEO = "https://abc.public.blob.vercel-storage.com/clip.mp4"
# What the wizard's MediaDrop stores for a video (lib/creative-upload.js): no thumbnail_url.
FE_VIDEO_MEDIA = {"blob_url": BLOB_VIDEO, "mime": "video/mp4", "bytes": 4_200_000,
                  "width": 1080, "height": 1920, "duration_s": 14.2}
VIDEO = {"format": "video", "media": FE_VIDEO_MEDIA, "primary_text": "Hi", "headline": "H",
         "description": "D", "cta_type": "LEARN_MORE", "destination": "website",
         "website_url": "https://example.com"}
THUMB_A = "https://scontent-lhr8-1.xx.fbcdn.net/v/t15.5256-10/a_n.jpg?_nc_cat=1&oh=x&oe=y"
THUMB_B = "https://scontent-lhr8-1.xx.fbcdn.net/v/t15.5256-10/b_n.jpg?_nc_cat=1&oh=x&oe=y"


def graph(monkeypatch, thumbnails, picture=None):
    """Recorded Graph v25.0 responses for the video posting path, routed by path."""
    from urllib.parse import parse_qs, urlparse

    import httpx

    seen = []

    def handler(request):
        url = urlparse(str(request.url))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        body = {k: v[0] for k, v in parse_qs(request.content.decode()).items()} if request.content else {}
        seen.append((request.method, url.path, q, body))
        path = url.path.removeprefix("/v25.0")
        if path == "/act_0/advideos":
            return httpx.Response(200, json={"id": "1209384756"})
        if path == "/1209384756" and q.get("fields") == "status":
            return httpx.Response(200, json={"status": {"video_status": "ready",
                                                        "processing_progress": 100},
                                             "id": "1209384756"})
        if path == "/1209384756/thumbnails":
            return httpx.Response(200, json={"data": thumbnails, "paging": {
                "cursors": {"before": "QVFI", "after": "QVFJ"}}})
        if path == "/1209384756" and q.get("fields") == "picture":
            return httpx.Response(200, json={"picture": picture, "id": "1209384756"})
        if path == "/act_0/adcreatives":
            return httpx.Response(200, json={"id": "120210000000001"})
        if path == "/act_0/ads":
            return httpx.Response(200, json={"id": "120210000000002"})
        return httpx.Response(400, json={"error": {"message": f"unexpected {path}", "code": 100}})

    real = httpx.AsyncClient
    monkeypatch.setattr("integrations.facebook_utils.meta_creative_writer.httpx.AsyncClient",
                        lambda *a, **kw: real(transport=httpx.MockTransport(handler),
                                              **{k: v for k, v in kw.items() if k != "timeout"}))

    async def token(uid, client):
        return {"access_token": "tok"}

    monkeypatch.setattr(posting, "get_facebook_token", token)
    Status(monkeypatch)
    return seen


async def _post_video(db, mc, creative=VIDEO):
    await db["client_groups"].insert_one({"id": "g0", "user_id": U, "name": "C",
                                          "meta_ad_account_id": "act_0"})
    t = await create_test(db, U, name="Clip", origin="new", creative=creative, targets={})
    await posting.enqueue_post(db, U, t["id"], accounts=[
        {"ad_account_id": "act_0", "client_group_id": "g0", "adset_id": "as0", "page_id": "p0"}],
        now=datetime(2026, 11, 3, 12))
    for m in range(4):
        await posting.run_post_jobs(db, mc, datetime(2026, 11, 3, 12, m), clock=lambda: 0.0)
    return t


def _spec(seen):
    import json as _json
    [body] = [b for meth, path, _, b in seen if path.endswith("/adcreatives")]
    return _json.loads(body["object_story_spec"])


async def test_video_without_thumbnail_uses_preferred_meta_thumbnail(mock_db, mock_mongo_client, monkeypatch):
    seen = graph(monkeypatch, [
        {"id": "10161", "uri": THUMB_A, "is_preferred": False},
        {"id": "10162", "uri": THUMB_B, "is_preferred": True}])
    t = await _post_video(mock_db, mock_mongo_client)
    video = _spec(seen)["video_data"]
    assert video["video_id"] == "1209384756" and video["image_url"] == THUMB_B
    [ad] = await list_test_ads(mock_db, t["id"], user_id=U)
    assert ad["post_state"] == "active" and ad["meta"]["thumbnail_url"] == THUMB_B
    q = next(q for _, path, q, _ in seen if path.endswith("/thumbnails"))
    assert q["fields"] == "uri,is_preferred"


async def test_video_thumbnail_falls_back_to_first_then_picture(mock_db, mock_mongo_client, monkeypatch):
    seen = graph(monkeypatch, [{"id": "1", "uri": THUMB_A, "is_preferred": False},
                               {"id": "2", "uri": THUMB_B, "is_preferred": False}])
    await _post_video(mock_db, mock_mongo_client)
    assert _spec(seen)["video_data"]["image_url"] == THUMB_A


async def test_video_thumbnail_picture_fallback(mock_db, mock_mongo_client, monkeypatch):
    seen = graph(monkeypatch, [], picture=THUMB_B)
    await _post_video(mock_db, mock_mongo_client)
    assert _spec(seen)["video_data"]["image_url"] == THUMB_B


async def test_uploaded_thumbnail_wins_over_meta(mock_db, mock_mongo_client, monkeypatch):
    own = "https://abc.public.blob.vercel-storage.com/cover.png"
    seen = graph(monkeypatch, [{"id": "1", "uri": THUMB_A, "is_preferred": True}])
    await _post_video(mock_db, mock_mongo_client,
                      {**VIDEO, "media": {**FE_VIDEO_MEDIA, "thumbnail_url": own}})
    assert _spec(seen)["video_data"]["image_url"] == own
    assert not any(path.endswith("/thumbnails") for _, path, _, _ in seen)


async def test_same_account_revive_video_without_thumbnail_can_post():
    test = {"creative": {"format": "video", "media": {"video_id": "v1"}},
            "source": {"ad_account_id": "act_0"}}
    assert posting.media_plan(test, "act_0") == ("reuse", "v1")


# ── M6: a lease-expiry reclaim counts as an attempt ─────────────────────────

async def test_lease_reclaim_counts_attempts_and_gives_up(mock_db, mock_mongo_client, monkeypatch):
    uploads = []

    async def upload_video(act, token, *, file_url, name):
        uploads.append(act)
        raise _Killed()  # the worker dies mid-upload; the job stays RUNNING under its lease

    async def token(uid, client):
        return {"access_token": "tok"}

    monkeypatch.setattr(posting, "upload_video", upload_video)
    monkeypatch.setattr(posting, "get_facebook_token", token)
    await mock_db["client_groups"].insert_one({"id": "g0", "user_id": U, "name": "C",
                                               "meta_ad_account_id": "act_0"})
    t = await create_test(mock_db, U, name="Clip", origin="new", creative=VIDEO, targets={})
    await posting.enqueue_post(mock_db, U, t["id"], accounts=[
        {"ad_account_id": "act_0", "client_group_id": "g0", "adset_id": "as0", "page_id": "p0"}],
        now=datetime(2026, 11, 3, 12))
    now = datetime(2026, 11, 3, 12)
    for _ in range(posting.MAX_ATTEMPTS + 3):
        try:
            await posting.run_post_jobs(mock_db, mock_mongo_client, now, clock=lambda: 0.0)
        except _Killed:
            pass
        now += posting.VIDEO_UPLOAD_LEASE + timedelta(seconds=1)
    job = await mock_db[store.POST_JOBS].find_one({})
    assert job["state"] == "failed"
    assert len(uploads) == posting.MAX_ATTEMPTS
    [ad] = await list_test_ads(mock_db, t["id"], user_id=U)
    assert ad["post_state"] == "failed" and "stalled" in ad["post_error"]["message"]


class _Killed(BaseException):
    """Stands in for the process dying: not an Exception, so nothing in posting catches it."""


# ── I5: targets are in the user's currency ──────────────────────────────────

RATES = {("JPY", "GBP"): 0.005, ("GBP", "GBP"): 1.0}


@pytest.fixture
def jpy(monkeypatch):
    from utils.currency_exchange import CurrencyService

    def rate(a, b):
        if a == b:
            return 1.0
        if (a, b) not in RATES:
            raise ValueError(f"no rate {a}->{b}")
        return RATES[(a, b)]

    monkeypatch.setattr(CurrencyService, "get_rate", staticmethod(rate))


async def test_jpy_account_scored_in_user_currency(mock_db, mock_mongo_client, monkeypatch, jpy):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=2, currencies={1: "JPY"})
    m.leads = {"ad0": 10, "ad1": 10}
    m.spend = {"ad0": 150, "ad1": 30000}   # ad1: ¥30,000 = £150, CPL ¥3,000 = £15
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    got = await rows(mock_db, t)
    jp = got["ad1"]
    assert jp["score"]["state"] == "pass"           # £15 <= £35 (¥3,000 would fail)
    assert jp["score"]["spend"] == pytest.approx(150) and jp["score"]["cpl"] == pytest.approx(15)
    assert jp["currency"] == "GBP" and jp["account_currency"] == "JPY"
    assert jp["fx_rate"] == pytest.approx(0.005)
    summary = (await get_test(mock_db, U, t["id"]))["summary"]
    assert summary["spend"] == pytest.approx(300) and summary["currency"] == "GBP"
    assert summary["mixed_currency"] is True and summary["rate_missing"] is False


async def test_min_spend_floor_is_in_user_currency(mock_db, mock_mongo_client, monkeypatch, jpy):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=1, currencies={0: "JPY"})
    m.leads = {"ad0": 10}
    m.spend = {"ad0": 20000}   # ¥20,000 = £100 < £150, one day live: still scoring
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    assert (await rows(mock_db, t))["ad0"]["score"]["state"] == "scoring"


async def test_group_cpl_and_override_convert_from_account_currency(mock_db, mock_mongo_client,
                                                                   monkeypatch, jpy):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=2, currencies={0: "JPY", 1: "JPY"})
    # g0's own CPL target ¥2,000 (= £10); ad1 has a per-account override ¥4,000 (= £20)
    await mock_db["client_groups"].update_one({"id": "g0"}, {"$set": {"targets": {"cpl": 2000}}})
    await mock_db[TEST_ADS].update_one({"meta.ad_id": "ad1"}, {"$set": {"targets_override": {"cpl": 4000}}})
    m.leads = {"ad0": 10, "ad1": 10}
    m.spend = {"ad0": 30000, "ad1": 30000}  # both CPL £15
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    got = await rows(mock_db, t)
    assert got["ad0"]["score"]["state"] == "fail"    # £15 > £10
    assert got["ad0"]["score"]["checks"][0]["target"] == pytest.approx(10)
    assert got["ad1"]["score"]["state"] == "pass"    # £15 <= £20
    assert got["ad1"]["score"]["checks"][0]["target"] == pytest.approx(20)


async def test_missing_rate_never_scores(mock_db, mock_mongo_client, monkeypatch, jpy):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=1, currencies={0: "XYZ"})
    m.leads = {"ad0": 1}
    m.spend = {"ad0": 99999}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    row = (await rows(mock_db, t))["ad0"]
    assert row["score"]["state"] == "scoring" and row["score"]["rate_missing"] is True
    assert row["currency"] == "XYZ"


async def test_creative_cpl_alert_uses_converted_values(mock_db, mock_mongo_client, monkeypatch, jpy):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=1, currencies={0: "JPY"})
    m.leads = {"ad0": 10}
    m.spend = {"ad0": 30000}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    alert = {"id": "a1", "user_id": U, "scope": "creative_tests", "creative_test_ids": [t["id"]],
             "condition": {"metric": "creative_cpl", "operator": "gt", "value": 20}}
    res = await alerts.evaluate_creative_alert(alert, mock_db)
    assert res["triggered"] is False and res["current_value"] == pytest.approx(15)


async def test_prefill_converts_client_cpl_to_user_currency(mock_db, jpy):
    from services.creatives.targets import prefill_targets
    await mock_db["client_groups"].insert_one({"id": "gj", "user_id": U, "targets": {"cpl": 4000},
                                               "ad_account_currency": "JPY"})
    out = await prefill_targets(mock_db, U, ["gj"])
    assert out["metrics"]["cpl"]["value"] == pytest.approx(20)
    assert out["per_account"]["gj"]["cpl"] == 4000  # per-account stays in the account's currency


# ── M1: Slack chat never gets the creative write tools ──────────────────────

class _Scripted:
    def __init__(self, responses):
        self.responses, self.seen, self.tools, self.model = list(responses), [], [], "stub"

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3,
                              max_tokens=2048):
        self.seen.append(messages)
        self.tools.append([s["function"]["name"] for s in tools or []])
        return self.responses.pop(0)


def _registry():
    from ai.tools import creative_tools
    from ai.tools.registry import ToolRegistry
    reg = ToolRegistry()
    creative_tools.register_creative_tools(reg)
    return reg


async def test_slack_chat_has_no_creative_write_tools_or_guide(mock_db):
    from ai import conversation_log
    from ai.orchestrator import run_chat
    from ai.prompts.creatives import CREATIVE_TESTS_GUIDE
    from ai.providers.base import ProviderResponse
    from ai.tools.creative_tools import CREATIVE_READ_TOOLS, CREATIVE_WRITE_TOOLS

    provider = _Scripted([ProviderResponse(content="hi")])
    await run_chat(provider=provider, tool_registry=_registry(), db=mock_db, user_id=U,
                   message="hello", page=None, source=conversation_log.SOURCE_SLACK)
    assert CREATIVE_TESTS_GUIDE not in provider.seen[0][0]["content"]
    assert not set(CREATIVE_WRITE_TOOLS) & set(provider.tools[0])
    assert set(CREATIVE_READ_TOOLS) <= set(provider.tools[0])

    web = _Scripted([ProviderResponse(content="hi")])
    await run_chat(provider=web, tool_registry=_registry(), db=mock_db, user_id=U,
                   message="hello", page=None)
    assert set(CREATIVE_WRITE_TOOLS) <= set(web.tools[0])


async def test_slack_chat_refuses_a_forced_write_tool_call(mock_db):
    import json

    from ai import conversation_log
    from ai.orchestrator import run_chat
    from ai.providers.base import ProviderResponse, ToolCall
    from ai.tools.registry import ToolRegistry

    called = []

    async def stub(db, user_id, mongo_client=None, **kw):
        called.append(kw)
        return {"ok": True}

    reg = ToolRegistry()
    reg.register(name="post_creative_test", description="s",
                 parameters={"type": "object", "properties": {}}, executor=stub)
    provider = _Scripted([
        ProviderResponse(content=None, finish_reason="tool_calls", tool_calls=[ToolCall(
            id="1", name="post_creative_test", arguments=json.dumps({"confirmed": True}))]),
        ProviderResponse(content="done")])
    await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id=U,
                   message="post it", page=None, source=conversation_log.SOURCE_SLACK)
    assert called == []


# ── M2: auto-pause honours the agent guardrails ─────────────────────────────

async def _loser(db, name="Hook", n=2):
    t = await create_test(db, U, name=name, origin="new", creative={"format": "image"},
                          targets={}, on_verdict={"auto_pause_losers": True})
    await update_test(db, U, t["id"], {"status": "loser", "verdict": "loser",
                                       "summary": {"currency": "GBP", "accounts_total": n}})
    for i in range(n):
        await db["client_groups"].insert_one({"id": f"g{i}", "user_id": U, "name": f"Client {i}",
                                              "meta_ad_account_id": f"act_{i}"})
    await add_test_ads(db, U, t["id"], [
        {"ad_account_id": f"act_{i}", "client_group_id": f"g{i}", "post_state": "active",
         "meta_status": "ACTIVE", "meta": {"ad_id": f"ad{i}"}, "score": {"state": "fail"}}
        for i in range(n)])
    return await get_test(db, U, t["id"])


async def _sync(db, mc, t):
    t = await get_test(db, U, t["id"])
    return await cf.sync_creative_suggestions(db, U, t, await list_test_ads(db, t["id"], user_id=U),
                                              mongo_client=mc)


async def test_auto_pause_blocked_by_protected_tag(mock_db, mock_mongo_client, monkeypatch):
    from ai.suggestions import store as sstore
    st = Status(monkeypatch)
    t = await _loser(mock_db, name="Evergreen hook")  # default protected tag: Evergreen
    await _sync(mock_db, mock_mongo_client, t)
    assert st.calls == []
    [doc] = await mock_db[sstore.SUGGESTIONS].find({"rule_id": "creative_loser"}).to_list(None)
    assert doc["status"] == "open" and doc["blocked_reason"]["code"] == "protected_tag"
    assert len(doc["action"]["targets"]) == 2


async def test_auto_pause_respects_per_client_pause_cap(mock_db, mock_mongo_client, monkeypatch):
    from ai.suggestions import store as sstore
    st = Status(monkeypatch)
    await mock_db["agents"].insert_one({"_id": "ag1", "user_id": U, "type": "media_buying",
                                        "guardrails": {"max_pauses_per_client_day": 1}})
    await mock_db[sstore.ACTIVITY].insert_one({
        "user_id": U, "client_group_id": "g0", "kind": sstore.KIND_ACTION_APPLIED,
        "applied_action_type": "pause_ads", "created_at": cf._utcnow() - timedelta(minutes=5)})
    t = await _loser(mock_db)
    await _sync(mock_db, mock_mongo_client, t)
    assert st.calls == [("ad1", "PAUSED")]           # g0 is at its cap, g1 is not
    [doc] = await mock_db[sstore.SUGGESTIONS].find({"rule_id": "creative_loser"}).to_list(None)
    assert doc["status"] == "open" and doc["blocked_reason"]["code"] == "pause_cap"
    assert [x["object_id"] for x in doc["action"]["targets"]] == ["ad0"]


# ── M3: blob cleanup keeps media another live test still uses ───────────────

async def test_cleanup_skips_urls_still_referenced(mock_db, monkeypatch):
    from datetime import timezone

    from services.creatives import upload_grant as ug

    now = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
    shared = "https://x.public.blob.vercel-storage.com/shared.mp4"
    recent = "https://x.public.blob.vercel-storage.com/recent.png"
    own = "https://x.public.blob.vercel-storage.com/own.png"
    private = "https://x.blob.vercel-storage.com/private.png"  # not a public blob URL
    calls = []

    async def fake_delete(urls):
        calls.append(sorted(urls))

    monkeypatch.setattr(ug, "_delete_blobs", fake_delete)

    async def seed(tid, status, archived_at, media):
        await mock_db[TESTS].insert_one({"id": tid, "user_id": U, "status": status,
                                         "archived_at": archived_at, "creative": {"media": media}})

    await seed("old", "archived", now - timedelta(days=40),
               {"blob_url": shared, "thumbnail_url": own})
    await seed("old2", "archived", now - timedelta(days=40), {"blob_url": recent, "thumbnail_url": private})
    await seed("scaled", "testing", None, {"blob_url": shared})           # still live
    await seed("fresh", "archived", now - timedelta(days=3), {"blob_url": recent})  # archived < 30d
    assert await ug.cleanup_archived_media(mock_db, now) == 2
    assert calls == [[own]]


# ── M11: adopting into a draft creates the verdict alert ────────────────────

async def test_adopt_into_draft_creates_verdict_alert(mock_db, mock_mongo_client, monkeypatch):
    import contextlib

    from fastapi.testclient import TestClient

    import routers.creatives as r
    from dependencies import get_current_user
    from main import app

    t = await create_test(mock_db, U, name="Hook", origin="new", creative={"format": "image"},
                          targets={}, on_verdict={"notify_channels": ["in_app"]})
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": U, "name": "C", "meta_ad_account_id": "act_1",
        "facebook_cache": {"entities": {"ads": [
            {"id": "ad1", "name": "CT-0001 | hook", "adset_id": "as1", "campaign_id": "c1"}]}}})

    @contextlib.asynccontextmanager
    async def client_cm():
        yield mock_mongo_client

    monkeypatch.setattr(r, "get_mongo_client", client_cm)
    app.dependency_overrides[get_current_user] = lambda: U
    try:
        res = TestClient(app).post(f"/api/creatives/{t['id']}/adopt", json={"ad_id": "ad1"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert res.status_code == 200, res.text
    after = await get_test(mock_db, U, t["id"])
    assert after["status"] == "testing"
    alert = await mock_db["alerts"].find_one({"id": after["on_verdict"]["alert_id"]})
    assert alert["creative_test_ids"] == [t["id"]] and alert["condition"]["metric"] == "creative_verdict"


# ── M14: env examples name the creatives settings ───────────────────────────

def test_env_example_lists_creative_settings():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    for key in ("FRONTEND_URL", "CREATIVE_UPLOAD_SECRET", "BLOB_READ_WRITE_TOKEN"):
        assert f"{key}=" in text


# ── I5 follow-up: user currency from the app db, AED through its USD peg ────

ECB = {("USD", "GBP"): 0.8, ("GBP", "USD"): 1.25}


@pytest.fixture
def ecb(monkeypatch):
    """A converter that, like the ECB data, has USD and GBP but no AED."""
    from utils.currency_exchange import CurrencyService

    def rate(a, b):
        if a == b:
            return 1.0
        if (a, b) not in ECB:
            raise ValueError(f"{a} is not a supported currency")
        return ECB[(a, b)]

    monkeypatch.setattr(CurrencyService, "get_rate", staticmethod(rate))


def test_fx_rate_aed_through_usd_peg(ecb):
    from services.creatives.verdict import fx_rate
    assert fx_rate("AED", "GBP") == pytest.approx(0.8 / 3.6725)
    assert fx_rate("GBP", "AED") == pytest.approx(1.25 * 3.6725)
    assert fx_rate("AED", "AED") == 1.0
    assert fx_rate("AED", "USD") == pytest.approx(1 / 3.6725)
    assert fx_rate("XYZ", "GBP") is None
    assert fx_rate("GBP", None) is None


async def test_aed_user_with_aed_and_usd_accounts(mock_db, mock_mongo_client, monkeypatch, ecb):
    await mock_db["users"].update_one({"user_id": U}, {"$set": {"default_currency": "AED"}})
    m = Meta(monkeypatch)
    # Targets in AED: CPL <= 100 AED, floor 150 AED.
    t = await make_test(mock_db, n=2, currencies={0: "AED", 1: "USD"},
                        targets={"mode": "thresholds", "metrics": {"cpl": {"op": "lte", "value": 100}}})
    m.leads = {"ad0": 10, "ad1": 10}
    m.spend = {"ad0": 900, "ad1": 300}  # ad0: 90 AED CPL (pass); ad1: $30 = 110.18 AED CPL (fail)
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    got = await rows(mock_db, t)
    assert got["ad0"]["score"]["state"] == "pass" and got["ad0"]["fx_rate"] == 1.0
    assert got["ad0"]["currency"] == "AED"
    assert got["ad1"]["score"]["cpl"] == pytest.approx(30 * 3.6725)
    assert got["ad1"]["score"]["state"] == "fail" and got["ad1"]["currency"] == "AED"
    summary = (await get_test(mock_db, U, t["id"]))["summary"]
    assert summary["currency"] == "AED" and summary["spend"] == pytest.approx(900 + 300 * 3.6725, abs=0.01)
    assert summary["rate_missing"] is False


async def test_no_user_currency_leaves_accounts_unscored(mock_db, mock_mongo_client, monkeypatch, ecb):
    await mock_db["users"].delete_many({})
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=2)
    m.leads = {"ad0": 10, "ad1": 10}
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    got = await rows(mock_db, t)
    assert all(r["score"]["state"] == "scoring" and r["score"]["rate_missing"] for r in got.values())
    after = await get_test(mock_db, U, t["id"])
    assert after.get("verdict") is None and after["summary"]["currency"] is None


async def test_creative_alerts_skip_unconverted_rows(mock_db, mock_mongo_client, monkeypatch, ecb):
    m = Meta(monkeypatch)
    t = await make_test(mock_db, n=2, currencies={1: "XYZ"})
    m.leads = {"ad0": 10, "ad1": 1}
    m.spend = {"ad0": 100, "ad1": 99999}  # ad1 has no rate: CPL 99999 XYZ, unconverted
    await ct.track_test(mock_db, mock_mongo_client, t, today=TODAY)
    for metric in ("creative_cpl", "creative_ctr"):
        alert = {"id": "a1", "user_id": U, "scope": "creative_tests", "creative_test_ids": [t["id"]],
                 "tracking_mode": "per_client",
                 "condition": {"metric": metric, "operator": "gt", "value": 50}}
        res = await alerts.evaluate_creative_alert(alert, mock_db)
        assert [r["group_id"] for r in res["per_client_results"]] == ["act_0"]
        assert res["triggered"] is False
