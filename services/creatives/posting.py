"""Posting pipeline for creative tests.

`enqueue_post` writes one `creative_test_ads` row plus one `creative_post_jobs` doc per
account. The per-minute cron (`run_post_jobs`) claims jobs under a 90 s lease and runs one
step per claim, media -> creative -> ad, storing each Meta id on `TEST_ADS.meta` before
moving on; a step whose id is already stored is skipped, so a crash, a retry or an
overlapping tick never creates a second Meta object. Every ad is created PAUSED; going
live is a separate pass (`go_live`), run automatically once every ad of a `posting` test
is created, or immediately per ad when the test is already live (add-accounts).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_writer import (
    MetaWriteError,
    create_ad,
    create_adcreative,
    find_ad_in_adset,
    image_url_for_hash,
    upload_image,
    upload_video,
    video_ready,
    video_source,
    video_thumbnail,
)
from services.creatives.errors import explain
from services.creatives.store import (
    ARCHIVED,
    DRAFT,
    LIVE_STATUSES,
    POST_JOBS,
    POSTING,
    TEST_ADS,
    TESTING,
    TESTS,
    add_test_ads,
    creative_format,
    get_test,
    list_test_ads,
    update_test,
)
from services.meta_status_service import MetaStatusError, set_object_status

logger = logging.getLogger(__name__)

LEASE = timedelta(seconds=90)
# Meta pulls the video from file_url inside the upload call (writer timeout 300 s), so that
# one step holds a longer lease: a reclaim mid-upload would create a second video.
VIDEO_UPLOAD_LEASE = timedelta(seconds=360)
BACKOFF_BASE = timedelta(seconds=30)
MAX_ATTEMPTS = 5
MAX_VIDEO_POLLS = 60  # ~1 h of minute ticks
MAX_VIDEO_UPLOADS_PER_TICK = 1
VIDEO_NEEDS_UPLOAD = "Upload the video file to post it to another ad account"
IMAGE_NEEDS_UPLOAD = "Upload the image file to post it to another ad account"
MANAGED_WRITES_OFF = "Posting new ads is switched off, so Birdy stopped before posting this."
REPLY_LOST = "Birdy lost touch with Meta during this step, so nothing has gone live. Try again."
AD_REPLY_LOST = ("Birdy lost touch with Meta while creating this ad, so it may already exist. "
                 "Try again: Birdy looks first, so it won't make a second one.")
AD_UNCONFIRMED = "Birdy couldn't check Meta for this ad, so it didn't make another one. Try again in a few minutes."

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

ACCOUNT_FIELDS = ("ad_account_id", "client_group_id", "adset_id", "adset_name", "page_id",
                  "lead_form_id", "targets_override", "campaign_id")
PATCH_FIELDS = ("destination", "website_url", "lead_form_id", "page_id", "adset_id", "adset_name")
CREATIVE_FIELDS = ("destination", "website_url", "lead_form_id", "page_id")
LIVE_STATES = LIVE_STATUSES


class _CheckBackOff(MetaWriteError):
    """Birdy couldn't look for an ad a lost reply may have left, so it waits rather than make another."""


class PostingError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _naive(dt: datetime | None) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def _owned_test(db, user_id: str, test_id: str) -> dict:
    test = await get_test(db, user_id, test_id)
    if test is None:
        raise PostingError("Creative test not found", 404)
    return test


def _new_job(user_id: str, test_id: str, test_ad_id: str, now: datetime) -> dict:
    return {"id": uuid.uuid4().hex, "user_id": user_id, "test_id": test_id,
            "test_ad_id": test_ad_id, "state": QUEUED, "attempts": 0, "lease_until": None,
            "created_at": now, "updated_at": now}


def _managed_job(test: dict) -> dict:
    """An agent's job is claimed after the others, so a burst of them can't make a Creatives post wait."""
    return {"managed": True} if test.get("managed_by") else {}


# ── enqueue ──────────────────────────────────────────────────────────────────

async def enqueue_post(db, user_id: str, test_id: str, *, accounts: list[dict],
                       add_accounts: bool = False, now: datetime | None = None) -> dict:
    """Queue one ad per account. Draft tests move to `posting` atomically (the
    double-click guard); add-accounts keeps the test's status so a live test's new ads
    go live as soon as they are created."""
    now = _naive(now)
    test = await _owned_test(db, user_id, test_id)
    if not accounts:
        raise PostingError("Pick at least one ad account")
    rows = []
    for acc in accounts:
        row = {k: acc.get(k) for k in ACCOUNT_FIELDS if acc.get(k) is not None}
        for req in ("ad_account_id", "adset_id", "page_id"):
            if not row.get(req):
                raise PostingError(f"Each account needs {req}")
        rows.append(row)
    acts = [r["ad_account_id"] for r in rows]
    if len(set(acts)) != len(acts):
        raise PostingError("An ad account is listed twice")
    owned = set(await db["client_groups"].distinct(
        "meta_ad_account_id", {"user_id": user_id, "meta_ad_account_id": {"$in": acts}}))
    if missing := [a for a in acts if a not in owned]:
        raise PostingError(f"Unknown ad account: {', '.join(missing)}")
    if creative_format(test) != "image":
        for act in acts:
            if media_plan(test, act)[0] == "unavailable":
                raise PostingError(f"{VIDEO_NEEDS_UPLOAD} ({act})")

    # One atomic claim on the test doc: the status check plus `posted_accounts` make a
    # double click (or two overlapping add-accounts calls) a 409 instead of a second ad.
    query = {"id": test_id, "user_id": user_id, "posted_accounts": {"$nin": acts}}
    update: dict = {"$addToSet": {"posted_accounts": {"$each": acts}},
                    "$set": {"updated_at": now}}
    if add_accounts:
        if test.get("status") in (DRAFT, ARCHIVED):
            raise PostingError("Accounts can only be added to a posted, unarchived test", 409)
        existing = set(await db[TEST_ADS].distinct(
            "ad_account_id", {"test_id": test_id, "user_id": user_id}))
        if dup := [a for a in acts if a in existing]:
            raise PostingError(f"Already in this test: {', '.join(dup)}", 409)
        query["status"] = {"$nin": [DRAFT, ARCHIVED]}
        conflict = "One of these accounts is already in this test"
    else:
        query["status"] = DRAFT
        update["$set"].update(status=POSTING, posted_at=now)
        conflict = "This test has already been posted"
    if await db[TESTS].find_one_and_update(query, update) is None:
        raise PostingError(conflict, 409)

    batch = uuid.uuid4().hex
    try:
        ads = await add_test_ads(db, user_id, test_id,
                                 [{**r, "meta": {}, "post_state": "pending", "post_error": None,
                                   "post_batch": batch} for r in rows])
        jobs = [{**_new_job(user_id, test_id, a["id"], now), "post_batch": batch, **_managed_job(test)} for a in ads]
        await db[POST_JOBS].insert_many([dict(j) for j in jobs])
    except Exception:
        # Undo the claim so the user can press Post again.
        scope = {"user_id": user_id, "test_id": test_id, "post_batch": batch}
        await db[POST_JOBS].delete_many(scope)
        await db[TEST_ADS].delete_many(scope)
        undo: dict = {"$pull": {"posted_accounts": {"$in": acts}}}
        if not add_accounts:
            undo["$set"] = {"status": DRAFT}
            undo["$unset"] = {"posted_at": ""}
        await db[TESTS].update_one({"id": test_id, "user_id": user_id}, undo)
        raise
    return {"test_id": test_id, "queued": len(jobs), "ads": ads}


# ── worker ───────────────────────────────────────────────────────────────────

def _media(creative: dict) -> dict:
    return {**{k: creative.get(k) for k in ("blob_url", "image_hash", "video_id",
                                            "thumbnail_url", "image_url") if creative.get(k)},
            **(creative.get("media") or {})}


def _norm_act(act) -> str:
    act = str(act or "").strip()
    return act[4:] if act.startswith("act_") else act


def media_plan(test: dict, act: str) -> tuple[str, str | None]:
    """How to get media into `act`: ("reuse", id) existing Meta media (no upload);
    ("blob", url) Birdy upload; ("meta_cdn", url) re-upload the source ad's image (revive into
    another account: image hashes / video ids are account-scoped); ("meta_source", video id) the
    source ad's video, and ("meta_image", hash) the source ad's image, whose short-lived URLs are
    read at upload; ("unavailable", None)."""
    creative = test.get("creative") or {}
    media = _media(creative)
    source = test.get("source")
    src_act = _norm_act(source.get("ad_account_id")) if isinstance(source, dict) else ""
    same_account = not src_act or src_act == _norm_act(act)
    if creative_format(test) == "image":
        if media.get("image_hash") and same_account:
            return "reuse", media["image_hash"]
        if media.get("blob_url"):
            return "blob", media["blob_url"]
        if media.get("image_url"):
            return "meta_cdn", media["image_url"]
        if media.get("source_image_hash"):
            return "meta_image", media["source_image_hash"]
        if media.get("image_hash"):
            return "reuse", media["image_hash"]
        return "unavailable", None
    # No thumbnail is fine: the creative step reads Meta's own (video_thumbnail).
    if media.get("video_id") and same_account:
        return "reuse", media["video_id"]
    if media.get("blob_url"):
        return "blob", media["blob_url"]
    if media.get("source_video_id"):
        return "meta_source", media["source_video_id"]
    return "unavailable", None


def _own_thumb(media: dict) -> str | None:
    return media.get("thumbnail_url") or media.get("image_url")


def _media_ref(test: dict, ad: dict):
    creative = test.get("creative") or {}
    meta = ad.get("meta") or {}
    if creative_format(test) == "image":
        return meta["image_hash"]
    media = _media(creative)
    ref = {"video_id": meta["video_id"]}
    thumb = _own_thumb(media) or meta.get("thumbnail_url")
    if thumb:
        ref["image_url"] = thumb
    elif media.get("image_hash"):
        ref["image_hash"] = media["image_hash"]
    return ref


async def _set_meta(db, ad: dict, key: str, value: str, now: datetime, **extra) -> None:
    ad.setdefault("meta", {})[key] = value
    await db[TEST_ADS].update_one(
        {"id": ad["id"], "user_id": ad["user_id"]},
        {"$set": {f"meta.{key}": value, "updated_at": now, **extra}})


class _Tick:
    """Clock for one run_post_jobs call: `now()` is the tick's wall time plus the monotonic
    time elapsed since it started, so a job claimed late in a tick still gets a full lease."""

    def __init__(self, now: datetime, clock, deadline: float | None):
        self.base = now
        self.clock = clock
        self.start = clock()
        self.deadline = deadline
        self.video_uploads = 0

    def now(self) -> datetime:
        return self.base + timedelta(seconds=max(self.clock() - self.start, 0))

    def out_of_time(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline


def _job_key(job: dict) -> dict:
    """Filter for writes by the worker holding this claim; a stale worker matches nothing."""
    return {"id": job["id"], "user_id": job["user_id"], "lease_id": job["lease_id"]}


async def _run_step(db, job: dict, test: dict, ad: dict, token: str, tick: _Tick) -> str:
    """Run the next missing step. Returns the step name, "wait" (video processing),
    "deferred" (video upload budget used this tick), or "created" once the ad exists.
    Raises MetaWriteError tagged with the pipeline step."""
    creative = test.get("creative") or {}
    meta = ad.get("meta") or {}
    act = ad["ad_account_id"]
    fmt = creative_format(test)
    name = test.get("ad_name") or test["meta_ad_name"]
    step = "media"
    try:
        if not meta.get("image_hash" if fmt == "image" else "video_id"):
            how, ref = media_plan(test, act)
            if how == "unavailable":
                raise MetaWriteError(VIDEO_NEEDS_UPLOAD if fmt != "image"
                                     else "This creative has no image to upload")
            if how == "reuse":
                value = ref
            elif fmt == "image":
                if how == "meta_image":
                    how, ref = "meta_cdn", await image_url_for_hash(test["source"]["ad_account_id"], token, ref)
                    if not ref:
                        raise MetaWriteError(IMAGE_NEEDS_UPLOAD)
                value = await upload_image(act, token, image_url=ref,
                                           allow_meta_cdn=how == "meta_cdn")
            else:
                if tick.video_uploads >= MAX_VIDEO_UPLOADS_PER_TICK:
                    return "deferred"
                tick.video_uploads += 1
                res = await db[POST_JOBS].update_one(
                    _job_key(job), {"$set": {"lease_until": tick.now() + VIDEO_UPLOAD_LEASE}})
                if res.matched_count == 0:  # lost the claim; another worker owns it now
                    return "stale"
                cdn: dict = {}
                if how == "meta_source":
                    ref, cdn = await video_source(ref, token), {"allow_meta_cdn": True}
                    if not ref:
                        raise MetaWriteError(VIDEO_NEEDS_UPLOAD)
                value = await upload_video(act, token, file_url=ref, name=name, **cdn)
            await _set_meta(db, ad, "image_hash" if fmt == "image" else "video_id", value,
                            tick.now())
            return step

        step = "creative"
        if not meta.get("creative_id"):
            if fmt != "image" and not meta.get("video_ready"):
                if not await video_ready(meta["video_id"], token):
                    return "wait"
                await _set_meta(db, ad, "video_ready", True, tick.now())
            media = _media(creative)
            if (fmt != "image" and not _own_thumb(media) and not media.get("image_hash")
                    and not meta.get("thumbnail_url")):
                # Wizard and chat uploads carry no thumbnail; Meta makes some once the
                # video is processed. None yet: wait like an unprocessed video.
                thumb = await video_thumbnail(meta["video_id"], token)
                if not thumb:
                    return "wait"
                await _set_meta(db, ad, "thumbnail_url", thumb, tick.now())
            dest = ad.get("destination") or creative.get("destination") or "website"
            value = await create_adcreative(
                act, token, name=name, page_id=ad["page_id"], fmt=fmt,
                media_ref=_media_ref(test, ad),
                primary_text=creative.get("primary_text") or "",
                headline=creative.get("headline") or "",
                description=creative.get("description") or "",
                cta_type=creative.get("cta_type") or "LEARN_MORE",
                destination=dest,
                website_url=ad.get("website_url") or creative.get("website_url"),
                url_params=creative.get("url_params"),
                lead_form_id=ad.get("lead_form_id"),
                whatsapp_number=creative.get("whatsapp_number"),
            )
            await _set_meta(db, ad, "creative_id", value, tick.now())
            return step

        step = "ad"
        if not meta.get("ad_id"):
            managed = bool(test.get("managed_by"))
            value = await _stray_ad(db, ad, name, token, tick) if managed else None
            try:
                value = value or await create_ad(act, token, name=name, adset_id=ad["adset_id"],
                                                 creative_id=meta["creative_id"])
            except MetaWriteError as e:
                if managed and e.code is not None and not e.retryable:  # Meta said no, so there's no ad
                    meta.pop("ad_create_started_at", None)
                    await db[TEST_ADS].update_one({"id": ad["id"], "user_id": ad["user_id"]},
                                                  {"$unset": {"meta.ad_create_started_at": ""}})
                raise
            await _set_meta(db, ad, "ad_id", value, tick.now(), post_state="created",
                            post_error=None)
        return "created"
    except MetaWriteError as e:
        e.step = step
        if test.get("managed_by") and not e.user_msg and str(e).startswith("Meta request failed"):
            e.user_msg = AD_REPLY_LOST if step == "ad" else REPLY_LOST
        raise


async def _stray_ad(db, ad: dict, name: str, token: str, tick: _Tick) -> str | None:
    """Managed tests only. `create_ad` has no way to be repeated safely, so its start is stored first, as with
    `write_started_at`. Coming back to that mark, the ad may exist: a lost reply or a kill. It's adopted if
    Meta has it, and if Meta can't say, nothing is created."""
    meta = ad.get("meta") or {}
    if not meta.get("ad_create_started_at"):
        await _set_meta(db, ad, "ad_create_started_at", tick.now(), tick.now())
        return None
    try:
        return await find_ad_in_adset(ad["adset_id"], token, name=name, creative_id=meta["creative_id"])
    except MetaWriteError as e:
        if e.code == 190:
            raise
        logger.warning(f"[creatives-post] can't look for an ad in {ad['adset_id']}: {e}")
        raise _CheckBackOff(AD_UNCONFIRMED) from e


def _post_error(e: MetaWriteError) -> dict:
    info = explain(e)
    return {"code": e.code, "subcode": e.subcode, "message": info["message"],
            "step": e.step, "fix": info["fix"]}


async def _claim(db, now: datetime, exclude: list[str]) -> dict | None:
    """Claim the next due job. A RUNNING job whose lease ran out lost its worker mid-step
    (crash, or a video upload outliving its lease): that reclaim counts as an attempt, so
    a step that keeps dying is failed after MAX_ATTEMPTS instead of re-running forever."""
    lease_id = uuid.uuid4().hex
    before = await db[POST_JOBS].find_one_and_update(
        {"id": {"$nin": exclude}, "state": {"$in": [QUEUED, RUNNING]},
         "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}]},
        {"$set": {"state": RUNNING, "lease_until": now + LEASE, "lease_id": lease_id,
                  "updated_at": now}},
        sort=[("managed", 1), ("created_at", 1)],
        return_document=ReturnDocument.BEFORE,
    )
    if before is None:
        return None
    job = {**before, "state": RUNNING, "lease_until": now + LEASE, "lease_id": lease_id,
           "updated_at": now}
    if before.get("state") == RUNNING:
        job["attempts"] = job.get("attempts", 0) + 1
        job["reclaimed"] = True
        await db[POST_JOBS].update_one(_job_key(job), {"$set": {"attempts": job["attempts"]}})
    return job


async def _finish(db, job: dict, now: datetime, **fields) -> bool:
    """Release or close the claimed job; False (no-op) when this worker lost the claim."""
    res = await db[POST_JOBS].update_one(
        _job_key(job), {"$set": {**fields, "lease_id": None, "updated_at": now}})
    return res.matched_count > 0


async def _fail(db, job: dict, ad: dict, err: dict, now: datetime) -> None:
    err = {**err, "at": err.get("at") or now}  # identifies this failure (its suggestion key)
    if not await _finish(db, job, now, state=FAILED, lease_until=None, last_error=err):
        return
    await db[TEST_ADS].update_one(
        {"id": ad["id"], "user_id": ad["user_id"]},
        {"$set": {"post_state": "failed", "post_error": err, "updated_at": now}})
    await _sync_suggestions(db, ad["user_id"], job["test_id"])


async def _sync_suggestions(db, user_id: str, test_id: str) -> None:
    """Refresh the test's suggestions from its rows (a failed row is the
    creative_post_failed finding); never fails the posting pipeline."""
    try:
        from ai.suggestions import creative_findings

        test = await get_test(db, user_id, test_id)
        if test is not None:
            ads = await list_test_ads(db, test_id, user_id=user_id)
            await creative_findings.sync_creative_suggestions(db, user_id, test, ads)
    except Exception as e:
        logger.error(f"[creatives-post] suggestions for {test_id} failed: {e}", exc_info=True)


async def _after_created(db, mongo_client, test: dict, ad: dict, now: datetime) -> dict | None:
    """Auto go-live: a posting test once every ad is created; a live test per ad. An agent's ad stays
    paused here: stock/add.py checks everything again, under the client's lease, before turning it on."""
    if test.get("managed_by"):
        return None
    fresh = await get_test(db, test["user_id"], test["id"])
    status = (fresh or {}).get("status")
    if status in LIVE_STATES:
        ad = (await db[TEST_ADS].find_one({"id": ad["id"], "user_id": ad["user_id"]})) or ad
        return await _activate(db, mongo_client, test["user_id"], fresh, [ad], now)
    if status == POSTING:
        ads = await list_test_ads(db, test["id"], user_id=test["user_id"])
        if all(a.get("post_state") in ("created", "active", "removed") for a in ads):
            return await go_live(db, mongo_client, test["user_id"], test["id"], now=now)
    return None


async def _process(db, mongo_client, job: dict, tick: _Tick, tokens: dict) -> str:
    uid = job["user_id"]
    ad = await db[TEST_ADS].find_one({"id": job["test_ad_id"], "user_id": uid})
    test = await get_test(db, uid, job["test_id"])
    if ad is None or test is None or test.get("status") == ARCHIVED:
        await _finish(db, job, tick.now(), state=DONE, lease_until=None, note="test or ad gone")
        return "skipped"
    if ad.get("post_state") not in ("pending", "uploading"):
        await _finish(db, job, tick.now(), state=DONE, lease_until=None)
        return "skipped"
    if test.get("managed_by"):
        from ai.agents.stock.settings import add_writes_enabled

        if not add_writes_enabled():  # the kill switch stops what's already queued too
            await _fail(db, job, ad, {"code": None, "subcode": None, "step": None, "fix": None,
                                      "message": MANAGED_WRITES_OFF}, tick.now())
            return "failed"
    if ad.get("post_state") == "pending":
        await db[TEST_ADS].update_one({"id": ad["id"], "user_id": uid},
                                      {"$set": {"post_state": "uploading", "updated_at": tick.now()}})

    if job.get("reclaimed") and job.get("attempts", 0) >= MAX_ATTEMPTS:
        await _fail(db, job, ad, {"code": None, "subcode": None, "step": None, "fix": "retry_later",
                                  "message": "Posting stalled: Meta did not answer in time. Retry later."},
                    tick.now())
        return "failed"

    if uid not in tokens:
        tokens[uid] = ((await get_facebook_token(uid, mongo_client)) or {}).get("access_token")
    token = tokens[uid]
    try:
        if not token:
            raise MetaWriteError("Meta is not connected", code=190)
        result = await _run_step(db, job, test, ad, token, tick)
    except MetaWriteError as e:
        now = tick.now()
        err = _post_error(e)
        attempts = job.get("attempts", 0) + 1
        if (e.retryable or isinstance(e, _CheckBackOff)) and attempts < MAX_ATTEMPTS:
            await _finish(db, job, now, state=QUEUED, attempts=attempts, last_error=err,
                          lease_until=now + BACKOFF_BASE * (2 ** attempts))
            return "backoff"
        await _fail(db, job, ad, err, now)
        return "failed"
    except Exception as e:  # a bug must not wedge the job under a lease forever
        logger.error(f"[creatives-post] job {job['id']} crashed: {e}", exc_info=True)
        await _fail(db, job, ad, {"code": None, "subcode": None, "message": str(e)[:300],
                                  "step": None, "fix": None}, tick.now())
        return "failed"

    now = tick.now()
    if result == "stale":
        return "stale"
    if result == "deferred":
        await _finish(db, job, now, state=QUEUED, lease_until=None)
        return "deferred"
    if result == "wait":
        polls = job.get("video_polls", 0) + 1
        if polls >= MAX_VIDEO_POLLS:
            await _fail(db, job, ad, {"code": None, "subcode": None, "step": "media", "fix": None,
                                      "message": "Meta is still processing the video. Retry later."},
                        now)
            return "failed"
        await _finish(db, job, now, state=QUEUED, lease_until=None, video_polls=polls)
        return "waiting"
    if result == "created":
        await _finish(db, job, now, state=DONE, lease_until=None, attempts=0)
        try:
            await _after_created(db, mongo_client, test, ad, now)
        except Exception as e:
            logger.error(f"[creatives-post] go-live after {ad['id']} failed: {e}", exc_info=True)
        return "created"
    await _finish(db, job, now, state=QUEUED, lease_until=None, attempts=0)
    return "advanced"


async def run_post_jobs(db, mongo_client, now_utc: datetime, *, limit: int = 5,
                        deadline: float | None = None, clock=None) -> dict:
    """Claim up to `limit` jobs (each at most once per call) and run one step for each.
    Stops claiming once `deadline` (a `clock()` value, default time.monotonic) passes;
    at most one video upload per call."""
    tick = _Tick(_naive(now_utc), clock or time.monotonic, deadline)
    counts = {"claimed": 0, "advanced": 0, "created": 0, "waiting": 0, "backoff": 0,
              "failed": 0, "skipped": 0, "deferred": 0, "stale": 0}
    tokens: dict[str, str | None] = {}
    seen: list[str] = []
    while counts["claimed"] < limit and not tick.out_of_time():
        job = await _claim(db, tick.now(), seen)
        if job is None:
            break
        seen.append(job["id"])
        counts["claimed"] += 1
        counts[await _process(db, mongo_client, job, tick, tokens)] += 1
    return counts


# ── go live / pause / retry ──────────────────────────────────────────────────

async def _activate(db, mongo_client, user_id: str, test: dict, ads: list[dict],
                    now: datetime) -> dict:
    """Set each created ad ACTIVE. Status writes are not queued for retry: a failure is
    recorded on the ad (post_state stays "created") and returned."""
    activated, errors = 0, []
    for ad in ads:
        ad_id = (ad.get("meta") or {}).get("ad_id")
        if ad.get("post_state") != "created" or not ad_id:
            continue
        if ad.get("meta_status") == "PAUSED":  # a pause wins over auto go-live
            continue
        try:
            await set_object_status(user_id, ad_id, "ad", "ACTIVE", mongo_client)
        except MetaStatusError as e:
            err = {"code": e.status_code, "subcode": None, "message": e.message,
                   "step": "go_live", "fix": "reconnect_meta" if e.status_code == 401 else None}
            await db[TEST_ADS].update_one({"id": ad["id"], "user_id": user_id},
                                          {"$set": {"post_error": err, "updated_at": now}})
            errors.append({"test_ad_id": ad["id"], "ad_account_id": ad.get("ad_account_id"), **err})
            continue
        await db[TEST_ADS].update_one(
            {"id": ad["id"], "user_id": user_id},
            {"$set": {"post_state": "active", "post_error": None, "meta_status": "ACTIVE",
                      "live_at": now, "updated_at": now}})
        activated += 1
    if activated and test.get("status") not in LIVE_STATES:
        fields = {"status": TESTING, "updated_at": now}
        if not test.get("started_at"):
            fields["started_at"] = now
        await db[TESTS].update_one({"id": test["id"], "user_id": user_id}, {"$set": fields})
    return {"activated": activated, "errors": errors}


async def ensure_verdict_alert(db, user_id: str, test: dict) -> None:
    """Create the "notify when a verdict lands" alert once (stored as on_verdict.alert_id)."""
    on_verdict = test.get("on_verdict") or {}
    if not on_verdict.get("notify_channels"):
        return
    if on_verdict.get("alert_id") and await db["alerts"].find_one(
            {"id": on_verdict["alert_id"], "user_id": user_id}, {"id": 1}):
        return
    try:
        from services.creatives.alerts import create_verdict_alert
        alert_id = await create_verdict_alert(db, user_id, test)
        await update_test(db, user_id, test["id"], {"on_verdict": {**on_verdict, "alert_id": alert_id}})
    except Exception as e:
        logger.error(f"[creatives] verdict alert for {test.get('id')} failed: {e}", exc_info=True)


async def go_live(db, mongo_client, user_id: str, test_id: str, *, partial_ok: bool = False,
                  now: datetime | None = None) -> dict:
    now = _naive(now)
    test = await _owned_test(db, user_id, test_id)
    if test.get("status") not in (POSTING, *LIVE_STATES):
        raise PostingError("This test has not been posted", 409)
    ads = await list_test_ads(db, test_id, user_id=user_id)
    created = [a for a in ads if a.get("post_state") == "created"]
    if not created:
        raise PostingError("No ads are ready to go live", 409)
    waiting = [a for a in ads if a.get("post_state") not in ("created", "active", "removed")]
    if waiting and not partial_ok:
        raise PostingError("Some ads are not created yet", 409)
    res = await _activate(db, mongo_client, user_id, test, created, now)
    await ensure_verdict_alert(db, user_id, test)
    return {"test_id": test_id, **res}


async def pause_ads(db, mongo_client, user_id: str, test_id: str,
                    ad_account_ids: list[str] | None, *, ad_ids: list[str] | None = None,
                    now: datetime | None = None) -> dict:
    """Pause the test's live ads (optionally only some accounts, and/or only some Meta
    ad ids). Tracking continues: post_state stays "active", meta_status records PAUSED."""
    now = _naive(now)
    await _owned_test(db, user_id, test_id)
    ads = await list_test_ads(db, test_id, user_id=user_id)
    paused, errors = 0, []
    for ad in ads:
        ad_id = (ad.get("meta") or {}).get("ad_id")
        if not ad_id or ad.get("post_state") not in ("created", "active"):
            continue
        if ad_account_ids is not None and ad.get("ad_account_id") not in ad_account_ids:
            continue
        if ad_ids is not None and ad_id not in ad_ids:
            continue
        # No skip on a cached PAUSED: the ad may have been set live in Meta since
        # (tracking refreshes meta_status). Pausing a paused ad is a no-op in Meta.
        try:
            await set_object_status(user_id, ad_id, "ad", "PAUSED", mongo_client)
        except MetaStatusError as e:
            errors.append({"test_ad_id": ad["id"], "ad_account_id": ad.get("ad_account_id"),
                           "code": e.status_code, "message": e.message, "step": "pause"})
            continue
        await db[TEST_ADS].update_one(
            {"id": ad["id"], "user_id": user_id},
            {"$set": {"meta_status": "PAUSED", "paused_at": now, "updated_at": now}})
        paused += 1
    return {"test_id": test_id, "paused": paused, "errors": errors}


async def retry(db, user_id: str, test_id: str, test_ad_id: str, *, patch: dict | None = None,
                now: datetime | None = None) -> dict:
    """Re-queue a failed ad from its failed step. Patching a creative field (destination,
    website URL, lead form, Page) drops the stored creative so it is rebuilt."""
    now = _naive(now)
    test = await _owned_test(db, user_id, test_id)
    if test.get("status") == ARCHIVED:
        raise PostingError("This test is archived", 409)
    ad = await db[TEST_ADS].find_one({"id": test_ad_id, "test_id": test_id, "user_id": user_id})
    if ad is None:
        raise PostingError("Ad not found", 404)
    if ad.get("post_state") != "failed":
        raise PostingError("Only failed ads can be retried", 409)
    patch = patch or {}
    if unknown := sorted(set(patch) - set(PATCH_FIELDS)):
        raise PostingError(f"Cannot change: {', '.join(unknown)}")
    if patch.get("destination") == "website" and not (
            patch.get("website_url") or ad.get("website_url")
            or (test.get("creative") or {}).get("website_url")):
        raise PostingError("A website URL is required")
    update: dict = {"$set": {**patch, "post_state": "pending", "post_error": None,
                             "updated_at": now}}
    meta = ad.get("meta") or {}
    if any(k in patch for k in CREATIVE_FIELDS) and meta.get("creative_id"):
        update["$unset"] = {"meta.creative_id": ""}
    await db[TEST_ADS].update_one({"id": test_ad_id, "user_id": user_id}, update)
    reset = {"state": QUEUED, "attempts": 0, "lease_until": None, "lease_id": None,
             "video_polls": 0, "updated_at": now}
    res = await db[POST_JOBS].update_one({"test_ad_id": test_ad_id, "user_id": user_id},
                                         {"$set": reset, "$unset": {"last_error": ""}})
    if res.matched_count == 0:
        await db[POST_JOBS].insert_one(_new_job(user_id, test_id, test_ad_id, now))
    await _sync_suggestions(db, user_id, test_id)  # the failure is no longer open
    return {"test_id": test_id, "test_ad_id": test_ad_id, "queued": True}
