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
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_writer import (
    MetaWriteError,
    create_ad,
    create_adcreative,
    upload_image,
    upload_video,
    video_ready,
)
from services.creatives.errors import explain
from services.creatives.store import (
    ARCHIVED,
    DRAFT,
    FINAL_VERDICTS,
    POST_JOBS,
    POSTING,
    TEST_ADS,
    TESTING,
    TESTS,
    add_test_ads,
    get_test,
    list_test_ads,
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

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

ACCOUNT_FIELDS = ("ad_account_id", "client_group_id", "adset_id", "adset_name", "page_id",
                  "lead_form_id", "targets_override", "campaign_id")
PATCH_FIELDS = ("destination", "website_url", "lead_form_id", "page_id", "adset_id", "adset_name")
CREATIVE_FIELDS = ("destination", "website_url", "lead_form_id", "page_id")
LIVE_STATES = (TESTING, *FINAL_VERDICTS)


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

    ads = await add_test_ads(db, user_id, test_id,
                             [{**r, "meta": {}, "post_state": "pending", "post_error": None}
                              for r in rows])
    jobs = [_new_job(user_id, test_id, a["id"], now) for a in ads]
    await db[POST_JOBS].insert_many([dict(j) for j in jobs])
    return {"test_id": test_id, "queued": len(jobs), "ads": ads}


# ── worker ───────────────────────────────────────────────────────────────────

def _media(creative: dict) -> dict:
    return {**{k: creative.get(k) for k in ("blob_url", "image_hash", "video_id",
                                            "thumbnail_url", "image_url") if creative.get(k)},
            **(creative.get("media") or {})}


def _reusable_media(test: dict, ad: dict) -> dict | None:
    """Existing Meta media on a revive creative (no download/upload). Image hashes and
    video ids are account-scoped, so when the target account differs from the source and
    a Birdy upload exists, upload that instead."""
    creative = test.get("creative") or {}
    media = _media(creative)
    fmt = creative.get("format", "image")
    src_act = (test.get("source") or {}).get("ad_account_id")
    if media.get("blob_url") and src_act and src_act != ad["ad_account_id"]:
        return None
    if fmt == "image" and media.get("image_hash"):
        return {"image_hash": media["image_hash"]}
    thumb = media.get("thumbnail_url") or media.get("image_url")
    if fmt != "image" and media.get("video_id") and (thumb or media.get("image_hash")):
        return {"video_id": media["video_id"]}
    return None


def _media_ref(test: dict, ad: dict):
    creative = test.get("creative") or {}
    meta = ad.get("meta") or {}
    if creative.get("format", "image") == "image":
        return meta["image_hash"]
    media = _media(creative)
    ref = {"video_id": meta["video_id"]}
    thumb = media.get("thumbnail_url") or media.get("image_url")
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


async def _run_step(db, job: dict, test: dict, ad: dict, token: str, now: datetime) -> str:
    """Run the next missing step. Returns the step name, or "wait" (video processing),
    or "created" once the ad exists. Raises MetaWriteError tagged with the pipeline step."""
    creative = test.get("creative") or {}
    meta = ad.get("meta") or {}
    act = ad["ad_account_id"]
    fmt = creative.get("format", "image")
    name = test["meta_ad_name"]
    step = "media"
    try:
        if fmt == "image" and not meta.get("image_hash"):
            reuse = _reusable_media(test, ad)
            value = reuse["image_hash"] if reuse else await upload_image(
                act, token, image_url=_media(creative).get("blob_url"))
            await _set_meta(db, ad, "image_hash", value, now)
            return step
        if fmt != "image" and not meta.get("video_id"):
            reuse = _reusable_media(test, ad)
            if reuse:
                value = reuse["video_id"]
            else:
                await db[POST_JOBS].update_one(
                    {"id": job["id"]}, {"$set": {"lease_until": now + VIDEO_UPLOAD_LEASE}})
                value = await upload_video(act, token, file_url=_media(creative).get("blob_url"),
                                           name=name)
            await _set_meta(db, ad, "video_id", value, now)
            return step

        step = "creative"
        if not meta.get("creative_id"):
            if fmt != "image" and not meta.get("video_ready"):
                if not await video_ready(meta["video_id"], token):
                    return "wait"
                await _set_meta(db, ad, "video_ready", True, now)
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
            await _set_meta(db, ad, "creative_id", value, now)
            return step

        step = "ad"
        if not meta.get("ad_id"):
            value = await create_ad(act, token, name=name, adset_id=ad["adset_id"],
                                    creative_id=meta["creative_id"])
            await _set_meta(db, ad, "ad_id", value, now, post_state="created", post_error=None)
        return "created"
    except MetaWriteError as e:
        e.step = step
        raise


def _post_error(e: MetaWriteError) -> dict:
    info = explain(e)
    return {"code": e.code, "subcode": e.subcode, "message": info["message"],
            "step": e.step, "fix": info["fix"]}


async def _claim(db, now: datetime, exclude: list[str]) -> dict | None:
    return await db[POST_JOBS].find_one_and_update(
        {"id": {"$nin": exclude}, "state": {"$in": [QUEUED, RUNNING]},
         "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}]},
        {"$set": {"state": RUNNING, "lease_until": now + LEASE, "updated_at": now}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


async def _finish(db, job: dict, now: datetime, **fields) -> None:
    await db[POST_JOBS].update_one({"id": job["id"]}, {"$set": {**fields, "updated_at": now}})


async def _fail(db, job: dict, ad: dict, err: dict, now: datetime) -> None:
    await db[TEST_ADS].update_one(
        {"id": ad["id"], "user_id": ad["user_id"]},
        {"$set": {"post_state": "failed", "post_error": err, "updated_at": now}})
    await _finish(db, job, now, state=FAILED, lease_until=None, last_error=err)


async def _after_created(db, mongo_client, test: dict, ad: dict, now: datetime) -> dict | None:
    """Auto go-live: a posting test once every ad is created; a live test per ad."""
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


async def _process(db, mongo_client, job: dict, now: datetime, tokens: dict) -> str:
    uid = job["user_id"]
    ad = await db[TEST_ADS].find_one({"id": job["test_ad_id"], "user_id": uid})
    test = await get_test(db, uid, job["test_id"])
    if ad is None or test is None or test.get("status") == ARCHIVED:
        await _finish(db, job, now, state=DONE, lease_until=None, note="test or ad gone")
        return "skipped"
    if ad.get("post_state") not in ("pending", "uploading"):
        await _finish(db, job, now, state=DONE, lease_until=None)
        return "skipped"
    if ad.get("post_state") == "pending":
        await db[TEST_ADS].update_one({"id": ad["id"], "user_id": uid},
                                      {"$set": {"post_state": "uploading", "updated_at": now}})

    if uid not in tokens:
        tokens[uid] = ((await get_facebook_token(uid, mongo_client)) or {}).get("access_token")
    token = tokens[uid]
    try:
        if not token:
            raise MetaWriteError("Meta is not connected", code=190)
        result = await _run_step(db, job, test, ad, token, now)
    except MetaWriteError as e:
        err = _post_error(e)
        attempts = job.get("attempts", 0) + 1
        if e.retryable and attempts < MAX_ATTEMPTS:
            await _finish(db, job, now, state=QUEUED, attempts=attempts, last_error=err,
                          lease_until=now + BACKOFF_BASE * (2 ** attempts))
            return "backoff"
        await _fail(db, job, ad, err, now)
        return "failed"
    except Exception as e:  # a bug must not wedge the job under a lease forever
        logger.error(f"[creatives-post] job {job['id']} crashed: {e}", exc_info=True)
        await _fail(db, job, ad, {"code": None, "subcode": None, "message": str(e)[:300],
                                  "step": None, "fix": None}, now)
        return "failed"

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


async def run_post_jobs(db, mongo_client, now_utc: datetime, *, limit: int = 5) -> dict:
    """Claim up to `limit` jobs (each at most once per call) and run one step for each."""
    now = _naive(now_utc)
    counts = {"claimed": 0, "advanced": 0, "created": 0, "waiting": 0, "backoff": 0,
              "failed": 0, "skipped": 0}
    tokens: dict[str, str | None] = {}
    seen: list[str] = []
    while counts["claimed"] < limit:
        job = await _claim(db, now, seen)
        if job is None:
            break
        seen.append(job["id"])
        counts["claimed"] += 1
        counts[await _process(db, mongo_client, job, now, tokens)] += 1
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
    return {"test_id": test_id, **res}


async def pause_ads(db, mongo_client, user_id: str, test_id: str,
                    ad_account_ids: list[str] | None, *, now: datetime | None = None) -> dict:
    """Pause the test's live ads (optionally only some accounts). Tracking continues:
    post_state stays "active", meta_status records PAUSED."""
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
        if ad.get("meta_status") == "PAUSED":
            continue
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
    reset = {"state": QUEUED, "attempts": 0, "lease_until": None, "video_polls": 0,
             "updated_at": now}
    res = await db[POST_JOBS].update_one({"test_ad_id": test_ad_id, "user_id": user_id},
                                         {"$set": reset, "$unset": {"last_error": ""}})
    if res.matched_count == 0:
        await db[POST_JOBS].insert_one(_new_job(user_id, test_id, test_ad_id, now))
    return {"test_id": test_id, "test_ad_id": test_ad_id, "queued": True}
