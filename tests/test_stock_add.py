"""Add through the Creatives posting pipeline (spec section 7). This puts new ads into real client
accounts through a pipeline that has never met real Meta, so most of this file is about what must not
happen: a second post, a post past the limit, a hash used in the wrong account. Meta is always a fake:
no test can reach Graph."""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import routers.agent_stock as stock_router
import routers.agents as agents_router
import routers.creatives as creatives_router
from ai.agents import claims, meta_state
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.stock import add as add_mod
from ai.agents.stock import apply as apply_mod
from ai.agents.stock.add import (FILES, clean_ad_name, destination_for, enqueue_add, plan_media, sync_add_lines)
from ai.agents.stock.apply import STUCK_AFTER, apply_card, auto_apply_run, dismiss_card, process_queued_lines, undo_line
from ai.agents.stock.candidates import creative_key, normalise_name
from ai.agents.stock.checks import follow_ups
from ai.agents.stock.scoreboard import OUTCOMES, record_for
from ai.agents.snapshot import AdState
from ai.agents.store import AGENTS, CARDS
from ai.suggestions import creative_findings as cf
from ai.suggestions.store import ACTIVITY, KIND_ACTION_APPLIED, SUGGESTIONS
from dependencies import get_current_user
from integrations.facebook_utils import meta_creative_writer as writer
from integrations.facebook_utils.meta_creative_writer import MetaWriteError
from jobs import creative_tracking
from main import app
from services.creatives import alerts as creative_alerts
from services.creatives import posting, reconcile, upload_grant
from services.creatives import store as creative_store
from services.creatives.store import POST_JOBS, TEST_ADS, TESTS, add_test_ads, create_test, list_tests
from services.meta_status_service import MetaStatusError
from test_stock_apply import FakeMeta, Killed, _agent, _card, _get, _line, _ln, _run
from test_stock_runner import USER

GID, SRC = "g1", "g2"
DEST_ACT, SRC_ACT = "act_g1", "act_g2"
KEY = "name:bs - timelapse - win"
STORE = "storeabc"
TOKEN = f"vercel_blob_rw_{STORE}_secretpart"
OWN_BLOB_HOST = f"https://{STORE}.public.blob.vercel-storage.com"
DAY_NAME = date.today().strftime("%m%d%y")


class Poster:
    """The writer functions the pipeline calls, and the Graph reads Add makes."""

    def __init__(self, monkeypatch, fake_meta: FakeMeta):
        self.calls = {k: [] for k in ("upload_image", "upload_video", "create_adcreative", "create_ad", "status")}
        self.err: dict = {}             # fn -> exception, raised once
        self.status_err: Exception | None = None
        self.source: str | None = "https://video.xx.fbcdn.net/v/source.mp4"
        self.source_err: Exception | None = None
        self.source_reads: list[str] = []
        self.image_url: str | None = "https://scontent.xx.fbcdn.net/img.jpg"
        self.image_reads: list[tuple] = []
        self.token: str | None = "tok"
        self.made: list[dict] = []      # the ads that really exist in Meta
        self.lose_ad_reply = False      # create_ad lands, then the reply is lost, once
        self.ad_reads: list[str] = []
        self.ad_read_err: Exception | None = None
        self.fake_meta = fake_meta
        n = {"ad": 0}

        def hit(fn, result, **kw):
            self.calls[fn].append(kw)
            err = self.err.pop(fn, None)
            if err:
                raise err
            return result

        async def upload_image(act, token, *, image_url, allow_meta_cdn=False):
            return hit("upload_image", f"hash_{act}", act=act, image_url=image_url, allow_meta_cdn=allow_meta_cdn)

        async def upload_video(act, token, *, file_url, name, allow_meta_cdn=False):
            return hit("upload_video", f"vid_{act}", act=act, file_url=file_url, name=name, allow_meta_cdn=allow_meta_cdn)

        async def video_ready(video_id, token):
            return True

        async def video_thumbnail(video_id, token):
            return "https://scontent.xx.fbcdn.net/thumb.jpg"

        async def create_adcreative(act, token, **kw):
            return hit("create_adcreative", f"cr_{act}", act=act, **kw)

        async def create_ad(act, token, *, name, adset_id, creative_id):
            n["ad"] += 1
            ad_id = f"new{n['ad']}"
            self.calls["create_ad"].append(dict(act=act, name=name, adset_id=adset_id, creative_id=creative_id))
            err = self.err.pop("create_ad", None)
            if err:
                raise err
            self.made.append({"id": ad_id, "name": name, "adset_id": adset_id, "creative_id": creative_id,
                              "created_time": datetime.utcnow()})
            self.fake_meta.set(ad_id, "PAUSED")
            if self.lose_ad_reply:
                self.lose_ad_reply = False
                raise MetaWriteError("Meta request failed: ", step="create_ad")
            return ad_id

        async def find_ad(adset_id, token, *, name, creative_id, since):
            """Answers as the real read would: tested on its own in test_the_real_read_applies_the_name_rule."""
            self.ad_reads.append(adset_id)
            if self.ad_read_err:
                raise self.ad_read_err
            here = [a for a in self.made if a["adset_id"] == adset_id]
            hit_ = next((a for a in here if a["creative_id"] == creative_id), None)
            for a in here:
                if hit_ is None and a["name"] == name and (self.fake_meta.ads.get(a["id"]) or {}).get("status") == "PAUSED" \
                        and a["created_time"] >= since - timedelta(minutes=2):
                    hit_ = a
            return hit_["id"] if hit_ else None

        async def set_status(user_id, object_id, object_type, status, mongo_client):
            self.calls["status"].append((object_id, status))
            if self.status_err:
                raise self.status_err
            self.fake_meta.set(object_id, status)
            return {"success": True}

        async def facebook_token(uid, client):
            return {"access_token": self.token} if self.token else None

        async def read_source(video_id, token):
            self.source_reads.append(video_id)
            if self.source_err:
                raise self.source_err
            return self.source

        async def read_image(act, token, image_hash):
            self.image_reads.append((act, image_hash))
            return self.image_url

        for name, fn in (("upload_image", upload_image), ("upload_video", upload_video), ("video_ready", video_ready),
                         ("video_thumbnail", video_thumbnail), ("create_adcreative", create_adcreative),
                         ("create_ad", create_ad), ("set_object_status", set_status),
                         ("get_facebook_token", facebook_token), ("video_source", read_source),
                         ("image_url_for_hash", read_image), ("find_ad_in_adset", find_ad)):
            monkeypatch.setattr(posting, name, fn)
        monkeypatch.setattr(add_mod, "get_facebook_token", facebook_token)
        monkeypatch.setattr(add_mod, "video_source", read_source)
        monkeypatch.setattr(add_mod, "image_url_for_hash", read_image)

    @property
    def ads(self):
        return self.calls["create_ad"]


@pytest.fixture(autouse=True)
def writes_on(monkeypatch):
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")


@pytest.fixture
def meta(monkeypatch, mock_db):
    m = FakeMeta()
    m.db = mock_db
    monkeypatch.setattr(meta_state, "fetch_adset_ads", m.adset_ads)
    monkeypatch.setattr(apply_mod, "execute_action", m.execute)
    monkeypatch.setattr(apply_mod, "get_object_state", m.state)
    return m


@pytest.fixture
def poster(monkeypatch, meta):
    return Poster(monkeypatch, meta)


async def _world(db, *, live=("d1", "d2"), adset="s1", page="p-dest", form="lf-dest", max_live=None, modes=None):
    """A destination with live ads in one ad set, and a source client in another account."""
    settings = {}
    if max_live is not None or modes is not None:
        from ai.agents.stock.settings import DEFAULT_SETTINGS
        if max_live is not None:
            settings["creative"] = {**DEFAULT_SETTINGS["creative"], "max_ads_live": max_live}
        settings["modes"] = modes or {"remove": "auto", "revive": "auto", "add": "ask"}
    agent = await _agent(db, settings=settings)
    await db.client_groups.insert_one({"id": SRC, "user_id": USER, "name": "Source client",
                                       "meta_ad_account_id": SRC_ACT, "meta_campaign_ids": None})
    await db["meta_adset_state"].insert_one({"client_group_id": GID, "adset_id": adset, "name": "Main set"})
    for ad in live:
        await _live_ad(db, ad, adset=adset, page=page, form=form)
    return agent


async def _live_ad(db, ad, *, adset="s1", page="p-dest", form="lf-dest", status="ACTIVE", link=None, gid=GID,
                   campaign="c1"):
    await db["meta_ad_state"].insert_one({
        "client_group_id": gid, "ad_id": ad, "name": f"Name {ad}", "adset_id": adset, "campaign_id": campaign,
        "effective_status": status, "creative": {"page_id": page, "lead_form_id": form, "link": link}})


def _add(n=1, *, video=True, name="BS - Timelapse - 100926 - Win", key=KEY, src=SRC, mode="ask", **extra):
    creative = ({"video_id": "v-src", "image_hash": "thumbhash"} if video else {"image_hash": "h1"})
    creative.update(page_id="p-src", lead_form_id="lf-src", link="https://source-client.example", body="Body",
                    title="Title", cta_type="SIGN_UP")
    return _ln(n, kind="add", ad="", mode=mode, source="Source client", record={"won": 0, "lost": 0, "moves": 0},
               candidate_id=f"{src}:s{n}", source_client_group_id=src, source_ad_id=f"s{n}",
               creative_key=key, creative=creative, treatment_ids=["t1"], currency="USD", spend=80, leads=10, cpl=8.0,
               **{**{"ad_name": name}, **extra})


async def _apply(db, client, *lines, line_ids=None):
    return await apply_card(db, client, USER, "c1", by="sam", source="dashboard", line_ids=line_ids)


def _now():
    return datetime.now(timezone.utc)


def _hangs(monkeypatch, target, name) -> asyncio.Event:
    """A Graph call that never answers, and the moment something reaches it."""
    reached = asyncio.Event()

    async def hang(*a, **k):
        reached.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(target, name, hang)
    return reached


async def _sync_cut_off_at(db, client, reached: asyncio.Event):
    """A tick whose budget ends just as it reaches the hang, however slow the machine is. Finishes by itself if
    it never gets there."""
    reached.clear()
    sync, waiter = asyncio.create_task(sync_add_lines(db, _now(), client)), asyncio.create_task(reached.wait())
    await asyncio.wait({sync, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    sync.cancel()
    await asyncio.gather(sync, waiter, return_exceptions=True)


async def _tick(db, client, minutes=0):
    await posting.run_post_jobs(db, client, _now() + timedelta(minutes=minutes), limit=20)
    return await process_queued_lines(db, client, _now() + timedelta(minutes=minutes), deadline=time.monotonic() + 30)


async def _ticks(db, client, n=6, minutes=0):
    for _ in range(n):
        await _tick(db, client, minutes)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def test_clean_name_replaces_date_and_drops_copy():
    assert clean_ad_name("BS - Timelapse - 100926 - Win", date(2026, 11, 3)) == "BS - Timelapse - 110326 - Win"
    assert clean_ad_name("Plans Concept – Copy 2", date(2026, 11, 3)) == "Plans Concept - 110326"


@pytest.mark.parametrize("source,want", [
    ("Clip", "Clip - 110326"),
    ("Clip - 100926 - Copy", "Clip - 110326"),
    ("Clip - Copy 100926", "Clip - 110326"),
    ("Clip (Copy)", "Clip - 110326"),
    ("Clip - Copy - Copy 2", "Clip - 110326"),
    ("Hook - Copy 1 - Win", "Hook - Copy 1 - Win - 110326"),   # Copy isn't at the end: part of the name
    ("Photocopy", "Photocopy - 110326"),
    ("Multi-word hook 09/10/26", "Multi-word hook 110326"),
    ("Clip 123456", "Clip 123456 - 110326"),                   # not a date
    ("A 100926 - B - 200926", "A 110326 - B"),
    ("100926 - Clip", "110326 - Clip"),
])
def test_clean_name_cases(source, want):
    assert clean_ad_name(source, date(2026, 11, 3)) == want


@pytest.mark.parametrize("source", [
    "BS - Timelapse - 100926 - Win", "Plans Concept – Copy 2", "Clip", "Clip - 100926 - Copy", "Clip - Copy 100926",
    "Clip (Copy)", "Clip - Copy - Copy 2", "Hook - Copy 1 - Win", "Multi-word hook 09/10/26", "Clip 123456",
    "A 100926 - B - 200926", "100926 - Clip", "Clip — Copy", "Hook copy copy"])
def test_clean_name_still_matches_its_source(source):
    """A flagged Add and already_run find an ad by this, so the clean name must never break it."""
    clean = clean_ad_name(source, date(2026, 11, 3))
    assert normalise_name(clean) == normalise_name(source)
    assert clean_ad_name(clean, date(2026, 11, 4)).count("110426") == 1  # a Retry on another day stays one date


# ---------------------------------------------------------------------------
# Managed tests are hidden from Creatives
# ---------------------------------------------------------------------------

async def _managed(db, *, status="testing", failed=False):
    t = await create_test(db, USER, name="Clip", origin="revive", targets={}, created_via="media_agent",
                          creative={"format": "image", "media": {"image_hash": "h"}}, managed_by="media_agent",
                          ad_name="Clip - 110326")
    await db[TESTS].update_one({"id": t["id"]}, {"$set": {"status": status, "verdict": "loser", "verdict_at": datetime.utcnow(),
                                                         "summary": {}}})
    state = {"post_state": "failed", "post_error": {"message": "no", "step": "ad"}} if failed else {"post_state": "active"}
    await add_test_ads(db, USER, t["id"], [{"ad_account_id": DEST_ACT, "client_group_id": GID, "adset_id": "s1",
                                            "meta": {"ad_id": "m1"}, "score": {"state": "fail", "cpl": 9}, **state}])
    return {**t, "status": status, "verdict": "loser"}


async def test_managed_test_is_hidden_and_untracked(mock_db, mock_mongo_client, monkeypatch):
    await mock_db.client_groups.insert_one({"id": GID, "user_id": USER, "name": "C", "meta_ad_account_id": DEST_ACT})
    t = await _managed(mock_db)
    assert await list_tests(mock_db, USER) == []
    assert await list_tests(mock_db, USER, status="testing") == []
    assert await list_tests(mock_db, USER, ad_account_ids=[DEST_ACT]) == []

    tracked = []

    async def track(db, client, test, *, today):
        tracked.append(test["id"])
        return {"changed": []}

    monkeypatch.setattr(creative_tracking, "track_test", track)
    assert (await creative_tracking.track_due_tests(mock_db, mock_mongo_client, _now()))["tracked"] == 0 and tracked == []

    for extra in ({}, {"creative_test_ids": [t["id"]]}):
        alert = {"id": "a", "user_id": USER, "scope": "creative_tests", "tracking_mode": "total",
                 "condition": {"metric": "creative_verdict", "operator": "eq", "value": 1}, **extra}
        assert (await creative_alerts.evaluate_creative_alert(alert, mock_db))["triggered"] is False

    assert await cf.sync_creative_suggestions(mock_db, USER, t, await creative_store.list_test_ads(
        mock_db, t["id"], user_id=USER), mongo_client=mock_mongo_client) == 0
    assert await mock_db[SUGGESTIONS].count_documents({}) == 0
    assert cf.findings_for_test(t, [{"post_state": "failed", "ad_account_id": DEST_ACT}], {}) == []


async def test_a_failed_managed_post_raises_no_finding_or_alert(mock_db):
    await mock_db.client_groups.insert_one({"id": GID, "user_id": USER, "name": "C", "meta_ad_account_id": DEST_ACT})
    t = await _managed(mock_db, status="posting", failed=True)
    await posting._sync_suggestions(mock_db, USER, t["id"])
    assert await mock_db[SUGGESTIONS].count_documents({}) == 0
    alert = {"id": "a", "user_id": USER, "scope": "creative_tests", "tracking_mode": "total",
             "condition": {"metric": "creative_post_failed", "operator": "gt", "value": 0}}
    assert (await creative_alerts.evaluate_creative_alert(alert, mock_db))["triggered"] is False


async def test_a_managed_test_offers_nothing_to_adopt(mock_db):
    t = await _managed(mock_db)
    await mock_db.client_groups.insert_one({
        "id": GID, "user_id": USER, "meta_ad_account_id": DEST_ACT,
        "facebook_cache": {"entities": {"ads": [{"id": "hand", "name": f"{t['code']} | by hand", "adset_id": "s"}]}}})
    assert await reconcile.find_adoptable(mock_db, USER) == []


async def test_creatives_routes_and_chat_cannot_reach_a_managed_test(mock_db, mock_mongo_client, monkeypatch):
    t = await _managed(mock_db)
    with pytest.raises(Exception) as e:
        await creatives_router._owned(mock_db, USER, t["id"])
    assert e.value.status_code == 404
    from ai.tools import creative_tools
    assert await creative_tools._resolve_test(mock_db, USER, t["id"]) is None
    assert await creative_tools._resolve_test(mock_db, USER, t["code"]) is None


async def test_normal_tests_are_exactly_as_before(mock_db):
    t = await create_test(mock_db, USER, name="Summer hook", origin="upload", creative={"format": "image"}, targets={})
    assert "managed_by" not in t and "ad_name" not in t and t["meta_ad_name"] == f"{t['code']} | summer-hook"
    stored = await mock_db[TESTS].find_one({"id": t["id"]})
    assert "managed_by" not in stored and "ad_name" not in stored
    # Existing documents have no managed_by, and an explicit null is the same thing.
    await mock_db[TESTS].insert_one({"id": "old", "user_id": USER, "status": "draft", "created_at": _now(), "code": "CT-9"})
    assert {x["id"] for x in await list_tests(mock_db, USER)} == {t["id"], "old"}
    assert len(await list_tests(mock_db, USER, status="draft")) == 2
    assert (await posting._owned_test(mock_db, USER, "old"))["id"] == "old"


async def test_a_normal_job_is_claimed_before_an_older_managed_one(mock_db, mock_mongo_client):
    """The cron's five claims a tick mustn't let an Apply all make a Creatives post wait."""
    old, new = _now() - timedelta(hours=1), _now()
    await mock_db[POST_JOBS].insert_many([
        {"id": "m", "user_id": USER, "state": "queued", "lease_until": None, "managed": True, "created_at": old},
        {"id": "n", "user_id": USER, "state": "queued", "lease_until": None, "created_at": new}])
    job = await posting._claim(mock_db, datetime.utcnow(), [])
    assert job["id"] == "n"
    assert (await posting._claim(mock_db, datetime.utcnow(), ["n"]))["id"] == "m"


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def test_apply_enqueues_then_the_pipeline_finishes_it(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), current_cpl=9.5, currency="GBP")
    res = await _apply(mock_db, mock_mongo_client)
    assert res["ok"] and res["outcome"] == "queued"
    line = await _line(mock_db)
    assert line["state"] == "applying" and line["write_started_at"] and line["test_id"] and line["test_ad_id"]
    assert poster.ads == [] and await mock_db[POST_JOBS].count_documents({}) == 1  # nothing posted in the request

    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["wrote"] is True and line["meta_ids"] == ["new1"]
    assert line["undo_until"] - line["applied_at"] == UNDO_AUTO_WINDOW
    assert line["ad_name"] == f"BS - Timelapse - {DAY_NAME} - Win"
    assert (await _get(mock_db))["status"] == "applied"
    assert len(poster.ads) == 1 and poster.calls["status"] == [("new1", "ACTIVE")]
    rows = await mock_db[ACTIVITY].find({"kind": KIND_ACTION_APPLIED}).to_list(None)
    assert len(rows) == 1 and rows[0]["ref_id"] == "c1:l1" and rows[0]["label"] == "Approved by you"


async def test_ad_uses_supplied_name_not_ct_format(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    want = f"BS - Timelapse - {DAY_NAME} - Win"
    assert poster.ads[0]["name"] == want and poster.calls["create_adcreative"][0]["name"] == want
    assert poster.calls["upload_video"][0]["name"] == want
    test = await mock_db[TESTS].find_one({})
    assert test["code"].startswith("CT-") and test["ad_name"] == want
    assert (test["origin"], test["created_via"], test["managed_by"]) == ("revive", "media_agent", "media_agent")


async def test_new_ad_keeps_destinations_page_form_and_ad_set_with_the_sources_words(mock_db, mock_mongo_client, meta,
                                                                                     poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    made = poster.calls["create_adcreative"][0]
    assert (made["act"], made["page_id"], made["lead_form_id"], made["destination"]) == (DEST_ACT, "p-dest", "lf-dest",
                                                                                        "lead_form")
    assert (made["primary_text"], made["headline"], made["cta_type"]) == ("Body", "Title", "SIGN_UP")
    assert poster.ads[0]["adset_id"] == "s1"
    row = await mock_db[TEST_ADS].find_one({})
    assert (row["adset_name"], row["campaign_id"], row["client_group_id"]) == ("Main set", "c1", GID)


async def test_website_destination_uses_the_destinations_link_never_the_sources(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", form=None, link="https://destination.example/offer")
    await _live_ad(mock_db, "d2", form=None, link="https://destination.example/offer")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    made = poster.calls["create_adcreative"][0]
    assert made["destination"] == "website" and made["website_url"] == "https://destination.example/offer"


async def test_applied_add_is_mirrored_so_undo_and_already_run_see_it(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    row = await mock_db["meta_ad_state"].find_one({"ad_id": "new1"})
    assert (row["client_group_id"], row["adset_id"], row["campaign_id"]) == (GID, "s1", "c1")
    assert row["effective_status"] == "ACTIVE" and creative_key(row) == KEY


async def test_applied_add_records_one_scoreboard_row(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), current_cpl=9.5, currency="GBP")
    await _apply(mock_db, mock_mongo_client)
    assert await mock_db[OUTCOMES].count_documents({}) == 0  # nothing until it is really live
    await _ticks(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client, 2)
    rows = await mock_db[OUTCOMES].find({}).to_list(None)
    assert len(rows) == 1
    r = rows[0]
    assert (r["agent_id"], r["card_id"], r["line_id"], r["client_group_id"]) == (agent["_id"], "c1", "l1", GID)
    assert (r["creative_key"], r["treatment_ids"], r["ad_id"]) == (KEY, ["t1"], "new1")
    assert r["ad_name"] == f"BS - Timelapse - {DAY_NAME} - Win"
    assert r["baseline_cpl"] == 9.5 and r["currency"] == "GBP"  # the destination's, not the source's USD
    assert r["live_since"] and r["created_at"] and r["result"] is None and r["scored_at"] is None
    assert await record_for(mock_db, agent["_id"], KEY, ["t1"]) == {"won": 0, "lost": 0, "moves": 1}


async def test_follow_up_finds_an_applied_add_by_its_new_id(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    card = await _get(mock_db)
    state = AdState(ad_id="new1", name="Other name", adset_id="s1", campaign_id="c1", effective_status="ACTIVE",
                    created_time=None, paused_at=None)
    assert follow_ups(card, {"new1": state}, {})[0]["ok"] is False  # found by id, live, not spending yet


async def test_flagged_add_is_still_matched_by_name_after_a_person_posts_it(mock_db):
    line = _add(mode="flag")
    posted = clean_ad_name(line["ad_name"], date(2026, 11, 3))
    state = AdState(ad_id="x", name=posted, adset_id="s", campaign_id="c", effective_status="ACTIVE", created_time=None,
                    paused_at=None)
    out = follow_ups({"lines": [line], "status": "flagged"}, {"x": state}, {})
    assert out[0]["text"] == f"{line['ad_name']}: live but not spending" and out[0]["ok"] is False


def test_the_mirror_keeps_the_call_to_action():
    spec = {"page_id": "p", "link_data": {"call_to_action": {"type": "SIGN_UP", "value": {"lead_gen_form_id": "f"}}}}
    assert meta_state._creative({"object_story_spec": spec})["cta_type"] == "SIGN_UP"


# ---------------------------------------------------------------------------
# Destination
# ---------------------------------------------------------------------------

async def test_destination_comes_from_a_live_ad_in_the_main_ad_set(mock_db, meta):
    await _world(mock_db, live=())
    await _live_ad(mock_db, "small", adset="s2", page="p2", form="lf2")
    await _live_ad(mock_db, "big", adset="s1", page="p1", form="lf1")
    await _live_ad(mock_db, "big2", adset="s1", page="p1", form="lf1")
    group = await mock_db.client_groups.find_one({"id": GID})
    dest = await destination_for(mock_db, group)
    assert (dest["adset_id"], dest["adset_name"], dest["page_id"], dest["lead_form_id"], dest["campaign_id"]) == (
        "s1", "Main set", "p1", "lf1", "c1")


async def test_destination_ignores_ads_outside_the_clients_campaigns(mock_db, meta):
    await _world(mock_db, live=())
    await _live_ad(mock_db, "theirs", adset="s9", campaign="other")
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"meta_campaign_ids": ["c1"]}})
    assert await destination_for(mock_db, await mock_db.client_groups.find_one({"id": GID})) is None


async def test_destination_needs_a_page_and_does_not_borrow_from_another_ad_set(mock_db, meta):
    await _world(mock_db, live=())
    await _live_ad(mock_db, "a", adset="s1", page=None)
    await _live_ad(mock_db, "b", adset="s1", page=None)
    await _live_ad(mock_db, "c", adset="s2", page="p2")
    assert await destination_for(mock_db, await mock_db.client_groups.find_one({"id": GID})) is None


async def test_no_readable_destination_blocks_line(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _card(mock_db, agent, _add())
    res = await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert res["outcome"] == "blocked" and line["state"] == "blocked" and line["reason_code"] == "no_destination"
    assert line["error"] and "write_started_at" not in line
    assert await mock_db[TESTS].count_documents({}) == 0 and await mock_db[POST_JOBS].count_documents({}) == 0


async def test_destination_that_went_dark_in_meta_blocks_line(mock_db, mock_mongo_client, meta, poster):
    """The mirror is a day old. Meta's live read decides."""
    agent = await _world(mock_db)
    meta.set("d1", "ADSET_PAUSED")
    meta.set("d2", "ADSET_PAUSED")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "no_destination"
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_unreadable_ad_set_refuses_the_line(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)

    async def boom(*a):
        raise RuntimeError("graph down")

    monkeypatch.setattr(meta_state, "fetch_adset_ads", boom)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "live_check_failed"
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_other_agent_holding_the_client_refuses_the_line(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "not_acting_agent"
    assert await mock_db[TESTS].count_documents({}) == 0


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def _planned(act=DEST_ACT, **extra):
    return {**_add(), "destination": {"ad_account_id": act}, **extra}


async def _hold(db, url=f"{OWN_BLOB_HOST}/held.mp4", media="v-src"):
    await db[FILES].insert_one({"_id": f"{USER}:g2:{media}", "user_id": USER, "source_ad_account_id": SRC_ACT,
                                "source_video_id": media, "blob_url": url})


async def test_video_tries_held_file_then_source_then_needs_file(mock_db, mock_mongo_client, meta, poster):
    await _world(mock_db)
    # 1. The source URL, when there's no file.
    assert await plan_media(mock_db, USER, _planned(), "tok") == "ready"
    assert poster.source_reads == ["v-src"]
    # 2. A held file wins, and Meta isn't asked.
    poster.source_reads.clear()
    await _hold(mock_db)
    assert await plan_media(mock_db, USER, _planned(), "tok") == "ready"
    assert poster.source_reads == []
    # 3. Neither.
    await mock_db[FILES].delete_many({})
    poster.source = None
    assert await plan_media(mock_db, USER, _planned(), "tok") == "needs_file"
    assert poster.source_reads == ["v-src"]


async def test_source_blocked_by_meta_means_needs_file_but_a_rate_limit_does_not(mock_db, mock_mongo_client, meta, poster):
    await _world(mock_db)
    poster.source_err = MetaWriteError("not allowed", code=10)
    assert await plan_media(mock_db, USER, _planned(), "tok") == "needs_file"
    poster.source_err = MetaWriteError("slow down", code=4)
    with pytest.raises(MetaWriteError):
        await plan_media(mock_db, USER, _planned(), "tok")


async def test_rate_limit_reading_the_source_goes_round_again_saying_nothing_yet(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.source_err = MetaWriteError("slow down", code=4)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "queued" and await mock_db[TESTS].count_documents({}) == 0


async def test_source_url_is_read_at_upload_and_never_stored(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert poster.calls["upload_video"] == []
    poster.source = "https://video.xx.fbcdn.net/v/fresher.mp4"  # short-lived: the one read for the upload is used
    await _ticks(mock_db, mock_mongo_client)
    up = poster.calls["upload_video"][0]
    assert up["file_url"] == "https://video.xx.fbcdn.net/v/fresher.mp4" and up["allow_meta_cdn"] is True
    assert await mock_db[FILES].count_documents({}) == 0
    assert "fbcdn" not in str(await mock_db[TESTS].find_one({}))
    assert "fbcdn" not in str(await _get(mock_db))


async def test_held_blob_is_uploaded_with_no_cdn_permission(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _hold(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    up = poster.calls["upload_video"][0]
    assert up["file_url"] == f"{OWN_BLOB_HOST}/held.mp4" and up["allow_meta_cdn"] is False
    assert poster.source_reads == []


async def test_upload_video_refuses_a_cdn_url_unless_told(monkeypatch):
    sent = []

    async def request(method, url, step, **kw):
        sent.append(kw["data"]["file_url"])
        return {"id": "v1"}

    monkeypatch.setattr(writer, "_request", request)
    cdn = "https://video.xx.fbcdn.net/v/source.mp4"
    with pytest.raises(MetaWriteError):
        await writer.upload_video("act_1", "t", file_url=cdn, name="n")
    with pytest.raises(MetaWriteError):
        await writer.upload_video("act_1", "t", file_url="https://evil.example/x.mp4", name="n", allow_meta_cdn=True)
    assert sent == []
    assert await writer.upload_video("act_1", "t", file_url=cdn, name="n", allow_meta_cdn=True) == "v1"


async def test_graph_reads_for_media(monkeypatch):
    calls = []

    async def request(method, url, step, *, params=None, **kw):
        calls.append((url, params))
        return {"source": "https://s"} if "v1" in url else {"data": [{"hash": "h", "url": "https://u"}]}

    monkeypatch.setattr(writer, "_request", request)
    assert await writer.video_source("v1", "t") == "https://s"
    assert await writer.image_url_for_hash("act_1", "t", "h") == "https://u"
    assert calls[0][1]["fields"] == "source" and '"h"' in calls[1][1]["hashes"]


async def test_needs_file_when_neither_file_nor_source(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.source = None
    await _card(mock_db, agent, _add())
    res = await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert res["outcome"] == "needs_file" and line["state"] == "needs_file" and line["needs_file"] is True and line["error"]
    assert "write_started_at" not in line or line["write_started_at"] is None
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_auto_add_with_no_file_falls_back_to_ask(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, modes={"remove": "auto", "revive": "auto", "add": "auto"})
    poster.source = None
    await _card(mock_db, agent, _add(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    line = await _line(mock_db)
    assert (line["state"], line["mode"], line["auto_fallback"]) == ("needs_file", "ask", "needs_file")


async def test_source_meta_cant_fetch_becomes_needs_file(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["upload_video"] = MetaWriteError("Could not fetch video", code=100)
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "needs_file" and "Upload" in line["error"] and poster.ads == []


async def test_file_is_reused_for_later_adds(mock_db, mock_mongo_client, meta, poster, api, monkeypatch):
    agent = await _world(mock_db)
    poster.source = None
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "needs_file"

    url = f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}abc-clip.mp4"
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": url})
    assert r.status_code == 200, r.text
    assert await mock_db[FILES].count_documents({"user_id": USER, "source_video_id": "v-src"}) == 1
    line = await _line(mock_db)
    assert line["state"] == "proposed" and line["needs_file"] is False and line["error"] is None
    assert url not in str(r.json()["card"])

    # A later Add of the same creative, to another client, finds it without asking.
    line2 = {**_add(), "destination": {"ad_account_id": "act_other"}}
    assert await plan_media(mock_db, USER, line2, "tok") == "ready"
    assert poster.source_reads == ["v-src"]  # only the first try ever read the source

    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    assert poster.calls["upload_video"][0]["file_url"] == url and (await _line(mock_db))["state"] == "applied"


async def test_a_second_upload_replaces_the_file_it_holds(mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="needs_file"))
    for name in ("a.mp4", "b.mp4"):
        url = f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}{name}"
        await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.state": "needs_file"}})
        assert api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": url}).status_code == 200
    rows = await mock_db[FILES].find({}).to_list(None)
    assert len(rows) == 1 and rows[0]["blob_url"].endswith("b.mp4")


async def test_image_in_the_same_account_reuses_its_hash(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await mock_db.client_groups.update_one({"id": SRC}, {"$set": {"meta_ad_account_id": DEST_ACT}})
    await _card(mock_db, agent, _add(video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    assert poster.calls["upload_image"] == [] and poster.image_reads == []
    assert poster.calls["create_adcreative"][0]["media_ref"] == "h1"


async def test_image_from_another_account_is_uploaded_never_reused_by_hash(mock_db, mock_mongo_client, meta, poster):
    """Image hashes belong to one account. Reusing one in another is what this mustn't do."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    assert set(poster.image_reads) == {(SRC_ACT, "h1")}  # to check it's there, and again for a fresh URL
    up = poster.calls["upload_image"][0]
    assert up["act"] == DEST_ACT and up["image_url"].endswith("img.jpg") and up["allow_meta_cdn"] is True
    assert poster.calls["create_adcreative"][0]["media_ref"] == f"hash_{DEST_ACT}"
    assert (await _line(mock_db))["state"] == "applied"


async def test_image_from_another_account_with_no_readable_url_needs_a_file(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.image_url = None
    await _card(mock_db, agent, _add(video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "needs_file"
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_image_with_no_hash_needs_a_file_even_from_the_same_account(mock_db, meta, poster):
    await _world(mock_db)
    await mock_db.client_groups.update_one({"id": SRC}, {"$set": {"meta_ad_account_id": DEST_ACT}})
    assert await plan_media(mock_db, USER, {**_planned(), "creative": {"body": "x"}, "creative_key": "id:s"}, "tok") == "needs_file"


async def test_video_from_the_same_account_reuses_its_id_and_uploads_nothing(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await mock_db.client_groups.update_one({"id": SRC}, {"$set": {"meta_ad_account_id": "g1"}})  # act_ prefix or not
    await mock_db.client_groups.update_one({"id": GID}, {"$set": {"meta_ad_account_id": "act_g1"}})
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    assert poster.calls["upload_video"] == [] and poster.source_reads == []
    assert poster.calls["create_adcreative"][0]["media_ref"]["video_id"] == "v-src"


async def test_source_with_no_known_account_never_reuses_media(mock_db, mock_mongo_client, meta, poster):
    await _world(mock_db)
    await mock_db.client_groups.delete_one({"id": SRC})
    poster.source = None
    assert await plan_media(mock_db, USER, _planned(), "tok") == "needs_file"
    line = {**_planned(), "creative": {"image_hash": "h1"}, "creative_key": "img:h1"}
    assert await plan_media(mock_db, USER, line, "tok") == "needs_file"


# ---------------------------------------------------------------------------
# The file route
# ---------------------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    class Ctx:
        async def __aenter__(self):
            return mock_mongo_client

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(stock_router, "get_mongo_client", lambda: Ctx())
    monkeypatch.setattr(agents_router, "get_mongo_client", lambda: Ctx())
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", TOKEN)
    monkeypatch.setenv("CREATIVE_UPLOAD_SECRET", "s3cret")
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("url", [
    "http://{store}.public.blob.vercel-storage.com/{prefix}a.mp4",             # not https
    "https://other.public.blob.vercel-storage.com/{prefix}a.mp4",              # someone else's Blob store
    "https://{store}.public.blob.vercel-storage.com.evil.com/{prefix}a.mp4",
    "https://{store}.public.blob.vercel-storage.com@evil.com/{prefix}a.mp4",
    "https://evil.com/{store}.public.blob.vercel-storage.com/{prefix}a.mp4",
    "https://169.254.169.254/{prefix}a.mp4",
    "https://{store}.public.blob.vercel-storage.com/creatives/uuid-a.mp4",     # no owner in the path
    "https://{store}.public.blob.vercel-storage.com/{other}a.mp4",             # another user's upload
    "https://{store}.public.blob.vercel-storage.com/{prefix}../{other}a.mp4",
    "https://{store}.public.blob.vercel-storage.com:8443/{prefix}a.mp4",
    "https://{store}.public.blob.vercel-storage.com/{prefix}a.mp4?x=https://evil.com",
    "not a url",
])
async def test_file_route_refuses_what_isnt_this_users_blob(url, mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="needs_file"))
    bad = url.format(store=STORE, prefix=upload_grant.owner_prefix(USER), other=upload_grant.owner_prefix("someone-else"))
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": bad})
    assert r.status_code == 400
    assert await mock_db[FILES].count_documents({}) == 0 and (await _line(mock_db))["state"] == "needs_file"


def test_blob_check_fails_closed_with_no_store_configured(monkeypatch):
    monkeypatch.setenv("CREATIVE_UPLOAD_SECRET", "s3cret")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    url = f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}a.mp4"
    assert upload_grant.is_own_blob_url(url, USER) is False


def test_upload_grant_endpoint_gives_the_path_prefix(monkeypatch, mock_mongo_client):
    monkeypatch.setenv("CREATIVE_UPLOAD_SECRET", "s3cret")
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        r = TestClient(app).post("/api/creatives/upload-grant", json={"mime": "video/mp4", "bytes": 100})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.json()["path_prefix"] == upload_grant.owner_prefix(USER)
    assert r.json()["path_prefix"].startswith("creatives/") and USER not in r.json()["path_prefix"]
    assert r.json()["grant"]


@pytest.mark.parametrize("line,card_extra,code", [
    ({"kind": "remove", "ad": "d1"}, {}, 409),
    ({"state": "applying"}, {}, 409),
    ({"state": "applied"}, {}, 409),
    ({"state": "flagged", "mode": "flag"}, {}, 409),
    ({}, {"status": "superseded"}, 409),
    ({}, {"preview": True}, 409),
])
async def test_file_route_refuses_lines_that_cant_take_a_file(line, card_extra, code, mock_db, api, meta, poster):
    agent = await _world(mock_db)
    built = _ln(kind="remove", ad="d1") if line.get("kind") else _add(**line)
    await _card(mock_db, agent, built, **card_extra)
    url = f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}a.mp4"
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": url})
    assert r.status_code == code and await mock_db[FILES].count_documents({}) == 0


async def test_file_route_is_owner_scoped(mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="needs_file"))
    url = f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}a.mp4"
    aid = agent["_id"]
    assert api.post(f"/api/agents/{aid}/cards/nope/lines/l1/file", json={"blob_url": url}).status_code == 404
    assert api.post(f"/api/agents/{aid}/cards/c1/lines/nope/file", json={"blob_url": url}).status_code == 404
    app.dependency_overrides[get_current_user] = lambda: "another-user"
    assert api.post(f"/api/agents/{aid}/cards/c1/lines/l1/file", json={"blob_url": url}).status_code == 404
    assert await mock_db[FILES].count_documents({}) == 0


# ---------------------------------------------------------------------------
# The flag and the mode at write time
# ---------------------------------------------------------------------------

async def test_writes_off_when_flag_disabled(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    res = await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert res["outcome"] == "flagged" and line["state"] == "flagged" and line["mode"] == "flag" and line["note"]
    assert await mock_db[TESTS].count_documents({}) == 0 and await mock_db[POST_JOBS].count_documents({}) == 0
    await _ticks(mock_db, mock_mongo_client)
    assert poster.ads == [] and poster.source_reads == [] and meta.adset_reads == []


async def test_enqueue_itself_refuses_with_the_flag_off(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    agent = await _world(mock_db)
    card = await _card(mock_db, agent, _add())
    res = await enqueue_add(mock_db, mock_mongo_client, USER, card, card["lines"][0])
    assert res["outcome"] == "blocked" and await mock_db[TESTS].count_documents({}) == 0


async def test_flag_turned_off_stops_a_post_already_queued(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert poster.calls["upload_video"] == [] and poster.ads == []
    assert line["state"] == "failed" and "switched off" in line["error"]
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")  # turned back on, a Retry carries on
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


async def test_flag_turned_off_leaves_a_created_ad_paused(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("down", 500)
    await _ticks(mock_db, mock_mongo_client, 3)
    assert poster.ads and (await _line(mock_db))["state"] == "applying"
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    poster.status_err = None
    before = len(poster.calls["status"])
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "paused" in line["error"] and len(poster.calls["status"]) == before


async def test_undo_still_works_with_the_flag_off(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "undone" and ("new1", "PAUSED") in meta.writes


async def test_mode_changed_to_flag_after_queueing_means_no_post(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="queued"))
    settings = (await mock_db[AGENTS].find_one({"_id": agent["_id"]}))["settings"]
    await mock_db[AGENTS].update_one({"_id": agent["_id"]},
                                     {"$set": {"settings": {**settings, "modes": {**settings["modes"], "add": "flag"}}}})
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "flagged" and line["mode"] == "flag" and await mock_db[TESTS].count_documents({}) == 0


async def test_auto_add_posts_only_while_it_still_resolves_to_auto(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, modes={"remove": "auto", "revive": "auto", "add": "ask"})
    await _card(mock_db, agent, _add(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    line = await _line(mock_db)
    assert (line["state"], line["mode"]) == ("proposed", "ask") and await mock_db[TESTS].count_documents({}) == 0


async def test_auto_add_posts_and_is_labelled(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, modes={"remove": "auto", "revive": "auto", "add": "auto"})
    await _card(mock_db, agent, _add(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1
    assert (await mock_db[ACTIVITY].find_one({"kind": KIND_ACTION_APPLIED}))["label"] == "Auto-run by Birdy"


async def test_on_hold_and_off_agents_refuse(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "agent_off" and await mock_db[TESTS].count_documents({}) == 0


# ---------------------------------------------------------------------------
# Failing and retrying
# ---------------------------------------------------------------------------

async def test_failed_post_sets_explained_error_and_retry(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await mock_db.client_groups.update_one({"id": SRC}, {"$set": {"meta_ad_account_id": DEST_ACT}})
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_adcreative"] = MetaWriteError("Invalid", code=100, user_msg="This lead form does not belong to the Page")
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "lead form does not belong" in line["error"]
    assert line["fix"] == "use_website_url" and not line.get("write_started_at")
    assert await mock_db[SUGGESTIONS].count_documents({}) == 0  # Creatives doesn't hear about it

    res = await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    assert res["outcome"] == "queued"
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["meta_ids"] == ["new1"] and line["error"] is None
    assert len(poster.ads) == 1
    assert await mock_db[TESTS].count_documents({}) == 1 and await mock_db[POST_JOBS].count_documents({}) == 1


async def test_retry_does_not_repeat_what_already_reached_meta(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("Invalid", code=100)
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed"
    assert (len(poster.calls["upload_video"]), len(poster.calls["create_adcreative"])) == (1, 1)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (len(poster.calls["upload_video"]), len(poster.calls["create_adcreative"])) == (1, 1)
    assert len(poster.source_reads) == 2 and len(poster.ads) == 2  # one refused, one made
    assert (await _line(mock_db))["state"] == "applied"


async def test_a_retry_posts_to_todays_destination_since_that_is_what_it_checked(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await mock_db.client_groups.update_one({"id": SRC}, {"$set": {"meta_ad_account_id": DEST_ACT}})
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("The ad set is paused", code=100)
    await _ticks(mock_db, mock_mongo_client)
    await mock_db["meta_ad_state"].delete_many({"adset_id": "s1"})
    await _live_ad(mock_db, "m1", adset="s7", page="p7", form="lf7")
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert poster.ads[-1]["adset_id"] == "s7" and (await _line(mock_db))["state"] == "applied"
    assert poster.calls["create_adcreative"][-1]["page_id"] == "p7"


async def test_no_meta_token_fails_clearly_and_sends_nothing(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.token = None
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and "Meta" in line["error"] and await mock_db[TESTS].count_documents({}) == 0


async def test_go_live_failure_is_retried_by_the_cron(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("Meta hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, 4)
    assert (await _line(mock_db))["state"] == "applying" and len(poster.ads) == 1
    poster.status_err = None
    await _tick(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["meta_ids"] == ["new1"] and len(poster.ads) == 1


async def test_go_live_that_keeps_failing_says_the_ad_is_paused_and_retry_doesnt_post_again(mock_db, mock_mongo_client,
                                                                                           meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("Meta hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "paused" in line["error"] and len(poster.ads) == 1
    tries = len(poster.calls["status"])
    await _ticks(mock_db, mock_mongo_client, 2)
    assert len(poster.calls["status"]) == tries  # it stopped asking

    poster.status_err = None
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


async def test_ad_removed_in_meta_before_it_was_seen_live_fails(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await mock_db[TEST_ADS].update_one({}, {"$set": {"post_state": "removed"}})
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed"


# ---------------------------------------------------------------------------
# Never twice
# ---------------------------------------------------------------------------

async def test_double_press_posts_once(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await asyncio.gather(*[_apply(mock_db, mock_mongo_client) for _ in range(5)])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and await mock_db[TESTS].count_documents({}) == 1
    assert await mock_db[POST_JOBS].count_documents({}) == 1


async def test_retry_press_while_posting_changes_nothing(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    res = await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    assert res["outcome"] == "already"
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and await mock_db[POST_JOBS].count_documents({}) == 1


async def test_two_lines_of_one_creative_post_once(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(1), _add(2))
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1
    assert (await _line(mock_db, "l1"))["state"] == "applied"
    assert (await _line(mock_db, "l2"))["reason_code"] == "already_posted"


async def test_two_cards_of_one_creative_post_once(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    res = await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert (await _line(mock_db, _id="c2"))["reason_code"] == "already_posted" and res["outcome"] == "blocked"
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1


async def test_a_creative_applied_and_undone_is_not_posted_again(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and (await _line(mock_db, _id="c2"))["state"] == "blocked"


async def test_the_reservation_itself_refuses_when_the_earlier_check_saw_nothing(mock_db, mock_mongo_client, meta, poster):
    """Two clients sharing an ad account don't share a lease, so this write is what decides between them."""
    agent = await _world(mock_db)
    first = await _card(mock_db, agent, _add(), created=datetime.utcnow() - timedelta(hours=3))
    second = await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    assert (await enqueue_add(mock_db, mock_mongo_client, USER, first, first["lines"][0]))["outcome"] == "posting"
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.state": "applying"}})
    res = await enqueue_add(mock_db, mock_mongo_client, USER, second, second["lines"][0])
    assert res["outcome"] == "blocked" and res["reason"]["code"] == "already_posted"
    assert await mock_db[TESTS].count_documents({}) == 1 and await mock_db[POST_JOBS].count_documents({}) == 1


async def test_the_same_creative_can_go_to_another_account(mock_db, mock_mongo_client, meta, poster):
    agent = await _agent(mock_db, gids=(GID, "g3"))
    await mock_db.client_groups.insert_one({"id": SRC, "user_id": USER, "name": "S", "meta_ad_account_id": SRC_ACT})
    for gid in (GID, "g3"):
        await _live_ad(mock_db, f"{gid}a", adset=f"s{gid}", gid=gid)
        await _live_ad(mock_db, f"{gid}b", adset=f"s{gid}", gid=gid)
    await _card(mock_db, agent, _add())
    await _card(mock_db, agent, _add(), _id="c3", run_id="r1", gid="g3")
    await _apply(mock_db, mock_mongo_client)
    await apply_card(mock_db, mock_mongo_client, USER, "c3", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 2 and {a["act"] for a in poster.ads} == {"act_g1", "act_g3"}


async def test_creative_that_reached_the_account_since_the_card_was_written_is_refused(mock_db, mock_mongo_client, meta,
                                                                                      poster):
    """already_run was true at proposal time. Someone hand-posted it since."""
    agent = await _world(mock_db)
    await _live_ad(mock_db, "hand", adset="s9", status="PAUSED")
    await mock_db["meta_ad_state"].update_one({"ad_id": "hand"}, {"$set": {"name": "BS - Timelapse - 010126 - Win"}})
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["reason_code"] == "already_run" and await mock_db[TESTS].count_documents({}) == 0


async def test_a_failed_post_doesnt_stop_a_newer_card_trying(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    poster.err["upload_video"] = MetaWriteError("Bad file", code=100)
    await mock_db[TESTS].update_one({}, {"$set": {"creative.media": {"blob_url": f"{OWN_BLOB_HOST}/x.mp4"}}})
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed" and poster.ads == []
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db, _id="c2"))["state"] == "applied" and len(poster.ads) == 1
    # The old card's Retry now finds the newer one holds it.
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"created_at": datetime.utcnow() + timedelta(hours=1)}})
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "already_posted" and len(poster.ads) == 1


async def test_a_paused_ad_from_a_failed_go_live_still_holds_the_creative(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("Meta hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    assert (await _line(mock_db))["state"] == "failed"
    poster.status_err = None
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db, _id="c2"))["reason_code"] == "already_posted" and len(poster.ads) == 1


# ---------------------------------------------------------------------------
# Crashes. An Add's post is asynchronous, so recovery must never enqueue a second one.
# ---------------------------------------------------------------------------

async def test_recovery_leaves_a_posting_add_to_the_pipeline(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    before = await _line(mock_db)
    await process_queued_lines(mock_db, mock_mongo_client, _now() + STUCK_AFTER * 3, deadline=time.monotonic() + 30)
    line = await _line(mock_db)
    assert (line["state"], line["write_started_at"]) == ("applying", before["write_started_at"])
    assert await mock_db[POST_JOBS].count_documents({}) == 1 and await mock_db[TESTS].count_documents({}) == 1
    await _ticks(mock_db, mock_mongo_client, minutes=20)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


async def test_add_killed_before_the_marker_is_queued_again_and_posts_once(mock_db, mock_mongo_client, meta, poster,
                                                                           monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="queued"))

    real = add_mod.plan_media_doc

    async def die(*a, **k):
        raise Killed()

    monkeypatch.setattr(add_mod, "plan_media_doc", die)
    with pytest.raises(Killed):
        await process_queued_lines(mock_db, mock_mongo_client, _now(), deadline=time.monotonic() + 30)
    await mock_db[apply_mod.LEASES].delete_many({})
    assert (await _line(mock_db))["state"] == "applying"
    monkeypatch.setattr(add_mod, "plan_media_doc", real)
    await _ticks(mock_db, mock_mongo_client, 1)
    assert (await _line(mock_db))["state"] == "applying" and not (await _line(mock_db)).get("write_started_at")
    await process_queued_lines(mock_db, mock_mongo_client, _now() + STUCK_AFTER * 2, deadline=time.monotonic() + 30)
    await _ticks(mock_db, mock_mongo_client, minutes=20)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


@pytest.mark.parametrize("die_at", ["create_test", "enqueue_post", "save_ids"])
async def test_add_killed_after_the_marker_resumes_without_a_second_post(die_at, mock_db, mock_mongo_client, meta, poster,
                                                                         monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="queued"))

    target = {"create_test": "create_test", "enqueue_post": "enqueue_post", "save_ids": "_linked"}[die_at]
    real = getattr(add_mod, target)

    async def die(*a, **k):
        if die_at == "save_ids":
            await real(*a, **k)
        raise Killed()

    monkeypatch.setattr(add_mod, target, die)
    with pytest.raises(Killed):
        await process_queued_lines(mock_db, mock_mongo_client, _now(), deadline=time.monotonic() + 30)
    monkeypatch.setattr(add_mod, target, real)
    await mock_db[apply_mod.LEASES].delete_many({})
    line = await _line(mock_db)
    assert line["state"] == "applying" and line["write_started_at"]

    # Not old enough to be sure the other invocation has gone.
    await _tick(mock_db, mock_mongo_client)
    assert await mock_db[POST_JOBS].count_documents({}) <= 1 and poster.ads == []

    later = STUCK_AFTER + timedelta(minutes=1)
    for _ in range(6):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now() + later, limit=20)
        await sync_add_lines(mock_db, _now() + later, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1
    assert await mock_db[POST_JOBS].count_documents({}) == 1
    assert await mock_db[TEST_ADS].count_documents({}) == 1


async def test_killed_between_claiming_the_test_and_inserting_its_row(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    """enqueue_post's own undo can't run if the function is killed."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="queued"))

    real = posting.add_test_ads

    async def die(*a, **k):
        raise Killed()

    monkeypatch.setattr(posting, "add_test_ads", die)
    with pytest.raises(Killed):
        await process_queued_lines(mock_db, mock_mongo_client, _now(), deadline=time.monotonic() + 30)
    monkeypatch.setattr(posting, "add_test_ads", real)
    await mock_db[apply_mod.LEASES].delete_many({})
    assert (await mock_db[TESTS].find_one({}))["status"] == "posting" and await mock_db[TEST_ADS].count_documents({}) == 0

    later = STUCK_AFTER + timedelta(minutes=1)
    for _ in range(6):
        await sync_add_lines(mock_db, _now() + later, mock_mongo_client)
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now() + later, limit=20)
    await sync_add_lines(mock_db, _now() + later, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


async def test_killed_with_a_row_but_no_job(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await mock_db[POST_JOBS].delete_many({})
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert await mock_db[POST_JOBS].count_documents({}) == 0  # it could still be being written
    later = STUCK_AFTER + timedelta(minutes=1)
    await sync_add_lines(mock_db, _now() + later, mock_mongo_client)
    assert await mock_db[POST_JOBS].count_documents({}) == 1
    await sync_add_lines(mock_db, _now() + later, mock_mongo_client)
    assert await mock_db[POST_JOBS].count_documents({}) == 1
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and len(poster.ads) == 1


async def test_killed_between_ad_created_and_go_live(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(posting, "_after_created", nothing)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    assert (await mock_db[TEST_ADS].find_one({}))["post_state"] == "created"
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and poster.calls["status"] == [("new1", "ACTIVE")]


async def test_killed_while_finishing_is_finished_once(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)

    real = add_mod._set_line

    async def die(*a, **k):
        raise Killed()

    monkeypatch.setattr(add_mod, "_set_line", die)
    with pytest.raises(Killed):
        await sync_add_lines(mock_db, _now(), mock_mongo_client)
    monkeypatch.setattr(add_mod, "_set_line", real)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"
    assert await mock_db[OUTCOMES].count_documents({}) == 1
    assert await mock_db[ACTIVITY].count_documents({"kind": KIND_ACTION_APPLIED}) == 1


async def test_one_broken_line_doesnt_stop_the_rest_of_the_sync(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _agent(mock_db, gids=(GID, "g3"))
    await mock_db.client_groups.insert_one({"id": SRC, "user_id": USER, "name": "S", "meta_ad_account_id": SRC_ACT})
    for gid in (GID, "g3"):
        await _live_ad(mock_db, f"{gid}a", adset=f"s{gid}", gid=gid)
        await _live_ad(mock_db, f"{gid}b", adset=f"s{gid}", gid=gid)
    await _card(mock_db, agent, _add())
    await _card(mock_db, agent, _add(), _id="c3", gid="g3")
    await _apply(mock_db, mock_mongo_client)
    await apply_card(mock_db, mock_mongo_client, USER, "c3", by="s", source="d")
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    real = add_mod._finish_add
    seen = []

    async def once(db, card, *a, **k):
        seen.append(card["_id"])
        if len(seen) == 1:
            raise RuntimeError("store hiccup")
        return await real(db, card, *a, **k)

    monkeypatch.setattr(add_mod, "_finish_add", once)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert len(seen) == 2
    states = [(await _line(mock_db, _id=c))["state"] for c in ("c1", "c3")]
    assert sorted(states) == ["applied", "applying"]


async def test_an_ad_already_live_finishes_on_a_card_closed_meanwhile(mock_db, mock_mongo_client, meta, poster):
    """A newer card closing this one doesn't change what really happened in Meta."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    await posting.go_live(mock_db, mock_mongo_client, USER, (await mock_db[TESTS].find_one({}))["id"])  # a kill after this
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "superseded"}})
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["meta_ids"] == ["new1"]
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"


async def test_card_that_closes_before_the_marker_blocks_the_add(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    real = add_mod.plan_media_doc

    async def close_then_plan(*a, **k):
        await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "superseded"}, "$inc": {"version": 1}})
        return await real(*a, **k)

    monkeypatch.setattr(add_mod, "plan_media_doc", close_then_plan)
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "superseded" and await mock_db[TESTS].count_documents({}) == 0


async def test_dismiss_is_refused_while_an_add_is_posting(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    res = await dismiss_card(mock_db, USER, "c1", by="s", source="d")
    assert res["outcome"] == "busy"


# ---------------------------------------------------------------------------
# The live ad limit
# ---------------------------------------------------------------------------

async def test_add_over_the_limit_is_blocked(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2", "d3"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "max_ads_live" and "3" in line["error"]
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_add_up_to_the_limit_goes_through(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"


async def test_two_adds_cant_overshoot_while_the_first_is_still_posting(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add(1), _add(2, key="name:other"))
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db, "l1"))["state"] == "applying"
    assert (await _line(mock_db, "l2"))["reason_code"] == "max_ads_live"
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1


async def test_an_add_in_flight_on_another_card_counts(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add(1), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    await _card(mock_db, agent, _add(2, key="name:other"), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert (await _line(mock_db, "l2", _id="c2"))["reason_code"] == "max_ads_live"


async def test_an_add_in_flight_to_another_ad_set_doesnt_count(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add(1), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.destination.adset_id": "elsewhere"}})
    await _card(mock_db, agent, _add(2, key="name:other"), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert (await _line(mock_db, "l2", _id="c2"))["state"] == "applying"


async def test_an_ad_in_review_counts_since_it_goes_live_by_itself(mock_db, mock_mongo_client, meta, poster):
    """A new ad reads PENDING_REVIEW or IN_PROCESS, not ACTIVE, for hours after it is turned on."""
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _live_ad(mock_db, "fresh", status="PENDING_REVIEW")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["reason_code"] == "max_ads_live"


async def test_a_paused_ad_doesnt_count(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _live_ad(mock_db, "off", status="PAUSED")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"


async def test_an_add_applied_moments_ago_counts_when_the_read_lags(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add(1), created=datetime.utcnow() - timedelta(hours=3))
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    await mock_db["meta_ad_state"].delete_one({"ad_id": "new1"})  # what the live read below is built from
    await _card(mock_db, agent, _add(2, key="name:other"), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert (await _line(mock_db, "l2", _id="c2"))["reason_code"] == "max_ads_live"


async def test_a_swap_removes_first_so_it_stays_within_the_limit(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2", "d3"), max_live=3)
    await _card(mock_db, agent, _add(1), _ln(2, ad="d1"))
    await _apply(mock_db, mock_mongo_client)
    assert meta.writes == [("d1", "PAUSED")]
    assert (await _line(mock_db, "l1"))["state"] == "applying"
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db, "l1"))["state"] == "applied"


async def test_a_bring_back_counts_adds_in_flight(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _live_ad(mock_db, "old", status="PAUSED")
    meta.set("old", "PAUSED")
    await _card(mock_db, agent, _add(1), _ln(2, kind="revive", ad="old"))
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l1"])
    await apply_card(mock_db, mock_mongo_client, USER, "c1", by="s", source="d", line_ids=["l2"])
    assert (await _line(mock_db, "l2"))["reason_code"] == "max_ads_live" and meta.writes == []


async def test_a_retry_of_a_paused_new_ad_doesnt_count_itself(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    assert (await _line(mock_db))["state"] == "failed"
    poster.status_err = None
    await _live_ad(mock_db, "new1", status="ACTIVE")  # Meta turned it on after all
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def _applied_add(db, client, agent):
    await _card(db, agent, _add())
    await _apply(db, client)
    await _ticks(db, client)
    assert (await _line(db))["state"] == "applied"


async def test_undo_add_pauses_new_ad(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="sam", source="dashboard")
    assert res["ok"] and res["outcome"] == "undone"
    assert meta.writes == [("new1", "PAUSED")]
    assert (await _line(mock_db))["state"] == "undone"
    assert (await mock_db["meta_ad_state"].find_one({"ad_id": "new1"}))["effective_status"] == "PAUSED"
    assert (await mock_db[TEST_ADS].find_one({}))["meta_status"] == "PAUSED"
    # An undone Add isn't a result. It stays listed but can't be scored against the ad.
    row = await mock_db[OUTCOMES].find_one({})
    assert row["result"] == "undone" and row["scored_at"]
    assert await record_for(mock_db, agent["_id"], KEY) == {"won": 0, "lost": 0, "moves": 1}


async def test_undo_never_touches_the_source_ad(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert {w[0] for w in meta.writes} == {"new1"}


@pytest.mark.parametrize("state", ["queued", "applying"])
async def test_undo_before_the_post_finished_says_so(state, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state=state))
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "busy" and "still posting" in res["reason"]
    assert meta.writes == [] and (await _line(mock_db))["state"] == state


async def test_undo_of_an_add_that_never_posted_is_not_undoable(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="failed"))
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "not_undoable" and meta.writes == []


async def test_undo_window_runs_for_add_too(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.undo_until": datetime.utcnow() - timedelta(minutes=1)}})
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "expired" and meta.writes == []


async def test_undo_add_keeps_the_guardrails(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    # A swap took the others off: this is now the ad set's last live ad.
    for ad in ("d1", "d2"):
        meta.set(ad, "PAUSED")
    meta.set("new1", "ACTIVE")
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["ok"] is False and res["outcome"] == "blocked" and "no live ads" in res["reason"]
    assert meta.writes == [] and (await _line(mock_db))["state"] == "applied"


async def test_undo_add_refused_after_the_client_moved(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    await claims.claim_clients(mock_db, USER, "another-agent", [GID], move=True)
    res = await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    assert res["outcome"] == "blocked" and meta.writes == []


async def test_undo_add_killed_mid_send_is_read_back(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _applied_add(mock_db, mock_mongo_client, agent)
    meta.kill_after_write = True
    with pytest.raises(Killed):
        await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    meta.kill_after_write = False
    await mock_db[apply_mod.LEASES].delete_many({})
    assert (await _line(mock_db))["state"] == "undoing"
    await process_queued_lines(mock_db, mock_mongo_client, _now() + STUCK_AFTER * 2, deadline=time.monotonic() + 30)
    assert (await _line(mock_db))["state"] == "undone" and meta.writes == [("new1", "PAUSED")]


async def test_an_undo_in_flight_counts_as_a_pause_for_the_last_live_ad_check(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1",))
    await _applied_add(mock_db, mock_mongo_client, agent)
    meta.kill_after_write = True
    with pytest.raises(Killed):
        await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d")
    meta.kill_after_write = False
    await mock_db[apply_mod.LEASES].delete_many({})
    await _card(mock_db, agent, _ln(1, ad="d1"), _id="c2", run_id="r2")
    res = await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert res["outcome"] == "blocked" and (await _line(mock_db, _id="c2"))["reason_code"] == "last_live_ad"


# ---------------------------------------------------------------------------
# The routes around it
# ---------------------------------------------------------------------------

async def test_route_undo_of_a_posting_add_is_a_conflict_with_the_reason(mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(state="applying"))
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/undo", json={"line_id": "l1"})
    assert r.status_code == 409 and "still posting" in r.json()["detail"]


async def test_a_needs_file_line_can_be_dropped_from_a_card(mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(1, state="needs_file"), _ln(2, ad="d1"))
    r = api.patch(f"/api/agents/{agent['_id']}/cards/c1", json={"lines": [{"id": "l2"}]})
    assert r.status_code == 200 and [ln["id"] for ln in r.json()["lines"]] == ["l2"]


async def test_tick_runs_add_sync_inside_the_stock_budget(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    res = await process_queued_lines(mock_db, mock_mongo_client, _now(), deadline=time.monotonic() + 30)
    assert res["add_synced"] == 1 and (await _line(mock_db))["state"] == "applied"


async def test_a_real_tick_budget_leaves_time_for_a_go_live(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    """The cron's stock budget is 10 s and a line takes 4 s, so a slice of that budget would never start one."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    poster.status_err = None
    await process_queued_lines(mock_db, mock_mongo_client, _now(), deadline=time.monotonic() + creative_tracking.STOCK_LINES_BUDGET_SECONDS)
    assert (await _line(mock_db))["state"] == "applied"


async def test_a_go_live_that_hangs_still_counts_as_a_try(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)

    reached = _hangs(monkeypatch, posting, "go_live")
    await _sync_cut_off_at(mock_db, mock_mongo_client, reached)
    assert reached.is_set()
    assert (await _line(mock_db))["go_live_tries"] == 1


async def test_no_time_left_means_no_go_live_call_this_tick(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    before = len(poster.calls["status"])
    await sync_add_lines(mock_db, _now(), mock_mongo_client, deadline=time.monotonic())
    assert len(poster.calls["status"]) == before


# ---------------------------------------------------------------------------
# A file belongs to one source video
# ---------------------------------------------------------------------------

def _owned_url(name):
    return f"{OWN_BLOB_HOST}/{upload_grant.owner_prefix(USER)}{name}"


async def _third_client(db):
    await db.client_groups.insert_one({"id": "g3", "user_id": USER, "name": "Third client", "meta_ad_account_id": "act_g3"})


async def test_a_file_never_goes_to_a_different_video_with_the_same_name(mock_db, mock_mongo_client, meta, poster, api):
    agent = await _world(mock_db)
    await _third_client(mock_db)
    poster.source = None
    await _card(mock_db, agent, _add(name="Video 1 - 100926", key="name:video 1"))
    await _apply(mock_db, mock_mongo_client)
    api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": _owned_url("client-two.mp4")})
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "dismissed"}})

    poster.source = "https://video.xx.fbcdn.net/third.mp4"
    other = _add(src="g3", name="Video 1 - 010226", key="name:video 1")
    other["creative"]["video_id"] = "v-third"
    await _card(mock_db, agent, other, _id="c3", run_id="r3", created=datetime.utcnow())
    await mock_db[add_mod.POSTS].delete_many({})  # in real life another destination account, so another row
    await apply_card(mock_db, mock_mongo_client, USER, "c3", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert "v-third" in poster.source_reads
    uploaded = [u["file_url"] for u in poster.calls["upload_video"]]
    assert uploaded == ["https://video.xx.fbcdn.net/third.mp4"]
    row = await mock_db[FILES].find_one({})
    assert row["source_ad_account_id"] == SRC_ACT and row["source_video_id"] == "v-src"


async def test_same_video_going_to_a_second_destination_reuses_the_file(mock_db, mock_mongo_client, meta, poster, api):
    agent = await _agent(mock_db, gids=(GID, "g4"))
    await mock_db.client_groups.insert_one({"id": SRC, "user_id": USER, "name": "S", "meta_ad_account_id": SRC_ACT})
    for gid, adset in ((GID, "s1"), ("g4", "s4")):
        await _live_ad(mock_db, f"{gid}a", adset=adset, gid=gid)
        await _live_ad(mock_db, f"{gid}b", adset=adset, gid=gid)
    poster.source = None
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    url = _owned_url("video.mp4")
    assert api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": url}).status_code == 200
    # Named differently on the second card, as agencies do: the video is what counts.
    await _card(mock_db, agent, _add(name="Renamed - 010226", key="name:renamed"), _id="c4", gid="g4")
    poster.source_reads.clear()
    await apply_card(mock_db, mock_mongo_client, USER, "c4", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    assert poster.source_reads == [] and [u["file_url"] for u in poster.calls["upload_video"]] == [url]
    assert (await _line(mock_db, _id="c4"))["state"] == "applied"


async def test_a_line_with_no_known_source_account_cant_take_a_file(mock_db, api, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(src="gone"), state="proposed")
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.state": "needs_file"}})
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": _owned_url("a.mp4")})
    assert r.status_code >= 400 and await mock_db[FILES].count_documents({}) == 0


# ---------------------------------------------------------------------------
# Checked again before it goes on
# ---------------------------------------------------------------------------

async def _stop(db, agent, change):
    if change == "agent_paused":
        await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    elif change == "on_hold":
        await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.clients": {GID: {"on_hold": True}}}})
    elif change == "other_agent":
        await claims.claim_clients(db, USER, "another-agent", [GID], move=True)
    elif change == "mode_flag":
        await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.add": "flag"}})
    elif change == "closed":
        await db[CARDS].update_one({"_id": "c1"}, {"$set": {"status": "superseded"}})
    elif change == "newer_card":
        await _card(db, agent, _ln(9), _id="c2", run_id="r2", created=datetime.utcnow())
    elif change == "over_limit":
        await _live_ad(db, "d3")


@pytest.mark.parametrize("change,code", [
    ("agent_paused", "agent_off"), ("on_hold", "on_hold"), ("other_agent", "not_acting_agent"), ("mode_flag", "now_flag"),
    ("closed", "superseded"), ("newer_card", "superseded"), ("over_limit", "max_ads_live")])
async def test_each_stop_leaves_the_new_ad_paused_with_the_reason(change, code, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"
    await _stop(mock_db, agent, change)
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert poster.calls["status"] == [] and len(poster.ads) == 1
    assert line["state"] == "failed" and line["reason_code"] in (code, "left_paused")
    assert "paused" in line["error"] and line["paused_ad_id"] == "new1" and not line.get("write_started_at")
    assert await mock_db[OUTCOMES].count_documents({}) == 0


async def test_an_ad_that_lost_auto_mode_waits_for_a_person(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, modes={"remove": "auto", "revive": "auto", "add": "auto"})
    await _card(mock_db, agent, _add(mode="auto"))
    await auto_apply_run(mock_db, mock_mongo_client, _run(agent), agent)
    assert (await _line(mock_db))["state"] == "applying"
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.modes.add": "ask"}})
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert poster.calls["status"] == [] and line["state"] == "failed" and line["reason_code"] == "not_auto" and line["mode"] == "ask"
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied" and poster.calls["status"] == [("new1", "ACTIVE")]


@pytest.mark.parametrize("change,clear", [("over_limit", "d3"), ("on_hold", None)])
async def test_a_person_can_retry_once_the_reason_has_passed(change, clear, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _stop(mock_db, agent, change)
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "failed"
    # Still full: Retry says so, and the ad stays paused.
    if change == "over_limit":
        await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
        await _ticks(mock_db, mock_mongo_client)
        line = await _line(mock_db)
        assert line["state"] == "blocked" and "paused" in line["error"] and poster.calls["status"] == []
        meta.set(clear, "PAUSED")
    else:
        await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"settings.clients": {}}})
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["paused_ad_id"] is None and len(poster.ads) == 1


async def test_retry_under_flag_says_the_ad_stays_paused(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _stop(mock_db, agent, "mode_flag")
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    line = await _line(mock_db)
    assert line["state"] == "flagged" and "paused" in line["note"]


async def test_a_failed_read_at_go_live_never_turns_it_on(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)

    async def boom(*a):
        raise RuntimeError("graph down")

    monkeypatch.setattr(meta_state, "fetch_adset_ads", boom)
    await _tick(mock_db, mock_mongo_client)
    await _tick(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"  # a blip: it tries again
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 2)
    line = await _line(mock_db)
    assert poster.calls["status"] == [] and line["state"] == "failed" and "paused" in line["error"]


async def test_go_live_waits_for_the_lease(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    key = apply_mod._lease_key(agent["_id"], GID)
    token = await apply_mod._take_lease(mock_db, key)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert poster.calls["status"] == [] and (await _line(mock_db))["state"] == "applying"
    await apply_mod._release_lease(mock_db, key, token)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert poster.calls["status"] == [("new1", "ACTIVE")]


async def test_the_pipeline_never_turns_on_a_managed_ad_itself(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(6):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    assert poster.calls["status"] == [] and (await mock_db[TEST_ADS].find_one({}))["post_state"] == "created"


async def _normal_post(db, poster):
    await db.client_groups.insert_one({"id": GID, "user_id": USER, "meta_ad_account_id": DEST_ACT})
    test = await create_test(db, USER, name="Normal", origin="upload", targets={}, creative={
        "format": "image", "media": {"blob_url": f"{OWN_BLOB_HOST}/a.jpg"}, "destination": "website", "website_url": "https://a.test"})
    await posting.enqueue_post(db, USER, test["id"], accounts=[
        {"ad_account_id": DEST_ACT, "client_group_id": GID, "adset_id": "s", "page_id": "p"}])
    return test


async def test_a_normal_post_goes_live_by_itself_and_never_looks_for_a_stray(mock_db, mock_mongo_client, meta, poster):
    await _normal_post(mock_db, poster)
    for _ in range(5):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=5)
    assert [a for a, s in poster.calls["status"] if s == "ACTIVE"] == ["new1"] and poster.ad_reads == []
    assert "ad_create_started_at" not in (await mock_db[TEST_ADS].find_one({}))["meta"]


async def test_a_normal_lost_reply_keeps_its_old_behaviour(mock_db, mock_mongo_client, meta, poster):
    await _normal_post(mock_db, poster)
    poster.lose_ad_reply = True
    for _ in range(5):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=5)
    row = await mock_db[TEST_ADS].find_one({})
    assert row["post_state"] == "failed" and row["post_error"]["message"] == "Meta request failed: " and poster.ad_reads == []


async def test_resume_after_a_kill_refuses_what_changed_meanwhile(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())

    async def die(*a, **k):
        raise Killed()

    with monkeypatch.context() as m:
        m.setattr(add_mod, "enqueue_post", die)
        with pytest.raises(Killed):
            await _apply(mock_db, mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await sync_add_lines(mock_db, _now() + STUCK_AFTER + timedelta(minutes=1), mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "agent_off"
    assert await mock_db[POST_JOBS].count_documents({}) == 0


# ---------------------------------------------------------------------------
# A stray paused ad
# ---------------------------------------------------------------------------

async def test_lost_create_ad_reply_then_retry_adopts_the_ad(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "Meta request failed" not in line["error"] and "Try again" in line["error"]
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert len(poster.ads) == 1 and poster.ad_reads == ["s1"] and line["state"] == "applied" and line["meta_ids"] == ["new1"]
    assert poster.calls["status"] == [("new1", "ACTIVE")]


async def test_kill_after_create_ad_adopts_with_no_person(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    real = posting._set_meta
    armed = [True]

    async def die(db, ad, key, value, now, **extra):
        if key == "ad_id" and armed:
            armed.clear()
            raise Killed()
        return await real(db, ad, key, value, now, **extra)

    monkeypatch.setattr(posting, "_set_meta", die)
    with pytest.raises(Killed):
        for _ in range(6):
            await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    await _ticks(mock_db, mock_mongo_client, 6, minutes=5)
    line = await _line(mock_db)
    assert len(poster.ads) == 1 and line["state"] == "applied" and line["meta_ids"] == ["new1"]


async def test_nothing_found_after_the_marker_means_it_never_landed(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("Meta request failed: ", step="create_ad")
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 2 and poster.ad_reads == ["s1"] and (await _line(mock_db))["meta_ids"] == ["new2"]


async def test_a_failed_read_backs_off_and_doesnt_create(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    poster.ad_read_err = MetaWriteError("Meta request failed: ", step="read_adset_ads")
    await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    job = await mock_db[POST_JOBS].find_one({})
    assert job["state"] == "queued" and job["attempts"] == 1 and job["lease_until"] is not None
    for i in range(1, 12):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now() + timedelta(minutes=10 * i), limit=20)
    assert len(poster.ads) == 1
    row = await mock_db[TEST_ADS].find_one({})
    assert row["post_state"] == "failed" and "Meta request failed" not in row["post_error"]["message"]
    poster.ad_read_err = None
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and (await _line(mock_db))["state"] == "applied"


async def test_an_expired_token_on_that_read_says_reconnect(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    poster.ad_read_err = MetaWriteError("expired", code=190)
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert len(poster.ads) == 1 and line["state"] == "failed" and "Reconnect" in line["error"]


async def test_meta_refusing_the_ad_clears_the_marker_so_retry_can_move(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("ad set is paused", code=100)
    await _ticks(mock_db, mock_mongo_client)
    row = await mock_db[TEST_ADS].find_one({})
    assert "ad_create_started_at" not in row["meta"]


async def test_a_failed_owner_that_may_have_made_an_ad_keeps_the_creative(mock_db, mock_mongo_client, meta, poster):
    """Otherwise a newer card posts a whole new test before the mirror has seen the first ad."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2", created=datetime.utcnow())
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db, _id="c2")
    assert line["state"] == "blocked" and line["reason_code"] == "already_posted" and len(poster.ads) == 1


async def test_find_ad_in_adset_prefers_the_creative_and_reads_every_page(monkeypatch):
    pages = [{"data": [{"id": "a1", "name": "Same", "creative": {"id": "other"}}], "paging": {"next": "u", "cursors": {"after": "x"}}},
             {"data": [{"id": "a2", "name": "Different", "creative": {"id": "cr"}}]}]
    seen = []

    async def request(method, url, step, *, params=None, **kw):
        seen.append(params)
        return pages[len(seen) - 1]

    monkeypatch.setattr(writer, "_request", request)
    since = datetime(2026, 11, 4, 12)
    fresh = {"status": "PAUSED", "created_time": "2026-11-04T12:00:30+0000"}
    pages[0]["data"][0].update(fresh)
    assert await writer.find_ad_in_adset("s", "t", name="Same", creative_id="cr", since=since) == "a2"
    assert seen[1]["after"] == "x" and "creative{id}" in seen[0]["fields"]
    seen.clear()
    pages[:] = [{"data": [{"id": "a1", "name": "Same", "creative": {"id": "other"}, **fresh}]}]
    assert await writer.find_ad_in_adset("s", "t", name="Same", creative_id="cr", since=since) == "a1"
    seen.clear()
    assert await writer.find_ad_in_adset("s", "t", name="Nope", creative_id="cr", since=since) is None


# ---------------------------------------------------------------------------
# A video Meta rejects while processing
# ---------------------------------------------------------------------------

async def test_video_rejected_in_processing_takes_a_new_file(mock_db, mock_mongo_client, meta, poster, api, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    bad = [True]

    async def ready(video_id, token):
        if bad:
            raise MetaWriteError("Meta could not process the video", step="video_status")
        return True

    monkeypatch.setattr(posting, "video_ready", ready)
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "needs_file" and line["needs_file"] is True and "process" in line["error"]

    url = _owned_url("right.mp4")
    assert api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": url}).status_code == 200
    bad.clear()
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and len(poster.ads) == 1
    assert [u["file_url"] for u in poster.calls["upload_video"]] == [poster.source, url]
    assert (await mock_db[TESTS].find_one({}))["creative"]["media"] == {"blob_url": url}


async def test_a_new_file_never_replaces_media_once_an_ad_may_exist(mock_db, mock_mongo_client, meta, poster, api):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    assert api.post(f"/api/agents/{agent['_id']}/cards/c1/lines/l1/file", json={"blob_url": _owned_url("x.mp4")}).status_code == 200
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.calls["upload_video"]) == 1 and len(poster.ads) == 1 and (await _line(mock_db))["state"] == "applied"


async def test_a_lost_upload_reply_is_not_a_request_for_a_file(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["upload_video"] = MetaWriteError("Meta request failed: ", step="upload_video")
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "Meta request failed" not in line["error"]


# ---------------------------------------------------------------------------
# A Retry of an ad that exists checks where it is
# ---------------------------------------------------------------------------

async def _main_moves_to(monkeypatch, adset):
    real = add_mod._live_by_adset
    monkeypatch.setattr(add_mod, "_live_by_adset", lambda live, spend: (real(live, spend)[0], adset))


async def test_retry_of_a_paused_ad_checks_its_own_ad_set(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    poster.status_err = None
    assert (await _line(mock_db))["state"] == "failed"
    await _live_ad(mock_db, "d3")  # s1 is now full
    for ad in ("m1", "m2"):
        await _live_ad(mock_db, ad, adset="zz")
    await _main_moves_to(monkeypatch, "zz")
    meta.adset_reads.clear()
    tries = len(poster.calls["status"])
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["destination"]["adset_id"] == "s1" and line["state"] == "blocked" and line["reason_code"] == "max_ads_live"
    assert set(meta.adset_reads) == {"s1"} and len(poster.calls["status"]) == tries


async def test_retry_after_a_lost_reply_stays_in_the_ad_set_that_was_asked(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    for ad in ("m1", "m2"):
        await _live_ad(mock_db, ad, adset="zz")
    await _main_moves_to(monkeypatch, "zz")
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and poster.ad_reads == ["s1"] and (await _line(mock_db))["meta_ids"] == ["new1"]


async def test_retry_with_no_ad_still_follows_todays_main_ad_set(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("ad set is paused", code=100)
    await _ticks(mock_db, mock_mongo_client)
    for ad in ("m1", "m2"):
        await _live_ad(mock_db, ad, adset="zz", page="p-z")
    await _main_moves_to(monkeypatch, "zz")
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert poster.ads[-1]["adset_id"] == "zz" and (await _line(mock_db))["state"] == "applied"


# ---------------------------------------------------------------------------
# No blank ads
# ---------------------------------------------------------------------------

def _worded(body="Body", title="Title", **kw):
    line = _add(**kw)
    line["creative"].update(body=body, title=title)
    return line


@pytest.mark.parametrize("body", [None, "", "   "])
@pytest.mark.parametrize("video", [True, False])
async def test_no_primary_text_is_blocked_before_the_marker(body, video, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _worded(body=body, video=video))
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "no_words" and not line.get("write_started_at")
    assert await mock_db[TESTS].count_documents({}) == 0


async def test_no_headline_blocks_an_image_ad_with_no_real_link_to_title_it(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)  # the destination's live ads use a lead form
    await _card(mock_db, agent, _worded(title="  ", video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "no_words" and "headline" in line["error"]


async def test_no_headline_is_fine_where_the_format_doesnt_need_one(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _worded(title=None))  # a video
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"


async def test_no_headline_is_fine_for_an_image_ad_to_a_website(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", form=None, link="https://client.example/offer")
    await _live_ad(mock_db, "d2", form=None, link="https://client.example/offer")
    await _card(mock_db, agent, _worded(title=None, video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"


# ---------------------------------------------------------------------------
# Reading the source's media
# ---------------------------------------------------------------------------

async def test_a_timeout_reading_the_source_tries_again_next_tick(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.source_err = MetaWriteError("Meta request failed: ", step="video_source")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "queued" and not line.get("write_started_at") and not line.get("needs_file")
    poster.source_err = None
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"


async def test_reading_the_source_gives_up_after_a_few_tries(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.source_err = MetaWriteError("slow down", code=4)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client, 8)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "Nothing was changed" in line["error"] and await mock_db[TESTS].count_documents({}) == 0


async def test_an_expired_token_reading_the_source_says_reconnect(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    poster.source_err = MetaWriteError("expired", code=190)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "no_token" and "Reconnect" in line["error"]


# ---------------------------------------------------------------------------
# Small ones
# ---------------------------------------------------------------------------

async def test_missing_job_repair_makes_one_job_when_crons_overlap(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await mock_db[POST_JOBS].delete_many({})
    card, row = await _get(mock_db), await mock_db[TEST_ADS].find_one({})
    coll = mock_db[POST_JOBS]
    real = type(coll).find_one

    async def slow(self, *a, **k):
        found = await real(self, *a, **k)
        await asyncio.sleep(0.01)
        return found

    monkeypatch.setattr(type(coll), "find_one", slow)
    later = datetime.utcnow() + timedelta(minutes=10)
    await asyncio.gather(*[add_mod._sync_in_pipeline(mock_db, card, card["lines"][0], row, later) for _ in range(2)])
    assert await coll.count_documents({"test_ad_id": row["id"]}) == 1
    assert await mock_db[apply_mod.LEASES].count_documents({}) == 0


async def test_an_add_on_its_way_counts_once_when_its_ad_is_in_the_read(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=4)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(5):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    await _live_ad(mock_db, "new1", status="PENDING_REVIEW")
    assert await apply_mod.adds_in_flight(mock_db, GID, "s1", ("x", "y"), datetime.utcnow()) == {"new1"}
    await _card(mock_db, agent, _add(key="name:other", name="Other - 100926"), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert (await _line(mock_db, _id="c2"))["state"] == "applying"


async def test_page_and_form_come_from_an_ad_meta_says_is_live(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", page="old-page", form="old-form")
    await _live_ad(mock_db, "d2", page="new-page", form="new-form")
    meta.set("d1", "PAUSED")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _ticks(mock_db, mock_mongo_client)
    made = poster.calls["create_adcreative"][0]
    assert made["page_id"] == "new-page" and made["lead_form_id"] == "new-form"


async def test_no_usable_live_ad_in_the_read_blocks_with_a_reason(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", page="p1", form="f1")
    await _live_ad(mock_db, "d2", page=None, form="f2")   # live in Meta, but nothing to copy from it
    meta.set("d1", "PAUSED")
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "no_destination" and await mock_db[TESTS].count_documents({}) == 0


@pytest.mark.parametrize("link", ["https://fb.com/messenger_doc/", "https://m.me/client", "https://www.messenger.com/t/x",
                                  "https://www.facebook.com/client", "https://www.instagram.com/client", "ftp://x.example/a", "not a link"])
async def test_a_link_that_isnt_a_website_blocks_with_a_reason(link, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", form=None, link=link)
    await _live_ad(mock_db, "d2", form=None, link=link)
    group = await mock_db.client_groups.find_one({"id": GID})
    assert await destination_for(mock_db, group) is None
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "unsupported_landing" and "website" in line["error"]


@pytest.mark.parametrize("link,want", [("https://api.whatsapp.com/send?phone=1", "whatsapp"), ("https://wa.me/123", "whatsapp"),
                                       ("https://client.example/offer", "website"), ("https://notfacebook.com/", "website")])
async def test_links_we_can_repeat(link, want, mock_db, meta, poster):
    await _world(mock_db, live=())
    await _live_ad(mock_db, "d1", form=None, link=link)
    await _live_ad(mock_db, "d2", form=None, link=link)
    group = await mock_db.client_groups.find_one({"id": GID})
    assert (await destination_for(mock_db, group))["destination"] == want


async def test_a_paused_ad_on_a_replaced_card_ends_clearly(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    poster.status_err = None
    assert "Try again" in (await _line(mock_db))["error"]

    await _card(mock_db, agent, _add(), _id="c2", run_id="r2", created=datetime.utcnow())
    await _ticks(mock_db, mock_mongo_client, 2)
    old = await _line(mock_db)
    assert old["state"] == "failed" and old["reason_code"] == "left_paused" and "Try again" not in old["error"]
    assert "paused in Meta" in old["error"] and "Ads Manager" in old["error"]
    rows = await mock_db[ACTIVITY].find({"ref_id": "c1:l1", "kind": add_mod.KIND_AD_LEFT_PAUSED}).to_list(None)
    assert len(rows) == 1 and "paused" in rows[0]["title"]

    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    new = await _line(mock_db, _id="c2")
    assert new["state"] == "blocked" and new["reason_code"] == "already_posted"
    assert "paused in Meta" in new["error"] and len(poster.ads) == 1
    await _ticks(mock_db, mock_mongo_client, 2)
    assert await mock_db[ACTIVITY].count_documents({"ref_id": "c1:l1", "kind": add_mod.KIND_AD_LEFT_PAUSED}) == 1


async def test_a_replaced_card_with_no_ad_just_says_nothing_is_left(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.err["create_ad"] = MetaWriteError("refused", code=100)
    await _ticks(mock_db, mock_mongo_client)
    before = (await _line(mock_db))["error"]
    await _card(mock_db, agent, _ln(9), _id="c2", run_id="r2", created=datetime.utcnow())
    await _ticks(mock_db, mock_mongo_client, 2)
    assert (await _line(mock_db))["error"] == before and await mock_db[ACTIVITY].count_documents({"ref_id": "c1:l1"}) == 0


async def test_an_image_url_is_read_at_upload_and_never_stored(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    test = await mock_db[TESTS].find_one({})
    assert test["creative"]["media"] == {"source_image_hash": "h1"} and "fbcdn" not in str(test)
    poster.image_url = "https://scontent.xx.fbcdn.net/fresher.jpg"
    await _ticks(mock_db, mock_mongo_client)
    up = poster.calls["upload_image"][0]
    assert up["image_url"].endswith("fresher.jpg") and up["allow_meta_cdn"] is True
    assert "fbcdn" not in str(await mock_db[TESTS].find_one({})) and "fbcdn" not in str(await _get(mock_db))


async def test_an_image_url_gone_by_upload_time_asks_for_a_file(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add(video=False, key="img:h1"))
    await _apply(mock_db, mock_mongo_client)
    poster.image_url = None
    await _ticks(mock_db, mock_mongo_client)
    assert (await _line(mock_db))["state"] == "needs_file"


async def test_bring_back_counts_what_add_counts(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _live_ad(mock_db, "d3", status="PENDING_REVIEW")
    await _live_ad(mock_db, "off1", status="PAUSED")
    await _card(mock_db, agent, _ln(1, kind="revive", ad="off1"))
    await _apply(mock_db, mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "blocked" and line["reason_code"] == "max_ads_live"


async def test_a_post_still_on_its_way_isnt_called_paused(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    for _ in range(4):
        await posting.run_post_jobs(mock_db, mock_mongo_client, _now(), limit=20)
    assert (await mock_db[TEST_ADS].find_one({}))["post_state"] == "created"
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2")
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    line = await _line(mock_db, _id="c2")
    assert line["reason_code"] == "already_posted" and "paused" not in line["error"]


async def test_a_lost_reply_is_only_said_to_maybe_exist(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2", created=datetime.utcnow())
    await _ticks(mock_db, mock_mongo_client, 2)
    assert "may" in (await _line(mock_db))["error"]
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert "may" in (await _line(mock_db, _id="c2"))["error"] and len(poster.ads) == 1


async def test_missing_job_repair_looks_again_inside_the_lease(mock_db, mock_mongo_client, meta, poster):
    """The other cron may have finished its repair between this one's first look and its lease."""
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    row = await mock_db[TEST_ADS].find_one({})
    await add_mod._repair_job(mock_db, await _get(mock_db), row, datetime.utcnow())
    assert await mock_db[POST_JOBS].count_documents({"test_ad_id": row["id"]}) == 1


async def test_missing_job_repair_waits_when_the_lease_is_taken(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await mock_db[POST_JOBS].delete_many({})
    token = await apply_mod._take_lease(mock_db, apply_mod._lease_key(agent["_id"], GID))
    await add_mod._repair_job(mock_db, await _get(mock_db), await mock_db[TEST_ADS].find_one({}), datetime.utcnow())
    assert token and await mock_db[POST_JOBS].count_documents({}) == 0


# ---------------------------------------------------------------------------
# Fix round 2: go-live tries
# ---------------------------------------------------------------------------

async def _to_created(db, client):
    for _ in range(4):
        await posting.run_post_jobs(db, client, _now(), limit=20)


def test_go_live_tries_allow_about_a_quarter_of_an_hour():
    assert add_mod.GO_LIVE_TRIES == 15


async def test_a_check_that_keeps_raising_ends_paused(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)

    async def bug(*a, **k):
        raise ValueError("unexpected shape")

    monkeypatch.setattr(add_mod, "_go_live_refusal", bug)
    for _ in range(add_mod.GO_LIVE_TRIES):
        await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and line["paused_ad_id"] == "new1" and "paused in Meta" in line["error"]
    assert poster.calls["status"] == [] and not line.get("write_started_at")


async def test_a_read_cut_off_every_tick_ends_paused(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)

    reached = _hangs(monkeypatch, meta_state, "fetch_adset_ads")
    for _ in range(add_mod.GO_LIVE_TRIES):
        await _sync_cut_off_at(mock_db, mock_mongo_client, reached)
        assert reached.is_set() and (await _line(mock_db))["state"] == "applying"
    await _sync_cut_off_at(mock_db, mock_mongo_client, reached)
    assert not reached.is_set()  # past the limit: no more reads
    line = await _line(mock_db)
    assert line["state"] == "failed" and line["paused_ad_id"] == "new1" and poster.calls["status"] == []
    assert await mock_db[apply_mod.LEASES].count_documents({}) == 0


# ---------------------------------------------------------------------------
# Fix round 2: adopting by name
# ---------------------------------------------------------------------------

async def _lost_reply_then_retry(db, client, poster, other: dict):
    """create_ad's reply is lost and nothing landed, and another ad with this name sits in the ad set."""
    poster.err["create_ad"] = MetaWriteError("Meta request failed: ")
    await _ticks(db, client)
    await _apply(db, client, line_ids=["l1"])
    poster.made.append(other)
    await _ticks(db, client)


TODAY_NAME = clean_ad_name("BS - Timelapse - 100926 - Win", date.today())


@pytest.mark.parametrize("status,age", [("PAUSED", timedelta(hours=3)), ("ACTIVE", timedelta(0)), ("ACTIVE", timedelta(hours=3))])
async def test_a_persons_ad_with_the_same_name_is_never_adopted(status, age, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, max_live=9)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    meta.set("person1", status)
    await _lost_reply_then_retry(mock_db, mock_mongo_client, poster, {
        "id": "person1", "name": TODAY_NAME, "adset_id": "s1", "creative_id": "theirs",
        "created_time": datetime.utcnow() - age})
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["meta_ids"] == ["new2"]
    assert "person1" not in [a for a, _ in poster.calls["status"]] and meta.ads["person1"]["status"] == status


async def test_birdys_own_live_ad_with_the_same_clean_name_is_not_adopted(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, max_live=9)
    a = _add(1, video=False, key="img:h1", name="Static 1 - 100926")
    b = _add(2, video=False, key="img:h2", name="Static 1 - 010226")
    b["creative"]["image_hash"] = "h2"
    await _card(mock_db, agent, a, b)
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    first = await _line(mock_db, "l1")
    assert first["meta_ids"] == ["new1"]
    poster.made[0]["creative_id"] = "cr_of_a"
    poster.err["create_ad"] = MetaWriteError("Meta request failed: ")
    await _apply(mock_db, mock_mongo_client, line_ids=["l2"])
    await _ticks(mock_db, mock_mongo_client)
    await _apply(mock_db, mock_mongo_client, line_ids=["l2"])
    await _ticks(mock_db, mock_mongo_client)
    second = await _line(mock_db, "l2")
    assert second["state"] == "applied" and second["meta_ids"] != ["new1"] and second["meta_ids"]


async def test_a_paused_fresh_ad_with_the_name_is_adopted_after_a_rebuilt_creative(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    poster.lose_ad_reply = True
    await _ticks(mock_db, mock_mongo_client)
    poster.made[0]["creative_id"] = "an-earlier-creative"
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    await _ticks(mock_db, mock_mongo_client)
    assert len(poster.ads) == 1 and (await _line(mock_db))["meta_ids"] == ["new1"]


async def test_the_real_read_applies_the_name_rule(monkeypatch):
    since = datetime(2026, 11, 4, 12, 0, 0)
    rows = {"data": [
        {"id": "old", "name": "N", "status": "PAUSED", "created_time": "2026-11-04T09:00:00+0000", "creative": {"id": "x"}},
        {"id": "live", "name": "N", "status": "ACTIVE", "created_time": "2026-11-04T12:00:30+0000", "creative": {"id": "x"}},
        {"id": "unparsable", "name": "N", "status": "PAUSED", "created_time": "soon", "creative": {"id": "x"}},
        {"id": "other", "name": "M", "status": "PAUSED", "created_time": "2026-11-04T12:00:30+0000", "creative": {"id": "x"}}]}
    seen = []

    async def request(method, url, step, *, params=None, **kw):
        seen.append(params)
        return rows

    monkeypatch.setattr(writer, "_request", request)
    assert await writer.find_ad_in_adset("s", "t", name="N", creative_id="cr", since=since) is None
    assert "status" in seen[0]["fields"] and "created_time" in seen[0]["fields"]
    # A little clock difference is forgiven, and a creative match needs nothing else.
    rows["data"].append({"id": "mine", "name": "N", "status": "PAUSED", "created_time": "2026-11-04T11:59:30+0000", "creative": {"id": "x"}})
    assert await writer.find_ad_in_adset("s", "t", name="N", creative_id="cr", since=since) == "mine"
    assert await writer.find_ad_in_adset("s", "t", name="zz", creative_id="x", since=since) == "old"


# ---------------------------------------------------------------------------
# Fix round 2: saying "paused" only when it is
# ---------------------------------------------------------------------------

def _lands_then_lost(meta, poster, monkeypatch):
    async def status(user_id, object_id, object_type, value, mongo_client):
        poster.calls["status"].append((object_id, value))
        meta.set(object_id, value)
        raise MetaStatusError("timed out", 500)

    monkeypatch.setattr(posting, "set_object_status", status)


async def test_go_live_that_landed_is_applied_when_a_person_stops_the_agent(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    _lands_then_lost(meta, poster, monkeypatch)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applying"
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "applied" and line["meta_ids"] == ["new1"] and line.get("paused_ad_id") is None
    assert (await mock_db[TEST_ADS].find_one({}))["post_state"] == "active"
    assert await mock_db[OUTCOMES].count_documents({}) == 1
    assert (await undo_line(mock_db, mock_mongo_client, USER, "c1", "l1", by="s", source="d"))["outcome"] == "undone"
    assert meta.ads["new1"]["status"] == "PAUSED"


async def test_go_live_that_landed_is_applied_when_the_tries_run_out(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"lines.0.go_live_tries": add_mod.GO_LIVE_TRIES - 1}})
    _lands_then_lost(meta, poster, monkeypatch)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"


async def test_go_live_that_didnt_land_says_paused_after_reading(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    meta.reads.clear()
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    line = await _line(mock_db)
    assert meta.reads == ["new1"] and line["state"] == "failed" and line["paused_ad_id"] == "new1" and "paused in Meta" in line["error"]


async def test_no_attempt_means_nothing_to_read(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert meta.reads == [] and (await _line(mock_db))["paused_ad_id"] == "new1"


async def test_unreadable_after_an_attempt_says_it_couldnt_confirm(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    meta.read_error = RuntimeError("graph down")
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and line["reason_code"] == "unconfirmed" and line.get("paused_ad_id") is None
    assert "confirm" in line["error"] and "paused" not in line["error"] and "Ads Manager" in line["error"]


async def test_unconfirmed_on_a_replaced_card_doesnt_say_paused(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("hiccup", 500)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    meta.read_error = RuntimeError("graph down")
    await _card(mock_db, agent, _add(), _id="c2", run_id="r2", created=datetime.utcnow())
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    old = await _line(mock_db)
    assert old["reason_code"] == "left_paused" and "paused in Meta" not in old["error"] and "confirm" in old["error"]
    await apply_card(mock_db, mock_mongo_client, USER, "c2", by="s", source="d")
    assert "paused" not in (await _line(mock_db, _id="c2"))["error"]


async def test_an_ad_deleted_in_meta_says_so(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("gone", 400)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    meta.ads["new1"] = None
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    line = await _line(mock_db)
    assert line["state"] == "failed" and "removed in Meta" in line["error"] and line.get("paused_ad_id") is None


async def test_kill_switch_after_an_attempt_reads_too(mock_db, mock_mongo_client, meta, poster, monkeypatch):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    _lands_then_lost(meta, poster, monkeypatch)
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "false")
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    assert (await _line(mock_db))["state"] == "applied"


# ---------------------------------------------------------------------------
# Fix round 2: go-live wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code", ["agent_off", "on_hold", "not_acting_agent", "now_flag", "not_auto", "superseded", "dismissed",
                                  "max_ads_live", "no_destination", "live_check_failed", "protected_tag", "no_permission", "something_new"])
async def test_each_go_live_stop_has_its_own_plain_words(code, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    card, row = await _get(mock_db), await mock_db[TEST_ADS].find_one({})
    await add_mod._leave_paused(mock_db, mock_mongo_client, card, card["lines"][0], row, {"code": code, "message": "Nothing was posted. Try again."})
    line = await _line(mock_db)
    assert line["state"] == "failed" and line["reason_code"] == code and "paused in Meta" in line["error"]
    low = line["error"].lower()
    assert "nothing was" not in low and low.count("try again") <= 1 and ": " not in low and "\u2014" not in low


async def test_stops_read_differently(mock_db, meta, poster):
    codes = ["agent_off", "on_hold", "not_acting_agent", "now_flag", "not_auto", "superseded", "dismissed", "max_ads_live",
             "no_destination", "live_check_failed"]
    assert len({add_mod._stop_text({"code": c, "message": ""}) for c in codes}) == len(codes)


@pytest.mark.parametrize("change", ["over_limit", "no_live_ads"])
async def test_a_retry_refused_with_an_ad_uses_the_same_words(change, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db, live=("d1", "d2"), max_live=3)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "paused"}})
    await sync_add_lines(mock_db, _now(), mock_mongo_client)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "active"}})
    if change == "over_limit":
        await _live_ad(mock_db, "d3")
    else:
        meta.set("d1", "PAUSED")
        meta.set("d2", "PAUSED")
    await _apply(mock_db, mock_mongo_client, line_ids=["l1"])
    line = await _line(mock_db)
    low = line["error"].lower()
    assert line["state"] == "blocked" and low.count("paused in meta") == 1 and "nothing was" not in low and low.count("try again") <= 1


async def test_a_meta_error_that_says_try_again_isnt_said_twice(mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _add())
    await _apply(mock_db, mock_mongo_client)
    await _to_created(mock_db, mock_mongo_client)
    poster.status_err = MetaStatusError("Meta is busy. Try again in a few minutes.", 500)
    await _ticks(mock_db, mock_mongo_client, add_mod.GO_LIVE_TRIES + 6)
    line = await _line(mock_db)
    assert line["state"] == "failed" and line["error"].lower().count("try again") == 1 and "paused in Meta" in line["error"]


@pytest.mark.parametrize("line_", [
    {"body": None, "title": "T"}, {"body": "B", "title": None}])
async def test_no_words_says_what_to_do_next(line_, mock_db, mock_mongo_client, meta, poster):
    agent = await _world(mock_db)
    await _card(mock_db, agent, _worded(video=False, key="img:h1", **line_))
    await _apply(mock_db, mock_mongo_client)
    assert "Post it by hand in Ads Manager if you want it." in (await _line(mock_db))["error"]
