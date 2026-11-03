import asyncio
import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routers.creatives as creatives_router
from dependencies import get_current_user
from integrations.facebook_utils.meta_creative_writer import MetaWriteError
from main import app
from services.creatives import posting
from services.creatives.store import (
    POST_JOBS, TEST_ADS, TESTS, create_test, get_test, list_test_ads,
)
from services.meta_status_service import MetaStatusError

U = "u1@example.com"
NOW = datetime(2026, 11, 3, 12, 0)
BLOB = "https://abc.public.blob.vercel-storage.com/img.png"
IMAGE = {"format": "image", "media": {"blob_url": BLOB, "mime": "image/png"},
         "primary_text": "Hi", "headline": "H", "description": "D", "cta_type": "SIGN_UP",
         "destination": "lead_form", "website_url": "https://example.com"}


def accounts(n=3):
    return [{"ad_account_id": f"act_{i}", "client_group_id": f"g{i}", "adset_id": f"as{i}",
             "adset_name": f"Ad set {i}", "page_id": f"p{i}", "lead_form_id": f"lf{i}"}
            for i in range(n)]


class Meta:
    """Programmable fake of the writer + status functions, patched into posting."""

    def __init__(self, monkeypatch):
        self.calls = {k: [] for k in ("upload_image", "upload_video", "video_ready",
                                      "create_adcreative", "create_ad", "status")}
        self.err = {}          # (fn, act) -> exception, raised once
        self.ready = True
        self.status_err = {}   # ad_id -> exception

        async def upload_image(act, token, *, image_url, allow_meta_cdn=False):
            return await self._hit("upload_image", act, f"hash_{act}", image_url=image_url,
                                   allow_meta_cdn=allow_meta_cdn)

        async def upload_video(act, token, *, file_url, name):
            return await self._hit("upload_video", act, f"vid_{act}", file_url=file_url)

        async def video_ready(video_id, token):
            self.calls["video_ready"].append(video_id)
            await asyncio.sleep(0)
            return self.ready

        async def create_adcreative(act, token, **kw):
            return await self._hit("create_adcreative", act, f"cr_{act}", **kw)

        async def create_ad(act, token, *, name, adset_id, creative_id):
            return await self._hit("create_ad", act, f"ad_{act}", name=name, adset_id=adset_id,
                                   creative_id=creative_id)

        async def set_status(user_id, object_id, object_type, status, mongo_client):
            await asyncio.sleep(0)
            self.calls["status"].append((object_id, status))
            if object_id in self.status_err:
                raise self.status_err[object_id]
            return {"success": True}

        async def token(uid, client):
            return {"access_token": "tok"}

        for name, fn in (("upload_image", upload_image), ("upload_video", upload_video),
                         ("video_ready", video_ready), ("create_adcreative", create_adcreative),
                         ("create_ad", create_ad), ("set_object_status", set_status),
                         ("get_facebook_token", token)):
            monkeypatch.setattr(posting, name, fn)

    hook = None  # async (fn, act) -> None, run inside every writer call

    async def _hit(self, fn, act, result, **kw):
        self.calls[fn].append((act, kw))
        await asyncio.sleep(0)
        if self.hook:
            await self.hook(fn, act)
        e = self.err.pop((fn, act), None)
        if e:
            raise e
        return result


@pytest.fixture
def meta(monkeypatch):
    return Meta(monkeypatch)


async def seed_groups(db, n=3):
    for i in range(n):
        await db["client_groups"].insert_one(
            {"id": f"g{i}", "user_id": U, "name": f"Client {i}", "meta_ad_account_id": f"act_{i}"})


async def make(db, creative=IMAGE, n=3):
    await seed_groups(db, n)
    return await create_test(db, U, name="Summer hook", origin="new", creative=dict(creative),
                             targets={})


async def posted(db, creative=IMAGE, n=3):
    t = await make(db, creative, n)
    await posting.enqueue_post(db, U, t["id"], accounts=accounts(n), now=NOW)
    return t


async def tick(db, mc, minutes=0, **kw):
    # the tick adds real elapsed time to `now` (lease math); freeze the monotonic clock
    kw.setdefault("clock", lambda: 0.0)
    return await posting.run_post_jobs(db, mc, NOW + timedelta(minutes=minutes), **kw)


async def test_happy_path_posts_paused_then_live(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db)
    assert (await get_test(mock_db, U, t["id"]))["status"] == "posting"
    jobs = await mock_db[POST_JOBS].find({"test_id": t["id"]}).to_list(None)
    assert len(jobs) == 3 and all(j["state"] == "queued" and j["attempts"] == 0
                                  and j["lease_until"] is None for j in jobs)
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    ads = await list_test_ads(mock_db, t["id"], user_id=U)
    assert {a["meta"]["ad_id"] for a in ads} == {"ad_act_0", "ad_act_1", "ad_act_2"}
    assert all(name == "CT-0001 | summer-hook" for name in
               (kw["name"] for _, kw in meta.calls["create_ad"]))
    assert all(a["post_state"] == "active" for a in ads)
    assert sorted(meta.calls["status"]) == [(f"ad_act_{i}", "ACTIVE") for i in range(3)]
    got = await get_test(mock_db, U, t["id"])
    assert got["status"] == "testing" and got["started_at"] == NOW + timedelta(minutes=2)
    # lead-form creative carried the per-account form + page
    kw = dict(meta.calls["create_adcreative"][0][1])
    assert kw["lead_form_id"] == "lf0" and kw["page_id"] == "p0" and kw["media_ref"] == "hash_act_0"


async def test_double_post_creates_no_duplicates(mock_db, mock_mongo_client, meta, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(creatives_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: U
    try:
        t = await make(mock_db)
        client = TestClient(app)
        first = client.post(f"/api/creatives/{t['id']}/post", json={"accounts": accounts()})
        second = client.post(f"/api/creatives/{t['id']}/post", json={"accounts": accounts()})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert first.status_code == 200, first.text
    assert second.status_code == 409
    assert await mock_db[TEST_ADS].count_documents({"test_id": t["id"]}) == 3
    assert await mock_db[POST_JOBS].count_documents({"test_id": t["id"]}) == 3

    # overlapping ticks at the same instant: the second sees live leases and skips
    for m in range(3):
        await asyncio.gather(tick(mock_db, mock_mongo_client, m),
                             tick(mock_db, mock_mongo_client, m))
    for fn in ("upload_image", "create_adcreative", "create_ad"):
        assert sorted(a for a, _ in meta.calls[fn]) == ["act_0", "act_1", "act_2"], fn
    # a lease that is still live blocks a later tick too
    await mock_db[POST_JOBS].update_many({}, {"$set": {"state": "queued"}})
    await mock_db[POST_JOBS].update_many(
        {}, {"$set": {"state": "running", "lease_until": NOW + timedelta(minutes=10)}})
    res = await tick(mock_db, mock_mongo_client, 5)
    assert res["claimed"] == 0


async def test_resume_after_crash_skips_done_steps(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    # crash mid-run: image uploaded and stored, lease left behind (expired by now)
    await mock_db[TEST_ADS].update_one({"id": ad["id"]}, {"$set": {"meta.image_hash": "hash_old"}})
    await mock_db[POST_JOBS].update_one(
        {"test_ad_id": ad["id"]}, {"$set": {"state": "running", "lease_until": NOW - timedelta(seconds=1)}})
    await tick(mock_db, mock_mongo_client)
    await tick(mock_db, mock_mongo_client, 1)
    assert meta.calls["upload_image"] == []
    assert meta.calls["create_adcreative"][0][1]["media_ref"] == "hash_old"
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["meta"]["ad_id"] == "ad_act_0" and ad["post_state"] == "active"


async def test_revive_uses_existing_meta_media(mock_db, mock_mongo_client, meta):
    creative = {**IMAGE, "format": "video",
                "media": {"video_id": "v_src", "image_hash": "th_src"}}
    t = await posted(mock_db, creative, n=1)
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    assert meta.calls["upload_video"] == [] and meta.calls["upload_image"] == []
    ref = meta.calls["create_adcreative"][0][1]["media_ref"]
    assert ref == {"video_id": "v_src", "image_hash": "th_src"}
    assert (await get_test(mock_db, U, t["id"]))["status"] == "testing"


async def test_video_waits_until_ready(mock_db, mock_mongo_client, meta):
    creative = {**IMAGE, "format": "video",
                "media": {"blob_url": BLOB.replace("img.png", "v.mp4"), "thumbnail_url": BLOB}}
    t = await posted(mock_db, creative, n=1)
    meta.ready = False
    await tick(mock_db, mock_mongo_client, 0)   # upload
    await tick(mock_db, mock_mongo_client, 1)   # not ready -> released
    job = await mock_db[POST_JOBS].find_one({"test_id": t["id"]})
    assert job["state"] == "queued" and job["attempts"] == 0 and meta.calls["create_adcreative"] == []
    meta.ready = True
    await tick(mock_db, mock_mongo_client, 2)
    await tick(mock_db, mock_mongo_client, 3)
    assert len(meta.calls["upload_video"]) == 1
    assert meta.calls["create_adcreative"][0][1]["media_ref"] == {"video_id": "vid_act_0",
                                                                  "image_url": BLOB}
    assert (await list_test_ads(mock_db, t["id"], user_id=U))[0]["post_state"] == "active"


async def test_partial_failure(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db)
    meta.err[("create_adcreative", "act_1")] = MetaWriteError(
        "Invalid parameter", code=100,
        user_msg="The lead form does not belong to this page", step="create_creative")
    for m in range(4):
        await tick(mock_db, mock_mongo_client, m)
    ads = {a["ad_account_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    bad = ads["act_1"]
    assert bad["post_state"] == "failed"
    assert bad["post_error"]["step"] == "creative" and bad["post_error"]["code"] == 100
    assert bad["post_error"]["fix"] == "use_website_url" and bad["post_error"]["message"]
    assert ads["act_0"]["post_state"] == "created" and ads["act_2"]["post_state"] == "created"
    assert meta.calls["status"] == []
    assert (await get_test(mock_db, U, t["id"]))["status"] == "posting"
    job = await mock_db[POST_JOBS].find_one({"test_ad_id": bad["id"]})
    assert job["state"] == "failed"

    with pytest.raises(posting.PostingError) as e:
        await posting.go_live(mock_db, mock_mongo_client, U, t["id"], now=NOW)
    assert e.value.status_code == 409
    res = await posting.go_live(mock_db, mock_mongo_client, U, t["id"], partial_ok=True, now=NOW)
    assert sorted(meta.calls["status"]) == [("ad_act_0", "ACTIVE"), ("ad_act_2", "ACTIVE")]
    assert res["activated"] == 2 and res["errors"] == []
    got = await get_test(mock_db, U, t["id"])
    assert got["status"] == "testing" and got["started_at"] == NOW


async def test_go_live_records_status_error_per_ad(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=2)
    meta.status_err["ad_act_1"] = MetaStatusError("Meta API error: nope", 400)
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    ads = {a["ad_account_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert ads["act_0"]["post_state"] == "active"
    assert ads["act_1"]["post_state"] == "created"
    assert ads["act_1"]["post_error"]["step"] == "go_live"
    assert (await get_test(mock_db, U, t["id"]))["status"] == "testing"
    # only the original go-live attempt touched Meta; no queue retry of status writes
    assert len(meta.calls["status"]) == 2


async def test_retry_with_website_patch(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    meta.err[("create_adcreative", "act_0")] = MetaWriteError(
        "Invalid parameter", code=100, user_msg="Lead form not owned by page", step="create_creative")
    await tick(mock_db, mock_mongo_client, 0)
    await tick(mock_db, mock_mongo_client, 1)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["post_state"] == "failed"

    await posting.retry(mock_db, U, t["id"], ad["id"], patch={"destination": "website"}, now=NOW)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["post_state"] == "pending" and ad["post_error"] is None
    job = await mock_db[POST_JOBS].find_one({"test_ad_id": ad["id"]})
    assert job["state"] == "queued" and job["attempts"] == 0

    await tick(mock_db, mock_mongo_client, 2)
    await tick(mock_db, mock_mongo_client, 3)
    assert len(meta.calls["upload_image"]) == 1
    kw = meta.calls["create_adcreative"][-1][1]
    assert kw["destination"] == "website" and kw["website_url"] == "https://example.com"
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["post_state"] == "active"
    assert (await get_test(mock_db, U, t["id"]))["status"] == "testing"


async def test_retry_rejects_non_failed_ad(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    with pytest.raises(posting.PostingError) as e:
        await posting.retry(mock_db, U, t["id"], ad["id"], now=NOW)
    assert e.value.status_code == 409


async def test_rate_limit_backoff(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    meta.err[("upload_image", "act_0")] = MetaWriteError("Too many calls", code=17, step="upload_image")
    await tick(mock_db, mock_mongo_client)
    job = await mock_db[POST_JOBS].find_one({"test_id": t["id"]})
    assert job["attempts"] == 1 and job["state"] == "queued"
    assert job["lease_until"] >= NOW + timedelta(seconds=60)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["post_state"] != "failed"
    # still backing off 30s later: not claimed
    assert (await tick(mock_db, mock_mongo_client, 0.5))["claimed"] == 0
    await tick(mock_db, mock_mongo_client, 2)
    assert len(meta.calls["upload_image"]) == 2


async def test_rate_limit_gives_up_after_max_attempts(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    await mock_db[POST_JOBS].update_one({"test_id": t["id"]}, {"$set": {"attempts": 4}})
    meta.err[("upload_image", "act_0")] = MetaWriteError("Too many calls", code=17, step="upload_image")
    await tick(mock_db, mock_mongo_client)
    ad = (await list_test_ads(mock_db, t["id"], user_id=U))[0]
    assert ad["post_state"] == "failed" and ad["post_error"]["fix"] == "retry_later"


async def test_add_accounts_to_live_test_go_live_automatically(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=2)
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    assert (await get_test(mock_db, U, t["id"]))["status"] == "testing"
    await seed_groups(mock_db, 3)
    with pytest.raises(posting.PostingError):  # already-posted accounts are rejected
        await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(1),
                                   add_accounts=True, now=NOW)
    await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(3)[2:],
                               add_accounts=True, now=NOW)
    assert (await get_test(mock_db, U, t["id"]))["status"] == "testing"
    for m in range(3, 6):
        await tick(mock_db, mock_mongo_client, m)
    ads = {a["ad_account_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert ads["act_2"]["post_state"] == "active"
    assert ("ad_act_2", "ACTIVE") in meta.calls["status"] and len(meta.calls["status"]) == 3


async def test_enqueue_validation(mock_db, mock_mongo_client, meta):
    t = await make(mock_db)
    with pytest.raises(posting.PostingError) as e:  # account not owned by the user
        await posting.enqueue_post(mock_db, U, t["id"], accounts=[
            {**accounts(1)[0], "ad_account_id": "act_x"}], now=NOW)
    assert e.value.status_code == 400
    with pytest.raises(posting.PostingError) as e:
        await posting.enqueue_post(mock_db, U, t["id"], accounts=[], now=NOW)
    assert e.value.status_code == 400
    with pytest.raises(posting.PostingError) as e:  # add-accounts needs a posted test
        await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(1),
                                   add_accounts=True, now=NOW)
    assert e.value.status_code == 409
    assert (await get_test(mock_db, U, t["id"]))["status"] == "draft"
    assert await mock_db[TEST_ADS].count_documents({}) == 0


async def test_pause_ads(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=2)
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    res = await posting.pause_ads(mock_db, mock_mongo_client, U, t["id"], ["act_1"], now=NOW)
    assert res["paused"] == 1 and ("ad_act_1", "PAUSED") in meta.calls["status"]
    ads = {a["ad_account_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert ads["act_1"]["meta_status"] == "PAUSED" and ads["act_1"]["post_state"] == "active"
    assert ads["act_0"].get("meta_status") != "PAUSED"


async def test_routes_owner_scoped_and_wired(mock_db, mock_mongo_client, meta, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(creatives_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    try:
        t = await make(mock_db, n=1)
        client = TestClient(app)
        assert client.post(f"/api/creatives/{t['id']}/post",
                           json={"accounts": accounts(1)}).status_code == 200
        r = client.post(f"/api/creatives/{t['id']}/go-live", json={})
        assert r.status_code == 409  # nothing created yet
        await tick(mock_db, mock_mongo_client, 0)
        state["user"] = "other@example.com"
        for r in (client.post(f"/api/creatives/{t['id']}/go-live", json={}),
                  client.post(f"/api/creatives/{t['id']}/pause", json={}),
                  client.post(f"/api/creatives/{t['id']}/accounts", json={"accounts": accounts(1)}),
                  client.post(f"/api/creatives/{t['id']}/ads/x/retry", json={})):
            assert r.status_code == 404
        state["user"] = U
        assert client.post(f"/api/creatives/{t['id']}/ads/nope/retry", json={}).status_code == 404
        assert client.post(f"/api/creatives/{t['id']}/pause", json={}).status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_tick_runs_post_jobs_first(mock_db, mock_mongo_client, monkeypatch):
    from jobs import creative_tracking as ct

    order = []

    async def fake_post(db, mc, now_utc, **kw):
        order.append("post")
        return {"claimed": 0}

    async def fake_track(db, mc, now_utc, **kw):
        order.append("track")
        return {"tracked": 0}

    monkeypatch.setattr(ct, "run_post_jobs", fake_post)
    monkeypatch.setattr(ct, "track_due_tests", fake_track)
    res = await ct.run_creatives_tick(datetime(2026, 11, 3, 9))
    assert order[:2] == ["post", "track"] and res["posting"] == {"claimed": 0}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_late_claim_gets_full_lease_and_stale_worker_is_noop(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=2)
    clock = Clock()
    seen = {}

    async def hook(fn, act):
        if act == "act_0":
            clock.t += 120  # the first job's step takes two minutes
        elif act == "act_1" and "inner" not in seen:
            job = await mock_db[POST_JOBS].find_one({"test_id": t["id"], "lease_id": {"$ne": None}})
            seen["job"] = job
            assert job["lease_until"] == NOW + timedelta(seconds=120 + 90)
            meta.hook = None
            # an overlapping tick at real time (NOW + 150s) while act_1 is mid-step
            seen["inner"] = await posting.run_post_jobs(mock_db, mock_mongo_client,
                                                        NOW + timedelta(seconds=150))

    meta.hook = hook
    await posting.run_post_jobs(mock_db, mock_mongo_client, NOW, clock=clock)
    assert [a for a, _ in meta.calls["upload_image"]].count("act_1") == 1
    assert seen["inner"]["claimed"] == 1  # only the released act_0 job
    # a worker whose claim was taken over cannot overwrite the job
    stale = {**seen["job"], "lease_id": "someone-else"}
    assert await posting._finish(mock_db, stale, NOW, state="failed") is False
    job = await mock_db[POST_JOBS].find_one({"id": stale["id"]})
    assert job["state"] == "queued"


async def test_posting_deadline_stops_claiming(mock_db, mock_mongo_client, meta):
    await posted(mock_db, n=4)
    clock = Clock()

    async def hook(fn, act):
        clock.t += 10

    meta.hook = hook
    res = await posting.run_post_jobs(mock_db, mock_mongo_client, NOW, clock=clock,
                                      deadline=clock.t + 25)
    assert res["claimed"] == 3  # claimed at +0, +10, +20; +30 is past the slice
    res = await posting.run_post_jobs(mock_db, mock_mongo_client, NOW + timedelta(minutes=1),
                                      clock=clock, deadline=clock.t - 1)
    assert res["claimed"] == 0


async def test_one_video_upload_per_tick(mock_db, mock_mongo_client, meta):
    creative = {**IMAGE, "format": "video",
                "media": {"blob_url": BLOB.replace("img.png", "v.mp4"), "thumbnail_url": BLOB}}
    t = await posted(mock_db, creative, n=2)
    res = await tick(mock_db, mock_mongo_client, 0)
    assert len(meta.calls["upload_video"]) == 1 and res["deferred"] == 1
    jobs = await mock_db[POST_JOBS].find({"test_id": t["id"]}).to_list(None)
    assert all(j["state"] == "queued" and j["lease_until"] is None for j in jobs)
    await tick(mock_db, mock_mongo_client, 1)
    assert len(meta.calls["upload_video"]) == 2


async def test_tick_gives_posting_a_deadline(mock_db, mock_mongo_client, monkeypatch):
    import time as _time
    from jobs import creative_tracking as ct

    got = {}

    async def fake_post(db, mc, now_utc, **kw):
        got.update(kw)
        return {"claimed": 0}

    async def fake_track(db, mc, now_utc, *, deadline=None):
        got["track_deadline"] = deadline
        return {"tracked": 0}

    monkeypatch.setattr(ct, "run_post_jobs", fake_post)
    monkeypatch.setattr(ct, "track_due_tests", fake_track)
    before = _time.monotonic()
    await ct.run_creatives_tick(datetime(2026, 11, 3, 9))
    assert before + 24 <= got["deadline"] <= _time.monotonic() + 25
    assert got["track_deadline"] >= got["deadline"] + 19


REVIVE_IMG = {**IMAGE, "media": {"image_hash": "src_hash",
                                 "image_url": "https://scontent-lhr8-1.xx.fbcdn.net/v/t45/a.jpg"}}


async def test_cross_account_image_revive_reuploads_from_meta_cdn(mock_db, mock_mongo_client, meta):
    t = await make(mock_db, REVIVE_IMG, n=2)
    await mock_db[TESTS].update_one({"id": t["id"]}, {"$set": {"source": {"ad_account_id": "0"}}})
    await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(2), now=NOW)
    await tick(mock_db, mock_mongo_client)
    ups = {a: kw for a, kw in meta.calls["upload_image"]}
    assert "act_0" not in ups  # "0" == "act_0": same account reuses the hash
    assert ups["act_1"] == {"image_url": REVIVE_IMG["media"]["image_url"], "allow_meta_cdn": True}
    ads = {a["ad_account_id"]: a for a in await list_test_ads(mock_db, t["id"], user_id=U)}
    assert ads["act_0"]["meta"]["image_hash"] == "src_hash"


async def test_odd_source_is_tolerated(mock_db, mock_mongo_client, meta):
    t = await make(mock_db, REVIVE_IMG, n=1)
    await mock_db[TESTS].update_one({"id": t["id"]}, {"$set": {"source": "weird"}})
    await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(1), now=NOW)
    await tick(mock_db, mock_mongo_client)
    assert meta.calls["upload_image"] == []


async def test_cross_account_video_revive_without_blob_rejected(mock_db, mock_mongo_client, meta):
    creative = {**IMAGE, "format": "video", "media": {"video_id": "v_src", "image_hash": "th"}}
    t = await make(mock_db, creative, n=2)
    await mock_db[TESTS].update_one({"id": t["id"]},
                                    {"$set": {"source": {"ad_account_id": "act_0"}}})
    with pytest.raises(posting.PostingError) as e:
        await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(2), now=NOW)
    assert e.value.status_code == 400 and "Upload the video file" in e.value.message
    assert (await get_test(mock_db, U, t["id"]))["status"] == "draft"
    await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(1), now=NOW)  # same account ok


async def test_pause_wins_over_go_live(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=2)
    meta.err[("create_adcreative", "act_1")] = MetaWriteError("x", code=100, step="create_creative")
    for m in range(3):
        await tick(mock_db, mock_mongo_client, m)
    await posting.pause_ads(mock_db, mock_mongo_client, U, t["id"], ["act_0"], now=NOW)
    res = await posting.go_live(mock_db, mock_mongo_client, U, t["id"], partial_ok=True, now=NOW)
    assert res["activated"] == 0 and ("ad_act_0", "ACTIVE") not in meta.calls["status"]


async def test_enqueue_rolls_back_on_insert_failure(mock_db, mock_mongo_client, meta, monkeypatch):
    t = await make(mock_db, n=2)
    real = posting.add_test_ads

    async def boom(*a, **kw):
        await real(*a, **kw)
        raise RuntimeError("mongo down")

    monkeypatch.setattr(posting, "add_test_ads", boom)
    with pytest.raises(RuntimeError):
        await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(2), now=NOW)
    got = await get_test(mock_db, U, t["id"])
    assert got["status"] == "draft" and got.get("posted_accounts") == []
    assert await mock_db[TEST_ADS].count_documents({}) == 0
    monkeypatch.setattr(posting, "add_test_ads", real)
    res = await posting.enqueue_post(mock_db, U, t["id"], accounts=accounts(2), now=NOW)
    assert res["queued"] == 2


async def test_post_jobs_writes_are_owner_scoped(mock_db, mock_mongo_client, meta):
    t = await posted(mock_db, n=1)
    # a job doc for the same id but another user must not be touched by the worker
    job = await mock_db[POST_JOBS].find_one({"test_id": t["id"]})
    await mock_db[POST_JOBS].insert_one({**{k: v for k, v in job.items() if k != "_id"},
                                         "user_id": "other", "state": "done"})
    await tick(mock_db, mock_mongo_client)
    other = await mock_db[POST_JOBS].find_one({"id": job["id"], "user_id": "other"})
    assert other["state"] == "done" and other.get("lease_id") is None
