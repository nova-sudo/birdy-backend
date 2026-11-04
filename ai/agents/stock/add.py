"""
ai/agents/stock/add.py
----------------------
An Add line: posting a winning ad from another client into this one, through the Creatives posting
pipeline (spec section 7). It writes a creative test with `managed_by` set, which Creatives hides, and
the pipeline's job makes the media, the creative and the ad, then turns the ad on. All of it is behind
AGENT_ADD_WRITES_ENABLED: with that off Add resolves to Flag, a queued post stops, and Undo still works.

apply.py runs the claim, the refusals and the mode for an Add as for any line, then calls `apply_add`.
That's where it differs, since the post takes minutes and outlives the request:

  1. Checks. Destination, duplicates, the live read of the ad set, the live ad limit. Nothing is stored.
  2. `enqueue_add` takes the post's reservation, plans the media, stores `write_started_at`, and only
     then writes the test and its job.
  3. `sync_add_lines`, every minute, reads where the pipeline got to and moves the line to `applied`,
     `failed` or `needs_file`. Nothing else moves a line once it has the marker.
  4. The pipeline leaves the ad paused. A video can take an hour, so before turning it on `sync_add_lines`
     checks again under the client's lease: the agent, the hold, who acts for the client, the mode, a newer
     card, and the live ad limit from Meta's own list of the ad's ad set. If any of those says no, the ad
     stays paused, the line says why, and a person can Retry.

Never twice:

  One post per creative per ad account. `agent_add_posts` has one row for (user, account, creative key),
  whoever asks: a Retry, two cards, two lines. The line that holds it keeps it while it can still reach
  Meta, and while its ad exists in Meta. A line that failed with no ad gives it up to the next that asks.

  One post per line. A line's test is found through that row, so every way back in, a Retry or a resume
  after a kill, carries on with the same test. The pipeline never repeats a step whose id it has stored.
  `create_ad` is the step that can land with no id stored, so its start is marked, and from that mark on the
  ad set is read first. An ad that exists, or may, keeps its ad set and keeps its row: nothing can move it.

A kill can leave the line at any point after the marker. Once the invocation must be gone (STUCK_AFTER, or
`cut_at`), `sync_add_lines` finishes what's missing: a test that never got its row is posted, and a row that
never got its job gets one. Neither can have reached Meta, since only a job reaches Meta.

Media belongs to one ad account, so the source's hash or video id only goes in the test when the source is
in the destination's account. Otherwise a file held for that same source video or image is used, else Meta's
short-lived URL for it, read at the upload and never stored. Neither available means `needs_file`. Only files
a person uploaded are kept, under the source's account and media id, never its name: "Video 1" means
something else in every client.

The page, form, link and ad set are always the destination's own, from an ad in its main ad set that Meta
itself reads as live. The words and the media are the source's, and an ad with no words isn't posted. A person is shown a flag on the card for words naming another client.
"""

import logging
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from pymongo.errors import DuplicateKeyError

from ai.agents.guardrails import _naive_utc, agent_tz, check_adset_contents_live, check_guardrails, local_today
from ai.agents.meta_state import AD_STATE, ADSET_STATE
from ai.agents.snapshot import load_ad_days
from ai.agents.stock.apply import (_ACTIONABLE, NOW_FLAG, STUCK_AFTER, _answer, _block, _fail, _find_line, _lease_key,
                                   _load_agent, _log_once, _lost_the_last_check, _no_time_for_a_line, _out_of_time,
                                   _refusal, _release_lease, _set_line, _settings, _short, _take_lease, _target_doc,
                                   _visible_card, _who, adds_in_flight, has_newer_card, running_ads)
from ai.agents.stock.candidates import _DATE_RUN, _DATE_SEP, _is_date, creative_key
from ai.agents.stock.cards import (STATE_APPLIED, STATE_APPLYING, STATE_BLOCKED, STATE_FAILED, STATE_NEEDS_FILE,
                                    STATE_PROPOSED, STATE_QUEUED, STATE_UNDOING, STATE_UNDONE)
from ai.agents.stock.checks import _live_by_adset, in_scope, scoped_days, totals
from ai.agents.stock.scoreboard import record_add
from ai.agents.stock.settings import add_writes_enabled, resolve_mode
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.store import CARDS, list_memory
from ai.suggestions.contracts import ACTION_ENABLE_ADS
from ai.suggestions.store import ACTOR_BIRDY, KIND_ACTION_APPLIED, KIND_AD_LEFT_PAUSED
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_writer import (VIDEO_PROCESS_FAILED, MetaWriteError, image_url_for_hash,
                                                              video_source)
from services.creatives import posting
from services.creatives.posting import DONE, FAILED, POST_JOBS, PostingError, _managed_job, _new_job, _norm_act, enqueue_post
from services.creatives.store import DRAFT, POSTING, TEST_ADS, TESTS, create_test, get_test, update_test
from services.creatives.upload_grant import is_own_blob_url

logger = logging.getLogger(__name__)

FILES = "agent_creative_files"     # one row per (user, source account, source media id): a file a person uploaded
POSTS = "agent_add_posts"          # one row per (user, ad account, creative key): who may post it
MANAGED_BY = "media_agent"
SYNC_BATCH = 200                   # cards looked at per cron pass; the rest wait a minute
GO_LIVE_TRIES = 5                  # one a minute: Meta is having a moment, or it won't happen
MEDIA_READ_TRIES = 5               # the same, reading the source's media before anything is posted
DEFAULT_WINDOW_DAYS = 7
HOLDING = (STATE_QUEUED, STATE_APPLYING, STATE_APPLIED, STATE_UNDOING, STATE_UNDONE)
_FILE_LINE_STATES = (STATE_NEEDS_FILE, STATE_PROPOSED, STATE_FAILED, STATE_BLOCKED)

WRITES_OFF = {"code": "add_writes_off", "message": "Posting new ads is switched off, so Birdy didn't post this."}
NO_DESTINATION = {"code": "no_destination",
                  "message": "Birdy couldn't find a live ad in this client's main ad set to copy the Page and form "
                             "from, so nothing was posted."}
UNSUPPORTED_LANDING = {"code": "unsupported_landing",
                       "message": "This client's live ads don't send people to a form, WhatsApp or a website, so Birdy "
                                  "can't tell where a new ad should go. Nothing was posted."}
ALREADY_POSTED = {"code": "already_posted", "message": "Birdy has already posted this creative to this ad account."}
PAUSED_ELSEWHERE = {"code": "already_posted",
                    "message": "Birdy already made this ad from an earlier card, and it's paused in Meta. "
                               "Turn it on in Ads Manager if you want it."}
MAYBE_PAUSED_ELSEWHERE = {"code": "already_posted",
                          "message": "Birdy may already have made this ad from an earlier card. If so, it's paused in Meta. "
                                     "Check Ads Manager."}
NO_BODY = {"code": "no_words", "message": "Birdy can't read any primary text on the source ad, so it didn't post a blank ad."}
NO_HEADLINE = {"code": "no_words", "message": "Birdy can't read a headline on the source ad, and this ad has no web page for "
                                              "Meta to take one from, so it didn't post it."}
NOW_FLAGGED = {"code": "now_flag", "message": NOW_FLAG}
NOT_AUTO_NOW = {"code": "not_auto", "message": "This change no longer runs by itself, so it's waiting for a person."}
ALREADY_RUN = {"code": "already_run", "message": "This ad account has already run this creative, so Birdy didn't post it again."}
NO_SOURCE = {"code": "no_source", "message": "Birdy doesn't know which creative this line means, so nothing was posted."}
NOT_CONNECTED = {"code": "no_token", "message": "Meta isn't connected, so nothing was posted. Reconnect Meta and try again."}
GONE_FROM_META = "This ad was removed in Meta before it went live."
NEEDS_FILE = "Meta won't share this video. Upload the video file to post it."
NEEDS_IMAGE_FILE = "Meta won't share this image. Upload the image file to post it."
VIDEO_REJECTED = "Meta couldn't process this video. Upload the video file to post it."
READ_FAILED = "Birdy couldn't read this ad's media from Meta. Nothing was changed. Try again in a few minutes."
NOT_THE_USERS = "That file isn't one of your uploads, so Birdy can't use it."
CANT_TAKE_FILE = "This line can't take a file right now."
POST_FAILED = "Meta didn't accept this ad. Try again."
STOPPED_WITH_AD = ("Posting new ads is switched off, so Birdy left this ad paused in Meta. "
                   "It needs turning on in Ads Manager, or trying again later.")
NOT_LIVE = "Birdy created this ad but couldn't turn it on, so it's paused in Meta: {why} Try again."
LEFT_PAUSED = "Birdy created this ad but left it paused in Meta: {why} Try again when that's changed."
CLOSED_PAUSED = ("Birdy created this ad and it's paused in Meta. This card was replaced or closed, so Birdy won't turn it "
                 "on. Turn it on in Ads Manager if you want it.")
CLOSED_MAYBE = ("Birdy may have created this ad, and if so it's paused in Meta. This card was replaced or closed, so Birdy "
                "won't look again. Check Ads Manager.")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

_MARK = "\x00"
_DASH = r"[-\u2013\u2014]"
_TAIL_COPY = re.compile(rf"(?:\s*{_DASH}\s*|\s+)?\(?\bcopy(?:\s*\d+)?\)?(?:\s*{_DASH}?\s*{_MARK})*\s*$", re.IGNORECASE)
_STRAY_MARK = re.compile(rf"\s*{_DASH}?\s*{_MARK}")
_DANGLING = re.compile(r"[\s\u2013\u2014-]+$")


def clean_ad_name(source_name: str | None, today: date) -> str:
    """The source's name, with today's date in place of its own and no "Copy" on the end. What Meta shows is
    kept as it is otherwise, and `normalise_name` gives the same answer for both, so a re-post still matches
    its source by name."""
    stamp = today.strftime("%m%d%y")
    text = _DATE_SEP.sub(_MARK, str(source_name or ""))
    text = _DATE_RUN.sub(lambda m: _MARK if _is_date(m.group()) else m.group(), text)
    while True:
        shorter = _TAIL_COPY.sub("", text)
        if shorter == text:
            break
        text = shorter
    if _MARK in text:
        text = text.replace(_MARK, stamp, 1)
        text = _STRAY_MARK.sub("", text)
    else:
        text = f"{_DANGLING.sub('', text)} - {stamp}" if text.strip() else stamp
    return _DANGLING.sub("", text).strip()


# ---------------------------------------------------------------------------
# Where it goes
# ---------------------------------------------------------------------------

def _window_days(window: dict | None) -> tuple[date, date]:
    try:
        return date.fromisoformat(window["start"]), date.fromisoformat(window["end"])
    except (TypeError, KeyError, ValueError):
        end = datetime.utcnow().date()
        return end - timedelta(days=DEFAULT_WINDOW_DAYS - 1), end


_WHATSAPP_HOSTS = ("whatsapp.com", "wa.me")
_NOT_WEBSITES = ("facebook.com", "fb.com", "fb.me", "m.me", "messenger.com", "instagram.com")


def _host_in(host: str, roots: tuple) -> bool:
    return any(host == root or host.endswith(f".{root}") for root in roots)


def _landing(creative: dict) -> dict | None:
    """Where this ad sends people, in the shape the pipeline wants. None when it can't be repeated: the pipeline
    only knows forms, WhatsApp and websites, and a Messenger or Page link isn't a website."""
    if creative.get("lead_form_id"):
        return {"destination": "lead_form", "website_url": None}
    link = creative.get("link")
    if not link:
        return None
    try:
        url = urlparse(str(link))
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not host:
        return None
    if _host_in(host, _WHATSAPP_HOSTS):
        return {"destination": "whatsapp", "website_url": link}
    return None if _host_in(host, _NOT_WEBSITES) else {"destination": "website", "website_url": link}


async def destination_for(db, group: dict, *, window: dict | None = None) -> dict | None:
    """The ad set, Page and form a new ad gets (spec section 7), or None if they can't be read."""
    return (await _pick_destination(db, group, window=window))[0]


async def _pick_destination(db, group: dict, *, window: dict | None = None,
                            live_ids: set[str] | None = None) -> tuple[dict | None, dict | None]:
    """(destination, why there's none). The ad set is the one `client_facts` calls main, over the same window
    when the card's is passed, and they come from its live ad spending most that has them. Never another ad
    set: an ad there may have another form or goal. `live_ids` is what Meta itself reads as live, which outranks
    the mirror."""
    gid, user_id = group.get("id"), group.get("user_id")
    act = group.get("meta_ad_account_id")
    if not act:
        return None, NO_DESTINATION
    rows = {d["ad_id"]: d async for d in db[AD_STATE].find({"client_group_id": gid})}
    live = {a: r for a, r in rows.items()
            if str(r.get("effective_status") or "").upper() == "ACTIVE" and in_scope(r.get("campaign_id"), group)}
    start, end = _window_days(window)
    spend = totals(scoped_days(await load_ad_days(db, user_id, gid, start, end), group), start, end)
    _, main = _live_by_adset(live, spend)
    if main is None:
        return None, NO_DESTINATION
    ranked = sorted((a for a, r in live.items() if str(r.get("adset_id")) == main),
                    key=lambda a: (-spend.get(a, (0.0, 0))[0], a))
    odd_link = False
    for ad_id in ranked:
        if live_ids is not None and str(ad_id) not in live_ids:
            continue
        creative = rows[ad_id].get("creative") or {}
        landing = _landing(creative)
        if creative.get("page_id") and landing:
            adset = await db[ADSET_STATE].find_one({"client_group_id": gid, "adset_id": main}) or {}
            return {"ad_account_id": act, "adset_id": main, "adset_name": adset.get("name"),
                    "page_id": creative["page_id"], "lead_form_id": creative.get("lead_form_id"),
                    "campaign_id": rows[ad_id].get("campaign_id"), **landing}, None
        odd_link = odd_link or bool(creative.get("link") and not landing)
    return None, UNSUPPORTED_LANDING if odd_link else NO_DESTINATION


def _row_destination(row: dict, line: dict) -> dict:
    """Where an ad that exists, or may, really is. Today's main ad set can be a different one."""
    return {**(line.get("destination") or {}),
            **{k: row[k] for k in ("ad_account_id", "adset_id", "adset_name", "page_id", "lead_form_id", "campaign_id") if row.get(k)}}


def _ad_may_exist(row: dict | None) -> bool:
    meta = (row or {}).get("meta") or {}
    return bool(meta.get("ad_id") or meta.get("ad_create_started_at"))


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

def _is_video(line: dict) -> bool:
    return bool((line.get("creative") or {}).get("video_id"))


async def _source_act(db, user_id: str, line: dict) -> str | None:
    group = await db["client_groups"].find_one({"id": line.get("source_client_group_id"), "user_id": user_id},
                                               {"meta_ad_account_id": 1})
    return (group or {}).get("meta_ad_account_id")


async def _file_row(db, user_id: str, line: dict) -> dict | None:
    """The identity a kept file is held under: this source account's own video, else image, else the source ad
    itself when Meta gave no media id. None when the source can't be told apart from another."""
    act = await _source_act(db, user_id, line)
    creative = line.get("creative") or {}
    video, image, ad = creative.get("video_id"), creative.get("image_hash"), line.get("source_ad_id")
    media = video or (f"image:{image}" if image else f"ad:{ad}" if ad else None)
    if not act or not media:
        return None
    return {"_id": f"{user_id}:{_norm_act(act)}:{media}", "user_id": user_id, "source_ad_account_id": act,
            "source_video_id": video, "source_image_hash": image, "source_ad_id": ad}


async def _held_file(db, user_id: str, line: dict) -> str | None:
    ref = await _file_row(db, user_id, line)
    row = await db[FILES].find_one({"_id": ref["_id"]}) if ref else None
    return (row or {}).get("blob_url")


async def plan_media_doc(db, user_id: str, line: dict, token: str) -> dict | None:
    """What goes in the test's `creative.media`, or None when a person must upload a file. A hash or
    video id only goes in when the source shares the destination's account, so `posting.media_plan`
    can never reuse one in the wrong account. Raises MetaWriteError when Meta didn't answer or a login
    failed: only Meta's own refusal means a person has to upload something."""
    creative = line.get("creative") or {}
    src_act = await _source_act(db, user_id, line)
    dest_act = (line.get("destination") or {}).get("ad_account_id")
    same = bool(src_act) and _norm_act(src_act) == _norm_act(dest_act)
    video = _is_video(line)
    if same and (video or creative.get("image_hash")):
        return {"video_id": creative["video_id"]} if video else {"image_hash": creative["image_hash"]}
    held = await _held_file(db, user_id, line)
    if held:
        return {"blob_url": held}
    try:
        if video:
            return {"source_video_id": creative["video_id"]} if await video_source(creative["video_id"], token) else None
        if src_act and creative.get("image_hash"):
            url = await image_url_for_hash(src_act, token, creative["image_hash"])
            return {"source_image_hash": creative["image_hash"]} if url else None
        return None
    except MetaWriteError as e:
        if e.retryable or e.code is None or e.code == 190:
            raise
        logger.info("stock add: Meta won't give the media for %s: %s", line.get("creative_key"), e)
        return None


async def plan_media(db, user_id: str, line: dict, token: str) -> str:
    """"ready" when the post has its media, "needs_file" when it takes a person's upload."""
    return "ready" if await plan_media_doc(db, user_id, line, token) is not None else "needs_file"


def _no_words(line: dict, dest: dict) -> dict | None:
    """Never a blank ad. Primary text is required everywhere, as in the Creatives wizard. A headline is required
    on an image ad that has no web page behind it: Meta only fills a missing one from the link's title, and a
    form's or WhatsApp's link has none worth showing. Meta needs no title on a video, nor does a video need one
    to make sense."""
    source = line.get("creative") or {}
    if not str(source.get("body") or "").strip():
        return NO_BODY
    if not _is_video(line) and dest.get("destination") != "website" and not str(source.get("title") or "").strip():
        return NO_HEADLINE
    return None


def _creative_doc(line: dict, dest: dict, media: dict) -> dict:
    """The source's words and media, sending people where the destination's own ads do."""
    source = line.get("creative") or {}
    return {"format": "video" if _is_video(line) else "image", "media": media, "primary_text": source.get("body") or "",
            "headline": source.get("title") or "", "description": "", "cta_type": source.get("cta_type") or "LEARN_MORE",
            "destination": dest.get("destination"), "website_url": dest.get("website_url")}


# ---------------------------------------------------------------------------
# One post per creative per account
# ---------------------------------------------------------------------------

def _post_id(user_id: str, act: str, key: str) -> str:
    return f"{user_id}:{_norm_act(act)}:{key}"


async def _holds(db, post: dict) -> bool:
    """Is this row's line still entitled to the creative? It is while it can reach Meta, and while its ad exists."""
    card = await db[CARDS].find_one({"_id": post.get("card_id")})
    line = _find_line(card, post.get("line_id")) if card else None
    if line is not None and line.get("state") in HOLDING:
        return True
    if not post.get("test_id"):
        return False
    row = await db[TEST_ADS].find_one({"test_id": post["test_id"], "user_id": post.get("user_id")})
    return _ad_may_exist(row)


async def _already_posted(db, post: dict | None) -> dict:
    """Why another line has this post. It says so if that's an ad sitting paused with nobody coming for it."""
    if not post or not post.get("test_id"):
        return ALREADY_POSTED
    row = await db[TEST_ADS].find_one({"test_id": post["test_id"], "user_id": post.get("user_id")}) or {}
    card = await db[CARDS].find_one({"_id": post.get("card_id")})
    owner = _find_line(card, post.get("line_id")) if card else None
    if row.get("post_state") not in ("created", "failed") or (owner or {}).get("state") in HOLDING:
        return ALREADY_POSTED
    if (row.get("meta") or {}).get("ad_id"):
        return PAUSED_ELSEWHERE
    return MAYBE_PAUSED_ELSEWHERE if _ad_may_exist(row) else ALREADY_POSTED


def _is_mine(post: dict, card: dict, line: dict) -> bool:
    return post.get("card_id") == card["_id"] and post.get("line_id") == line["id"]


async def _reserve(db, post_id: str, user_id: str, act: str, key: str, card: dict, line: dict) -> dict | None:
    """The row, this line's. None when another line holds it. The filters make a lost race an honest None."""
    mine = {"card_id": card["_id"], "line_id": line["id"]}
    for _ in range(2):
        post = await db[POSTS].find_one({"_id": post_id})
        if post is None:
            doc = {"_id": post_id, "user_id": user_id, "ad_account_id": act, "creative_key": key, "test_id": None,
                   "created_at": datetime.utcnow(), **mine}
            try:
                await db[POSTS].insert_one(doc)
                return doc
            except DuplicateKeyError:
                continue
        if _is_mine(post, card, line):
            return post
        if await _holds(db, post):
            return None
        taken = await db[POSTS].update_one(
            {"_id": post_id, "card_id": post.get("card_id"), "line_id": post.get("line_id")},
            {"$set": {**mine, "test_id": None}})
        return {**post, **mine, "test_id": None} if taken.modified_count == 1 else None
    return None


async def _already_run(db, gid: str, key: str) -> bool:
    """`already_run` again at write time: the card may be days old, and someone may have posted it by hand."""
    async for row in db[AD_STATE].find({"client_group_id": gid}):
        if creative_key(row) == key:
            return True
    return False


# ---------------------------------------------------------------------------
# Handing it to the pipeline
# ---------------------------------------------------------------------------

async def _linked(db, card: dict, line: dict, test_id: str, row_id: str) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), test_id=test_id, test_ad_id=row_id)


def _retry_patch(row: dict, old_creative: dict, dest: dict) -> dict:
    """What moved since the failed try. The checks just ran against today's destination, so that's where it goes."""
    patch = {k: dest.get(k) for k in ("adset_id", "adset_name", "page_id", "lead_form_id") if dest.get(k) != row.get(k)}
    patch.update({k: dest.get(k) for k in ("destination", "website_url") if dest.get(k) != old_creative.get(k)})
    return patch


async def _post_new(db, user_id: str, gid: str, post: dict, line: dict, dest: dict, media: dict, name: str) -> tuple[str, dict]:
    """The test and its row and job. A test left by a kill is carried on with. Returns (test id, row)."""
    test_id = post.get("test_id")
    creative = _creative_doc(line, dest, media)
    if test_id is None:
        act = await _source_act(db, user_id, line)
        test = await create_test(db, user_id, name=name, origin="revive", creative=creative, targets={},
                                 created_via=MANAGED_BY, managed_by=MANAGED_BY, ad_name=name,
                                 source={"ad_account_id": act, "client_group_id": line.get("source_client_group_id"),
                                         "ad_id": line.get("source_ad_id"), "creative_key": line.get("creative_key")})
        test_id = test["id"]
        await db[POSTS].update_one({"_id": post["_id"]}, {"$set": {"test_id": test_id}})
    else:
        await update_test(db, user_id, test_id, {"creative": creative})
        # enqueue_post moved it to posting and was killed before its row. Nothing exists, so it can start again.
        await db[TESTS].update_one({"id": test_id, "user_id": user_id, "status": POSTING},
                                   {"$set": {"status": DRAFT}, "$pull": {"posted_accounts": dest["ad_account_id"]},
                                    "$unset": {"posted_at": ""}})
    account = {"ad_account_id": dest["ad_account_id"], "client_group_id": gid,
               **{k: dest.get(k) for k in ("adset_id", "adset_name", "page_id", "lead_form_id", "campaign_id")}}
    res = await enqueue_post(db, user_id, test_id, accounts=[account])
    return test_id, res["ads"][0]


async def _has_new_file(db, user_id: str, line: dict, row: dict) -> bool:
    held = await _held_file(db, user_id, line)
    test = await get_test(db, user_id, row["test_id"]) or {}
    return bool(held) and ((test.get("creative") or {}).get("media") or {}).get("blob_url") != held


async def enqueue_add(db, mongo_client, user_id: str, card: dict, line: dict, *, before_post=None) -> dict:
    """Give this line's post to the pipeline, or carry on with the one it has. {"outcome": ...}:
      posting      with test_id, test_ad_id. Nothing has reached Meta yet.
      needs_file   nothing was posted. It needs a person's upload.
      blocked      nothing was posted. `reason` says why.
      retry        nothing was posted, and Meta didn't answer. Worth asking again in a minute.
      stopped      `before_post` refused, so nothing was posted.
    `before_post(fields)` runs after every check, immediately before the first write that could lead to Meta,
    to store the marker and `fields` on the line. It is False if the line can no longer go ahead."""
    blocked = lambda reason: {"outcome": "blocked", "reason": reason}  # noqa: E731
    if not add_writes_enabled():
        return blocked(WRITES_OFF)
    key, gid = line.get("creative_key"), card.get("client_group_id")
    group = await db["client_groups"].find_one({"id": gid, "user_id": user_id})
    if not key or group is None:
        return blocked(NO_SOURCE)
    dest = line.get("destination") or await destination_for(db, group, window=card.get("window"))
    if dest is None:
        return blocked(NO_DESTINATION)
    line = {**line, "destination": dest}
    name = line.get("new_ad_name") or clean_ad_name(line.get("ad_name"), datetime.utcnow().date())
    post_id = _post_id(user_id, dest["ad_account_id"], key)
    post = await _reserve(db, post_id, user_id, dest["ad_account_id"], key, card, line)
    if post is None:
        return blocked(await _already_posted(db, await db[POSTS].find_one({"_id": post_id})))

    test_id = post.get("test_id")
    row = await db[TEST_ADS].find_one({"test_id": test_id, "user_id": user_id}) if test_id else None
    state = (row or {}).get("post_state")
    if state == "removed":
        return blocked({"code": "removed", "message": GONE_FROM_META})
    if row is None and await _already_run(db, gid, key):
        return blocked(ALREADY_RUN)

    stored = (row or {}).get("meta") or {}
    if state in (None, "failed") and not _ad_may_exist(row):
        if (no_words := _no_words(line, dest)) is not None:
            return blocked(no_words)
        if row is not None and await _has_new_file(db, user_id, line, row):
            # The stored media is what Meta turned down, and a Retry would go on with it for ever.
            await db[TEST_ADS].update_one({"id": row["id"], "user_id": user_id}, {"$unset": {
                f"meta.{k}": "" for k in ("image_hash", "video_id", "video_ready", "thumbnail_url", "creative_id")}})
            stored = {}
    media = None
    if state in (None, "failed") and not (stored.get("image_hash") or stored.get("video_id")):
        token = ((await get_facebook_token(user_id, mongo_client)) or {}).get("access_token")
        if not token:
            return blocked(NOT_CONNECTED)
        try:
            media = await plan_media_doc(db, user_id, line, token)
        except MetaWriteError as e:
            logger.warning("stock add: can't read media for %s: %s", key, e)
            return blocked(NOT_CONNECTED) if e.code == 190 else {"outcome": "retry"}
        if media is None:
            return {"outcome": "needs_file"}

    if before_post is not None and not await before_post(
            {"destination": dest, "new_ad_name": name, "ad_name": name, "go_live_tries": 0, "fix": None}):
        return {"outcome": "stopped"}

    try:
        if row is None:
            test_id, row = await _post_new(db, user_id, gid, post, line, dest, media, name)
        elif state == "failed":
            test = await get_test(db, user_id, test_id)
            old = test.get("creative") or {}
            await update_test(db, user_id, test_id, {"creative": _creative_doc(line, dest, media or old.get("media") or {})})
            await posting.retry(db, user_id, test_id, row["id"], patch=_retry_patch(row, old, dest))
    except PostingError as e:
        logger.warning("stock add: the pipeline refused %s: %s", key, e.message)
        return blocked({"code": "post_refused", "message": e.message})
    await _linked(db, card, line, test_id, row["id"])
    return {"outcome": "posting", "test_id": test_id, "test_ad_id": row["id"]}


# ---------------------------------------------------------------------------
# From apply.py
# ---------------------------------------------------------------------------

async def _live_read_block(db, mongo_client, agent: dict, card: dict, line: dict, dest: dict, now: datetime,
                           read: dict | None = None) -> dict | None:
    """Every check before an Add, on what Meta says now. The ad set's list decides whether it's still live and
    whether there's room. Adds still on their way count, and so does what's in review, so two can't overshoot:
    the client's lease covers this check and the marker that makes this line count for the next. `read`, if
    given, receives the ad set's list."""
    doc = _target_doc(card, line, ACTION_ENABLE_ADS)
    guard = {**agent, "guardrails": {**(agent.get("guardrails") or {}), "protect_last_live_ad": False}}
    g = await check_guardrails(db, guard, doc, now_utc=now)
    if not g.ok:
        return {"code": g.reason, "message": g.message}
    adset_id = str(dest["adset_id"])
    # No ad of the ad set is changing, so only the new ad's own name is judged.
    target = {"object_id": adset_id, "object_type": "adset", "name": line.get("ad_name"), "only_ad_id": "new"}
    read = {} if read is None else read
    memories = await list_memory(db, agent["_id"], active_only=True)
    g = await check_adset_contents_live(db, mongo_client, agent, {**doc, "action": {"type": ACTION_ENABLE_ADS, "targets": [target]}},
                                        memories=memories, now_utc=now, read_ads=read)
    if not g.ok:
        return {"code": g.reason, "message": g.message}
    ads = read.get(adset_id) or []
    if not any(str(a.get("effective_status") or "").upper() == "ACTIVE" for a in ads):
        return NO_DESTINATION
    taken = await running_ads(db, card.get("client_group_id"), adset_id, ads, now) | await adds_in_flight(db, card.get("client_group_id"), adset_id, (card["_id"], line["id"]), now)
    limit = _settings(agent)["creative"]["max_ads_live"]
    if len(taken) < limit or await _own_ad(db, card, line) in taken:
        return None
    return {"code": "max_ads_live",
            "message": f"Adding this would make {len(taken) + 1} ads live in its ad set, over the limit of {limit}."}


async def _own_ad(db, card: dict, line: dict) -> str | None:
    """The ad a Retry already made, if it has one: it's in the count, and it isn't a new one."""
    if not line.get("test_id"):
        return None
    row = await db[TEST_ADS].find_one({"test_id": line["test_id"], "user_id": card.get("user_id")}) or {}
    return (row.get("meta") or {}).get("ad_id")


async def _held_elsewhere(db, user_id: str, card: dict, line: dict, dest: dict) -> dict | None:
    """Why another line has this post, or None."""
    if not line.get("creative_key"):
        return None
    post = await db[POSTS].find_one({"_id": _post_id(user_id, dest["ad_account_id"], line["creative_key"])})
    if not post or _is_mine(post, card, line) or not await _holds(db, post):
        return None
    return await _already_posted(db, post)


async def _own_row(db, user_id: str, act: str | None, card: dict, line: dict) -> dict | None:
    """The pipeline's row for this line's post, if it has got that far."""
    if not act or not line.get("creative_key"):
        return None
    post = await db[POSTS].find_one({"_id": _post_id(user_id, act, line["creative_key"])})
    if not post or not _is_mine(post, card, line) or not post.get("test_id"):
        return None
    return await db[TEST_ADS].find_one({"test_id": post["test_id"], "user_id": user_id})


async def _to_needs_file(db, card: dict, line: dict, error: str | None = None) -> None:
    """A person has to act. On Auto that makes it Ask me, for good (spec section 7)."""
    error = error or (NEEDS_FILE if _is_video(line) else NEEDS_IMAGE_FILE)
    auto = line.get("mode") == "auto" or (line.get("decision") or {}).get("actor") == ACTOR_BIRDY
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_NEEDS_FILE, needs_file=True, error=error,
                    reason_code="needs_file", write_started_at=None,
                    **({"mode": "ask", "auto_fallback": "needs_file"} if auto else {}))


async def _after_enqueue(db, card: dict, line: dict, res: dict) -> None:
    outcome = res["outcome"]
    if outcome == "blocked":
        await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_BLOCKED,
                        error=res["reason"]["message"], reason_code=res["reason"]["code"], write_started_at=None)
    elif outcome == "needs_file":
        await _to_needs_file(db, card, line)
    elif outcome == "retry":
        tries = (line.get("media_read_tries") or 0) + 1
        if tries >= MEDIA_READ_TRIES:
            await _fail(db, card, line, READ_FAILED, write_started_at=None, media_read_tries=0)
        else:  # Nothing exists, so it goes round again with every check
            await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_QUEUED, write_started_at=None,
                            media_read_tries=tries)


async def apply_add(db, mongo_client, card: dict, line: dict, agent: dict, now: datetime, sent: list) -> None:
    """A claimed Add line that passed the refusals and the mode. apply._run_line holds the client's lease."""
    user_id, gid = card["user_id"], card.get("client_group_id")
    group = await db["client_groups"].find_one({"id": gid, "user_id": user_id})
    if group is None:
        return await _block(db, card, line, NO_DESTINATION)
    own = await _own_row(db, user_id, group.get("meta_ad_account_id"), card, line)
    fixed = _ad_may_exist(own)  # that ad is where it is: today's main ad set may be another
    if fixed:
        dest, why = _row_destination(own, line), None
    else:
        dest, why = await _pick_destination(db, group, window=card.get("window"))
    if dest is None:
        return await _block(db, card, line, why)
    line = {**line, "destination": dest,
            "new_ad_name": line.get("new_ad_name") or clean_ad_name(line.get("ad_name"), local_today(now, agent_tz(agent)))}
    held = await _held_elsewhere(db, user_id, card, line, dest)
    if held is not None:
        return await _block(db, card, line, held)
    read: dict = {}
    refusal = await _live_read_block(db, mongo_client, agent, card, line, dest, now, read)
    if refusal is not None:
        return await _block(db, card, line, refusal)
    if not fixed:  # now that Meta has said what's live, take the Page and form from one of those
        live = {str(a.get("id")) for a in read.get(str(dest["adset_id"])) or [] if str(a.get("effective_status") or "").upper() == "ACTIVE"}
        dest, why = await _pick_destination(db, group, window=card.get("window"), live_ids=live)
        if dest is None:
            return await _block(db, card, line, why)
        line = {**line, "destination": dest}

    async def mark(fields: dict) -> bool:
        started = await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), statuses=_ACTIONABLE,
                                  write_started_at=now, **fields)
        if started is not None:
            sent.append(True)
        return started is not None

    res = await enqueue_add(db, mongo_client, user_id, card, line, before_post=mark)
    if res["outcome"] == "stopped":
        return await _lost_the_last_check(db, agent, card, line)
    return await _after_enqueue(db, card, line, res)


# ---------------------------------------------------------------------------
# A person's file
# ---------------------------------------------------------------------------

async def attach_file(db, user_id: str, card_id: str, line_id: str, blob_url: str) -> dict:
    """Keep this upload for the line's source video or image, so it's asked for at most once. A line waiting on it goes back
    to Proposed: on Auto it fell back to Ask me, and a person's Apply is what posts it. Only a URL on this app's
    own Blob store, in this user's own folder, is accepted, so this can't be used to make Meta fetch anything else."""
    card = await _visible_card(db, user_id, card_id)
    line = _find_line(card, line_id) if card else None
    if line is None:
        return _answer("not_found", None, ok=False)
    if (card.get("preview") or card.get("status") not in _ACTIONABLE or line.get("kind") != "add"
            or line.get("state") not in _FILE_LINE_STATES or not line.get("creative_key")):
        return _answer("refused", card, ok=False, reason=CANT_TAKE_FILE)
    if not is_own_blob_url(blob_url, user_id):
        return _answer("bad_file", card, ok=False, reason=NOT_THE_USERS)
    ref = await _file_row(db, user_id, line)
    if ref is None:
        return _answer("refused", card, ok=False, reason=CANT_TAKE_FILE)
    now = datetime.utcnow()
    file_id = ref.pop("_id")
    await db[FILES].update_one({"_id": file_id}, {"$set": {**ref, "blob_url": blob_url, "updated_at": now},
                                                  "$setOnInsert": {"created_at": now}}, upsert=True)
    await _set_line(db, card_id, line_id, expect=(STATE_NEEDS_FILE,), statuses=_ACTIONABLE, state=STATE_PROPOSED,
                    needs_file=False, error=None, reason_code=None)
    return _answer("attached", await db[CARDS].find_one({"_id": card_id}))


# ---------------------------------------------------------------------------
# Following the pipeline
# ---------------------------------------------------------------------------

async def _mirror_new_ad(db, card: dict, line: dict, row: dict, now: datetime) -> None:
    """The mirror only learns of a new ad at the next refresh. Undo, the last live ad check and already_run
    need it before that. A refresh that got here first is left alone."""
    meta = row.get("meta") or {}
    dest = line.get("destination") or {}
    await db[AD_STATE].update_one({"client_group_id": card.get("client_group_id"), "ad_id": meta.get("ad_id")}, {"$setOnInsert": {
        "user_id": card.get("user_id"), "ad_account_id": row.get("ad_account_id"), "adset_id": row.get("adset_id"),
        "campaign_id": row.get("campaign_id"), "name": line.get("ad_name") or "", "effective_status": "ACTIVE",
        "status": "ACTIVE", "created_time": now, "issues": [], "review_feedback": {}, "paused_at": None, "updated_at": now,
        "creative": {"image_hash": meta.get("image_hash"), "video_id": meta.get("video_id"), "page_id": dest.get("page_id"),
                     "lead_form_id": dest.get("lead_form_id"), "link": dest.get("website_url")}}}, upsert=True)


async def _finish_add(db, card: dict, line: dict, row: dict, now: datetime) -> None:
    """It's live. Each write here is safe to repeat, and the line moves last, so a kill just comes back here."""
    ad_id = row["meta"]["ad_id"]
    applied_at = row.get("live_at") or now
    decision = line.get("decision") or {}
    label = "Auto-run by Birdy" if decision.get("actor") == ACTOR_BIRDY else f"Approved by {_who(decision)}"
    await _mirror_new_ad(db, card, line, row, now)
    await record_add(db, card, line, ad_id, applied_at)
    await _log_once(db, card, line, KIND_ACTION_APPLIED, label=label, title=f"Added {line.get('ad_name') or 'an ad'}",
                    decision=decision)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_APPLIED, meta_ids=[ad_id],
                    test_ad_id=row["id"], applied_at=applied_at, undo_until=applied_at + UNDO_AUTO_WINDOW, wrote=True, note=None,
                    error=None, reason_code=None, cut_at=None, needs_file=False, paused_ad_id=None)


def _needs_upload(err: dict, test: dict | None) -> bool:
    """Meta wouldn't take the source's video: a file is the way on. A limit or a lost login isn't that."""
    media = ((test or {}).get("creative") or {}).get("media") or {}
    if err.get("message") in (posting.VIDEO_NEEDS_UPLOAD, posting.IMAGE_NEEDS_UPLOAD, VIDEO_PROCESS_FAILED):
        return True
    from_source = media.get("source_video_id") or media.get("source_image_hash")
    # No code means Meta never answered, and that says nothing about the file.
    return (err.get("step") == "media" and bool(from_source) and not media.get("blob_url") and err.get("code") is not None
            and err.get("fix") not in ("retry_later", "reconnect_meta"))


async def _sync_failed(db, card: dict, line: dict, row: dict) -> None:
    err = row.get("post_error") or {}
    test = await get_test(db, card["user_id"], row["test_id"])
    if _needs_upload(err, test):
        return await _to_needs_file(db, card, line, VIDEO_REJECTED if err.get("message") == VIDEO_PROCESS_FAILED else None)
    return await _fail(db, card, line, err.get("message") or POST_FAILED, write_started_at=None, fix=err.get("fix"))


async def _go_live_refusal(db, mongo_client, card: dict, line: dict, row: dict, now: datetime) -> dict | None:
    """What may have changed since the enqueue, which can be an hour ago. The same checks a line runs before any
    write, against the ad's own ad set."""
    agent = await _load_agent(db, card)
    refusal = await _refusal(db, agent, card)
    if refusal is not None:
        return refusal
    mode = resolve_mode(_settings(agent), card.get("client_group_id"), "add")
    if mode == "flag":
        return NOW_FLAGGED
    if mode != "auto" and (line.get("decision") or {}).get("actor") == ACTOR_BIRDY:
        return NOT_AUTO_NOW
    return await _live_read_block(db, mongo_client, agent, card, {**line, "test_id": row["test_id"]},
                                  _row_destination(row, line), now)


async def _leave_paused(db, card: dict, line: dict, row: dict, reason: dict) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_FAILED, reason_code=reason["code"],
                    error=_short(LEFT_PAUSED.format(why=reason["message"].rstrip(".") + ".")), write_started_at=None,
                    paused_ad_id=row["meta"]["ad_id"], **({"mode": "ask"} if reason is NOT_AUTO_NOW else {}))


async def _sync_created(db, mongo_client, card: dict, line: dict, row: dict, now: datetime, deadline: float | None) -> None:
    """The ad exists, paused, and this is the only thing that turns it on. Under the client's lease, like any
    write, so what the check counted can't change before the ad goes on."""
    ad_id = row["meta"]["ad_id"]
    if not add_writes_enabled():
        return await _fail(db, card, line, STOPPED_WITH_AD, write_started_at=None, paused_ad_id=ad_id)
    if mongo_client is None or _no_time_for_a_line(deadline):
        return None
    key = _lease_key(card.get("agent_id"), card.get("client_group_id"))
    token = await _take_lease(db, key)
    if token is None:
        return None
    try:
        card = await db[CARDS].find_one({"_id": card["_id"]}) or card
        line = _find_line(card, line["id"]) or line
        row = await db[TEST_ADS].find_one({"id": row["id"], "user_id": card["user_id"]}) or row
        if line.get("state") != STATE_APPLYING or row.get("post_state") != "created":
            return None  # an overlapping tick got here first
        # Counted before the call: one that hangs is cut off by the tick's budget and has to count too.
        tries = (line.get("go_live_tries") or 0) + 1
        await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), go_live_tries=tries)
        refusal = await _go_live_refusal(db, mongo_client, card, line, row, now)
        if refusal is not None:
            if refusal["code"] == "live_check_failed" and tries < GO_LIVE_TRIES:
                return None  # couldn't read: it stays off, and the next minute asks again
            return await _leave_paused(db, card, line, row, refusal)
        try:
            await posting.go_live(db, mongo_client, card["user_id"], row["test_id"], now=now)
        except Exception as e:  # noqa: BLE001 - counts as a try, and the next minute asks again
            logger.warning("stock card %s line %s: go-live failed: %s", card["_id"], line["id"], e)
        fresh = await db[TEST_ADS].find_one({"id": row["id"], "user_id": card["user_id"]}) or row
        if fresh.get("post_state") == "active":
            return await _finish_add(db, card, line, fresh, now)
        if tries < GO_LIVE_TRIES:
            return None
        why = (fresh.get("post_error") or {}).get("message") or "Meta didn't answer."
        return await _fail(db, card, line, NOT_LIVE.format(why=why.rstrip(".") + "."), write_started_at=None,
                           fix=(fresh.get("post_error") or {}).get("fix"), paused_ad_id=ad_id)
    finally:
        await _release_lease(db, key, token)


def _gone(line: dict, now: datetime) -> bool:
    """Has the invocation that was enqueueing this line finished, one way or another?"""
    since = line.get("applying_at") or line.get("write_started_at")
    return bool(line.get("cut_at")) or (since is not None and now - since >= STUCK_AFTER)


async def _repair_job(db, card: dict, row: dict, now: datetime) -> None:
    """Under the lease, and looking again inside it: two jobs for one row could both reach `create_ad`."""
    uid = card["user_id"]
    key = _lease_key(card.get("agent_id"), card.get("client_group_id"))
    token = await _take_lease(db, key)
    if token is None:
        return
    try:
        if await db[POST_JOBS].find_one({"test_ad_id": row["id"], "user_id": uid}) is None:
            test = await get_test(db, uid, row["test_id"]) or {}
            await db[POST_JOBS].insert_one({**_new_job(uid, row["test_id"], row["id"], now), **_managed_job(test)})
    finally:
        await _release_lease(db, key, token)


async def _sync_in_pipeline(db, card: dict, line: dict, row: dict, now: datetime) -> None:
    """Queued or going. All there is to check is that it really has a job to go on."""
    if not _gone(line, now):
        return
    uid = card["user_id"]
    job = await db[POST_JOBS].find_one({"test_ad_id": row["id"], "user_id": uid})
    if job is None:
        await _repair_job(db, card, row, now)
    elif job.get("state") in (DONE, FAILED):
        err = job.get("last_error") or {"message": POST_FAILED, "step": None, "fix": None}
        await db[TEST_ADS].update_one({"id": row["id"], "user_id": uid},
                                      {"$set": {"post_state": "failed", "post_error": err, "updated_at": now}})


async def _resume(db, mongo_client, card: dict, line: dict, now: datetime, deadline: float | None) -> None:
    """Killed after the marker and before the test had a row, so nothing can have reached Meta. Finish the
    enqueue, once that invocation must be gone, under the same lease as any write."""
    if mongo_client is None or not _gone(line, now) or _no_time_for_a_line(deadline):
        return
    key = _lease_key(card.get("agent_id"), card.get("client_group_id"))
    token = await _take_lease(db, key)
    if token is None:
        return
    try:
        refusal = await _refusal(db, await _load_agent(db, card), card)
        res = ({"outcome": "blocked", "reason": refusal} if refusal is not None
               else await enqueue_add(db, mongo_client, card["user_id"], card, line))
        await _after_enqueue(db, card, line, res)
    finally:
        await _release_lease(db, key, token)


async def _sync_line(db, mongo_client, card: dict, line: dict, now: datetime, deadline: float | None) -> bool:
    # Through the post's row: a kill can leave the line not knowing its test yet.
    act = (line.get("destination") or {}).get("ad_account_id")
    post = await db[POSTS].find_one({"_id": _post_id(card["user_id"], act, line.get("creative_key"))})
    test_id = post.get("test_id") if post and _is_mine(post, card, line) else line.get("test_id")
    row = await db[TEST_ADS].find_one({"test_id": test_id, "user_id": card["user_id"]}) if test_id else None
    if row is None:
        await _resume(db, mongo_client, card, line, now, deadline)
        return False
    state = row.get("post_state")
    if state == "active":
        await _finish_add(db, card, line, row, now)
    elif state == "failed":
        await _sync_failed(db, card, line, row)
    elif state == "removed":
        await _fail(db, card, line, GONE_FROM_META, write_started_at=None)
    elif state == "created":
        await _sync_created(db, mongo_client, card, line, row, now, deadline)
    else:
        await _sync_in_pipeline(db, card, line, row, now)
        return False
    return True


async def _say_what_was_left(db, deadline: float | None) -> None:
    """A line that can't be retried any more, since a newer card arrived or the card closed, with an ad sitting
    paused in Meta. Nothing can happen to that ad now but a person, so the line and the activity feed say so."""
    query = {"preview": {"$ne": True}, "lines": {"$elemMatch": {"kind": "add", "state": {"$in": [STATE_FAILED, STATE_BLOCKED]},
                                                                "test_ad_id": {"$ne": None}, "left_told": {"$ne": True}}}}
    cards = await db[CARDS].find(query).sort("updated_at", 1).limit(SYNC_BATCH).to_list(length=SYNC_BATCH)
    for card in cards:
        if _out_of_time(deadline):
            return
        if card.get("status") in _ACTIONABLE and not await has_newer_card(db, card):
            continue
        for line in card.get("lines") or []:
            if (line.get("kind") != "add" or line.get("state") not in (STATE_FAILED, STATE_BLOCKED)
                    or not line.get("test_ad_id") or line.get("left_told")):
                continue
            row = await db[TEST_ADS].find_one({"id": line["test_ad_id"], "user_id": card.get("user_id")})
            if not _ad_may_exist(row):
                await _set_line(db, card["_id"], line["id"], expect=(line["state"],), left_told=True)
                continue
            text = CLOSED_PAUSED if row["meta"].get("ad_id") else CLOSED_MAYBE
            await _log_once(db, card, line, KIND_AD_LEFT_PAUSED, label="Needs a person", decision=line.get("decision") or {},
                            title=f"Left paused in Meta: {line.get('ad_name') or 'an ad'}", note=text)
            await _set_line(db, card["_id"], line["id"], expect=(line["state"],), error=text, reason_code="left_paused", left_told=True)


async def sync_add_lines(db, now_utc: datetime, mongo_client=None, *, deadline: float | None = None) -> int:
    """Every minute, from `process_queued_lines`: move each Add handed to the pipeline on as far as the pipeline
    has got. The only thing that moves a line once it has its marker. This reaches Meta only to turn on an ad
    the pipeline couldn't, and to finish an enqueue that was killed, which is what `mongo_client` is for.
    Returns how many lines it moved. A card that closed meanwhile still records what really happened, and an ad
    not yet on stays off."""
    now = _naive_utc(now_utc)
    query = {"lines": {"$elemMatch": {"kind": "add", "state": STATE_APPLYING, "write_started_at": {"$ne": None}}}}
    cards = await db[CARDS].find(query).sort("updated_at", 1).limit(SYNC_BATCH).to_list(length=SYNC_BATCH)
    moved = 0
    for card in cards:
        for line in card.get("lines") or []:
            if line.get("kind") != "add" or line.get("state") != STATE_APPLYING or not line.get("write_started_at"):
                continue
            if _out_of_time(deadline):
                return moved
            try:
                if await _sync_line(db, mongo_client, card, line, now, deadline):
                    moved += 1
            except Exception as e:  # noqa: BLE001 - it stays as it is and is read again next minute
                logger.error("stock card %s line %s: sync failed: %s", card["_id"], line["id"], e, exc_info=True)
            card = await db[CARDS].find_one({"_id": card["_id"]}) or card
    await _say_what_was_left(db, deadline)
    return moved
