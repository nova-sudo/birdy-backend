"""
ai/agents/stock/apply.py
------------------------
Making a card's lines happen in Meta, and undoing them (spec section 7). This changes real client ad
accounts, so every step is stored before the next one starts.

A line moves:   proposed -> queued -> applying -> applied | failed | blocked
and back:       applied -> undoing -> undone | applied

Every caller (a person, Apply all, Auto) only moves lines to `queued`. That is the atomic claim: a
card's version must match, so of many presses one wins, and the decision survives a kill. `_run_line`
then takes each queued line, in the request while there's budget and otherwise from the per-minute
cron. A queued line is checked again when it runs, so it never acts on a card closed meanwhile.

Inside `applying`, `write_started_at` is stored immediately before anything is sent, with one last
check of the card's status and version. So a killed invocation leaves one of two things:

  no write_started_at   nothing was sent. Queued again, up to MAX_RECOVERIES, and every check reruns.
  write_started_at      it may have landed. Never sent again: the ad's status is read from Meta.
                        Landed: `applied`, counted as Birdy's write, logged once.
                        Not landed: `failed`, saying so; a person can Retry. The marker stays, so
                        if it lands late, the Retry finds it done and it still counts as Birdy's.
                        Unreadable: stays `applying` and is read again each minute. After
                        CONFIRM_GIVE_UP it is `failed`, saying Birdy couldn't confirm it. A Retry reads
                        first, so that can't send twice either.

Recovery only touches lines older than STUCK_AFTER, longer than an invocation can live. A send that
timed out stays `applying` for recovery, since it can land after any read made now. A reply where
Meta said no is read back once and is `failed`.
`undoing` works the same way, using `undo_write_started_at`.

Refusals: statuses are an allow-list (`open`, plus `partly_applied`), `closing` and `superseded` are
never applied, and a card with a newer card for its client is refused. If a card closes after the
send has gone, the line still records what really happened, and its Undo still works.

Before each write, one short lease per agent and client covers "check the guardrails, then send", so
two invocations can't each see a spare ad and pause it. The lease expires by itself after a crash. A
line that can't get it stays `queued` for the cron. Recovery only reads, so it doesn't take it.

A pause sent but not yet confirmed counts as done for the guardrails: toward the day's pause cap until
its activity row exists, and as off when checking for the last live ad.

In the cron and Apply all, no line starts with under MIN_TIME_FOR_LINE_S of the budget left, so normal
running doesn't cut a send. The hard budget stays as a backstop. A line it cuts mid-send records `cut_at`: that invocation has
stopped, so the next tick reads it instead of waiting for STUCK_AFTER.

The target's ad set is read from Meta before every write: last live ad, protected tags, taught
exceptions and the live ad limit are judged on that list. A mirror row saying "off" only outranks Meta's
"live" for MIRROR_TRUSTED_FOR, the time a read can lag a write. A failed read, or an ad with no known ad
set, refuses the line. The mode is resolved again at write time, for Remove and Bring back: now Flag means
`flagged` and no write, and Auto only writes if it still resolves to Auto.

An Add goes through all of this too, as far as its write, which is different: stock/add.py hands it to the
Creatives posting pipeline, and a later tick moves it on. So for an Add:

  queued                 approved, nothing looked at yet.
  applying, no marker    being checked. Nothing exists anywhere. Recovery queues it again, as for any line.
  applying, marker       handed to the pipeline, or about to be. Only add.sync_add_lines moves it from here,
                         and recovery leaves it alone, so nothing can enqueue a second post.

Undo on an Add pauses the new ad, by the same path as the Undo of a Bring back.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from pymongo.errors import DuplicateKeyError

from ai.agents import claims
from ai.agents.guardrails import (DEFAULT_GUARDRAILS, _naive_utc, agent_tz, check_adset_contents_live, check_guardrails,
                                  local_today)
from ai.agents.meta_state import AD_STATE, record_status_write
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.stock.cards import (STATE_APPLIED, STATE_APPLYING, STATE_BLOCKED, STATE_FAILED, STATE_FLAGGED,
                                   STATE_NEEDS_FILE, STATE_PROPOSED, STATE_QUEUED, STATE_UNDOING, STATE_UNDONE,
                                   STATUS_APPLIED, STATUS_CLOSING, STATUS_DISMISSED, STATUS_FLAGGED, STATUS_OPEN,
                                   STATUS_PARTLY_APPLIED, STATUS_SUPERSEDED, guarded_update)
from ai.agents.stock.scoreboard import mark_add_undone
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_mode
from ai.agents.store import AGENTS, CARDS, RUNS, list_memory
from ai.suggestions.actions import _is_desired, execute_action
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from ai.suggestions.store import (ACTIVITY, ACTOR_BIRDY, ACTOR_USER, KIND_ACTION_APPLIED, KIND_ACTION_UNDONE,
                                  KIND_SUGGESTION_DISMISSED, log_activity)
from services.creatives.store import TEST_ADS
from services.meta_status_service import get_object_state

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=6)     # an invocation lives 300 s at most, so nothing alive is this old
CONFIRM_GIVE_UP = timedelta(hours=2)   # how long a sent write can wait for Meta to be readable
MAX_RECOVERIES = 2                     # kills before the send that a line survives
QUEUE_BATCH = 200                      # cards looked at per cron pass; the rest wait a minute
ERROR_MAX = 300
_RACE_TRIES = 8

MIN_TIME_FOR_LINE_S = 4.0              # a line's 3 or 4 Graph calls usually fit in this
MIRROR_TRUSTED_FOR = timedelta(minutes=5)
LEASES = "agent_write_leases"          # one row per agent and client while it is checking and sending
LEASE_TTL = timedelta(seconds=150)     # longer than the four Graph calls of one line, at 30 s each
UNDO_LEASE_WAIT_S = 2.0                # Undo has a person waiting, so it waits this long, then says retry
_LEASE_POLL_S = 0.2
RECOVERY_READ_TIMEOUT_S = 3            # a hung read counts as unreadable, so one can't eat the tick

WRITABLE_KINDS = ("remove", "revive", "add")
# What an ad set will have delivering without anyone doing anything. A new ad reads as one of the review
# states for hours after it's turned on, and it still takes a place.
WILL_RUN = {"ACTIVE", "PENDING_REVIEW", "IN_PROCESS", "PREAPPROVED"}
_ACTIONABLE = (STATUS_OPEN, STATUS_PARTLY_APPLIED)
_LIVE = (STATUS_OPEN, STATUS_FLAGGED, STATUS_PARTLY_APPLIED, STATUS_APPLIED, STATUS_SUPERSEDED, STATUS_DISMISSED)
_RETRYABLE = (STATE_PROPOSED, STATE_FAILED, STATE_BLOCKED, STATE_NEEDS_FILE)    # only when a person names the line
_IN_PROGRESS = (STATE_QUEUED, STATE_APPLYING, STATE_UNDOING)
_WAITING = (STATE_PROPOSED, STATE_QUEUED, STATE_APPLYING, STATE_FAILED, STATE_BLOCKED, STATE_NEEDS_FILE)
_TOUCHED = (STATE_APPLIED, STATE_UNDOING, STATE_UNDONE)

AGENT_OFF = {"code": "agent_off", "message": "This agent is off, so it can't change this client."}
SUPERSEDED = {"code": "superseded", "message": "A newer card replaced this one, so it can't be applied."}
DISMISSED = {"code": "dismissed", "message": "This card was dismissed, so it can't be applied."}
ON_HOLD = {"code": "on_hold", "message": "This client is on hold, so Birdy won't change its ads."}
PREVIEW_CARD = "A preview card can't be applied."
FLAG_CARD = "This card only flags changes. People make them in Meta."

NOTHING_CHANGED = "Birdy couldn't finish this. Nothing was changed in Meta. Try again."
COULD_NOT_CONFIRM = ("Birdy sent this to Meta but couldn't confirm it. Check the ad in Ads Manager, "
                     "or try again: Birdy looks first, so it won't send it twice.")
GONE = "This ad no longer exists in Meta."
ALREADY_DONE = "Already done in Meta. Nothing changed."
UNDO_NOT_WRITTEN = "Birdy made no change here, so there was nothing to reverse."
ALREADY_APPLIED = "Already applied. Undo it instead."
BUSY = "Birdy is applying this right now. Try again in a moment."
STILL_POSTING = "Birdy is still posting this ad, so there's nothing to undo yet. You can undo it once it's live."
NOW_FLAG = "This change is now set to Flag, so Birdy didn't make it. Make it in Meta if you want it."
STAYS_PAUSED = " The ad Birdy made stays paused in Meta."
OTHER_AGENT = "Another agent now handles this client, so Birdy can't undo this. Change it in Ads Manager if you need to."
NO_ADSET = {"code": "no_adset", "message": "Birdy doesn't know which ad set this ad is in, so nothing was changed."}
LAST_LIVE = {"code": "last_live_ad", "message": "Pausing this would leave its ad set with no live ads."}
NO_AD = {"code": "no_ad", "message": "This line doesn't name an ad, so nothing was changed."}
SEVERAL_ADS = {"code": "several_ads", "message": "This line names more than one ad, so nothing was changed."}


def _action_type(kind: str, *, undo: bool = False) -> str:
    pause = (kind == "remove") != undo
    return ACTION_PAUSE_ADS if pause else ACTION_ENABLE_ADS


def _desired(atype: str) -> str:
    return "PAUSED" if atype == ACTION_PAUSE_ADS else "ACTIVE"


def _landed(atype: str, state: dict | None) -> bool:
    """Did a write reach this ad? An ad's own `status` says so. `effective_status` can't, since
    a paused ad set or a review leaves a turned-on ad not delivering."""
    if atype == ACTION_PAUSE_ADS:
        return _is_desired(atype, state)
    return bool(state) and (state.get("status") or state.get("effective_status")) == "ACTIVE"


def _not_landed(state: dict | None) -> str:
    own = (state or {}).get("status") or (state or {}).get("effective_status")
    return f"This didn't go through. The ad is still {own} in Meta. Try again." if state else GONE


def _who(decision: dict) -> str:
    """For the activity feed: in the dashboard that's the reader, and `by` is only an account id."""
    return "you" if decision.get("source") == "dashboard" else decision.get("by") or "you"


def _short(text) -> str:
    return str(text)[:ERROR_MAX]


# ---------------------------------------------------------------------------
# Changing a card
# ---------------------------------------------------------------------------

def _find_line(card: dict, line_id: str) -> dict | None:
    return next((ln for ln in card.get("lines") or [] if ln.get("id") == line_id), None)


def _status_after(status: str, lines: list[dict]) -> str:
    """partly_applied from the first applied line until nothing waits on anyone, then applied. Flag
    lines wait on nobody. A closed card keeps its status."""
    if status not in (STATUS_OPEN, STATUS_PARTLY_APPLIED, STATUS_APPLIED):
        return status
    states = [ln.get("state") for ln in lines]
    if status == STATUS_OPEN and states and all(s == STATE_FLAGGED for s in states):
        return STATUS_FLAGGED  # the mode changed: nothing here can be applied any more
    if not any(s in _TOUCHED for s in states):
        return status
    return STATUS_PARTLY_APPLIED if any(s in _WAITING for s in states) else STATUS_APPLIED


async def _change(db, card_id: str, change, *, statuses=_LIVE) -> dict | None:
    """Apply `change(card) -> fields | None` through guarded_update, reading again when another
    writer got in first. The card as written, or None when it didn't happen."""
    for _ in range(_RACE_TRIES):
        card = await db[CARDS].find_one({"_id": card_id})
        if card is None or card.get("status") not in statuses:
            return None
        fields = change(card)
        if fields is None:
            return None
        if await guarded_update(db, card, fields, statuses=statuses):
            return {**card, **fields}
    logger.warning("stock card %s: gave up on a write after %s races", card_id, _RACE_TRIES)
    return None


async def _set_line(db, card_id: str, line_id: str, *, expect: tuple, statuses=_LIVE, **changes) -> dict | None:
    """Move one line that is still in an `expect` state. The card as written, or None."""
    def change(card):
        line = _find_line(card, line_id)
        if line is None or line.get("state") not in expect:
            return None
        lines = [{**ln, **changes} if ln.get("id") == line_id else ln for ln in card["lines"]]
        return {"lines": lines, "status": _status_after(card["status"], lines)}

    return await _change(db, card_id, change, statuses=statuses)


def _decision(actor: str, by: str | None, source: str | None, now: datetime) -> dict:
    return {"actor": actor, "by": by, "source": source, "at": now}


def _work_order(line: dict) -> int:
    """Removes first, so a swap never goes over the live ad limit on the way."""
    return 0 if line.get("kind") == "remove" else 1


async def _queue_lines(db, card_id: str, wanted, decision: dict) -> list[tuple[str, str]]:
    """The atomic claim: wanted lines move to `queued` in one write. Returns what this call queued,
    in working order. Empty when another caller has them or the card can't be applied."""
    queued: list[dict] = []

    def change(card):
        queued.clear()
        lines = []
        for ln in card.get("lines") or []:
            if wanted(ln):
                ln = {**ln, "state": STATE_QUEUED, "error": None, "reason_code": None, "decision": decision,
                      **({"needs_file": False} if ln.get("needs_file") else {})}
                queued.append(ln)
            lines.append(ln)
        if not queued:
            return None
        fields = {"lines": lines, "status": _status_after(card["status"], lines)}
        if not card.get("decided_by"):
            fields.update(decided_by=decision, decided_at=decision["at"])
        return fields

    if await _change(db, card_id, change, statuses=_ACTIONABLE) is None:
        return []
    return [(card_id, ln["id"]) for ln in sorted(queued, key=_work_order)]


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

async def has_newer_card(db, card: dict) -> bool:
    """A later card exists for this card's client, whatever state that one has reached."""
    created = card.get("created_at")
    if created is None:
        return False
    return await db[CARDS].find_one(
        {"agent_id": card.get("agent_id"), "client_group_id": card.get("client_group_id"), "_id": {"$ne": card["_id"]},
         "preview": {"$ne": True}, "created_at": {"$gt": created}}, {"_id": 1}) is not None


def _settings(agent: dict) -> dict:
    return merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})


async def _load_agent(db, card: dict) -> dict | None:
    return await db[AGENTS].find_one({"_id": card.get("agent_id"), "user_id": card.get("user_id")})


def _is_on(agent: dict | None) -> bool:
    return bool(agent) and agent.get("status") == "active"


async def _refusal(db, agent: dict | None, card: dict) -> dict | None:
    """Things that changed since the decision. Checked for every line, however it was queued."""
    if not _is_on(agent):
        return AGENT_OFF
    status = card.get("status")
    if status == STATUS_DISMISSED:
        return DISMISSED
    if status not in _ACTIONABLE or await has_newer_card(db, card):
        return SUPERSEDED
    client = ((_settings(agent).get("clients") or {}).get(card.get("client_group_id"))) or {}
    return ON_HOLD if client.get("on_hold") else None


def _target_doc(card: dict, line: dict, atype: str) -> dict:
    """The shape check_guardrails reads. Given a stored agent, its `_id` turns on the
    not_acting_agent check."""
    return {"action": {"type": atype, "targets": [{"object_id": _ad_id(line), "object_type": "ad",
                                                   "name": line.get("ad_name")}]},
            "client_group_id": card.get("client_group_id"), "client_name": card.get("client_name"),
            "user_id": card.get("user_id"), "agent_id": card.get("agent_id"), "title": line.get("ad_name")}


def _ad_id(line: dict) -> str | None:
    ids = line.get("meta_ids") or []
    return str(ids[0]) if ids else None


async def adds_in_flight(db, gid: str, adset_id: str, skip: tuple, now: datetime) -> set[str]:
    """New ads the ad set will have that a read of Meta can't be relied on to show: Adds handed to the
    pipeline, and ones applied too recently for a read to have caught up. Their ad ids where they have one,
    so a caller can take the union with what it read. `skip` is (card id, line id) of the line being checked."""
    found: set[str] = set()
    query = {"client_group_id": gid, "lines": {"$elemMatch": {"kind": "add", "state": {"$in": [STATE_APPLYING, STATE_APPLIED]}}}}
    async for card in db[CARDS].find(query):
        for ln in card.get("lines") or []:
            if (ln.get("kind") != "add" or (card["_id"], ln.get("id")) == skip
                    or str((ln.get("destination") or {}).get("adset_id")) != str(adset_id)):
                continue
            sent = ln.get("state") == STATE_APPLYING and ln.get("write_started_at")
            recent = ln.get("state") == STATE_APPLIED and ln.get("applied_at") and now - ln["applied_at"] < MIRROR_TRUSTED_FOR
            if sent or recent:
                # An ad on its way has its id on the pipeline's row: the read may already list it.
                post = await db[TEST_ADS].find_one({"id": ln.get("test_ad_id"), "user_id": card.get("user_id")}) or {}
                found.add(_ad_id(ln) or (post.get("meta") or {}).get("ad_id") or f"{card['_id']}:{ln.get('id')}")
    return found


async def running_ads(db, gid: str, adset_id: str, ads: list[dict], now: datetime) -> set[str]:
    """What takes a place under max_ads_live, from a read of the ad set. Off in the mirror just now is more
    recent than a read that can lag: a swap's Remove has only just gone."""
    running = {str(a.get("id")) for a in ads if str(a.get("effective_status") or "").upper() in WILL_RUN}
    off = db[AD_STATE].find({"client_group_id": gid, "adset_id": adset_id, "effective_status": {"$ne": "ACTIVE"},
                             "updated_at": {"$gte": now - MIRROR_TRUSTED_FOR}})
    return running - {str(r["ad_id"]) async for r in off}


def _over_live_limit(agent: dict, ad_id: str, live: set[str]) -> dict | None:
    """Live ads per ad set never go over max_ads_live (spec section 6), however old the card is."""
    limit = _settings(agent)["creative"]["max_ads_live"]
    if ad_id in live or len(live) < limit:
        return None
    return {"code": "max_ads_live",
            "message": f"Turning this on would make {len(live) + 1} ads live in its ad set, over the limit of {limit}."}


def _sent_pause(line: dict) -> bool:
    """A pause that has gone to Meta with no answer yet: a Remove, or the Undo of a Bring back or an Add."""
    state, kind = line.get("state"), line.get("kind")
    return bool((state == STATE_APPLYING and kind == "remove" and line.get("write_started_at"))
                or (state == STATE_UNDOING and kind in ("revive", "add") and line.get("undo_write_started_at")))


async def _unconfirmed_pauses(db, gid: str, card: dict, line: dict) -> tuple[set[str], int]:
    """(The ads of this client's sent, unconfirmed pauses, how many of those the pause cap can't see yet.)
    An Undo is never in the cap. A line whose activity row exists is already counted by that row, and the
    line being checked isn't counted, since a Retry can still carry the marker of its earlier try."""
    ads: set[str] = set()
    refs: list[str] = []
    async for row in db[CARDS].find({"client_group_id": gid, "lines.state": {"$in": [STATE_APPLYING, STATE_UNDOING]}}):
        for ln in row.get("lines") or []:
            if _sent_pause(ln) and not (row["_id"] == card["_id"] and ln.get("id") == line.get("id")):
                ads.add(_ad_id(ln))
                if ln.get("state") == STATE_APPLYING:
                    refs.append(f"{row['_id']}:{ln.get('id')}")
    if not refs:
        return ads, 0
    logged = await db[ACTIVITY].count_documents({"ref_id": {"$in": refs}, "kind": KIND_ACTION_APPLIED})
    return ads, len(refs) - logged


async def _would_empty_adset(db, gid: str, adset_id: str, ad_id: str, live: set[str], going_off: set[str],
                             now: datetime) -> bool:
    """Would pausing this leave nothing live? `going_off` isn't a spare. Nor is an ad the mirror recently
    set to off, even if Meta lists it live: a read just after a write can lag. An older "off" is out of
    date if Meta says live, say when someone turned it on in Ads Manager."""
    rows = db[AD_STATE].find({"client_group_id": gid, "adset_id": adset_id, "effective_status": {"$ne": "ACTIVE"},
                              "updated_at": {"$gte": now - MIRROR_TRUSTED_FOR}})
    recently_off = {str(r["ad_id"]) async for r in rows}
    return bool(live) and not (live - {ad_id} - going_off - recently_off)


async def _guardrail_block(db, mongo_client, agent: dict, card: dict, line: dict, atype: str, now: datetime, *,
                           undo: bool = False) -> dict | None:
    """Every check before a write. The stored rules run with the last live ad check off, because the
    list from Meta decides that. An Undo restores what Birdy changed, so it skips the pause cap, the live
    ad limit and taught exceptions, and its acting agent check is done by the caller."""
    gid, ad_id = card.get("client_group_id"), _ad_id(line)
    going_off, unseen = await _unconfirmed_pauses(db, gid, card, line)
    rules = {**(agent.get("guardrails") or {}), "protect_last_live_ad": False}
    guard = {**agent, "guardrails": rules}
    if undo:
        guard.pop("_id", None)
        guard.update(guardrails={**rules, "max_pauses_per_client_day": None}, client_caps={})
    doc = _target_doc(card, line, atype)
    g = await check_guardrails(db, guard, doc, now_utc=now, unconfirmed_pauses=0 if undo else unseen)
    if not g.ok:
        return {"code": g.reason, "message": g.message}

    row = await db[AD_STATE].find_one({"client_group_id": gid, "ad_id": ad_id})
    adset_id = str(row["adset_id"]) if row and row.get("adset_id") else None
    if adset_id is None:
        return NO_ADSET
    target = {"object_id": adset_id, "object_type": "adset", "name": line.get("ad_name"), "only_ad_id": ad_id}
    read: dict = {}
    memories = [] if undo else await list_memory(db, agent["_id"], active_only=True)
    g = await check_adset_contents_live(db, mongo_client, agent, {**doc, "action": {"type": atype, "targets": [target]}},
                                        memories=memories, now_utc=now, read_ads=read)
    if not g.ok:
        return {"code": g.reason, "message": g.message}
    live = {str(a.get("id")) for a in read.get(adset_id) or [] if str(a.get("effective_status") or "").upper() == "ACTIVE"}
    if atype == ACTION_PAUSE_ADS:
        protect = {**DEFAULT_GUARDRAILS, **(agent.get("guardrails") or {})}.get("protect_last_live_ad")
        empties = protect and await _would_empty_adset(db, gid, adset_id, ad_id, live, going_off, now)
        return LAST_LIVE if empties else None
    if undo:
        return None
    running = await running_ads(db, gid, adset_id, read.get(adset_id) or [], now)
    return _over_live_limit(agent, ad_id, running | await adds_in_flight(db, gid, adset_id, (card["_id"], line.get("id")), now))


# ---------------------------------------------------------------------------
# The lease
# ---------------------------------------------------------------------------

def _lease_key(agent_id: str, gid: str) -> str:
    return f"{agent_id}:{gid}"


async def _take_lease(db, key: str) -> str | None:
    """A token, or None while another invocation holds this lease. The real clock on purpose."""
    token, now = uuid.uuid4().hex, datetime.utcnow()
    try:
        await db[LEASES].find_one_and_update({"_id": key, "expires_at": {"$lte": now}},
                                             {"$set": {"token": token, "expires_at": now + LEASE_TTL}}, upsert=True)
    except DuplicateKeyError:
        return None
    return token


async def _wait_for_lease(db, key: str) -> str | None:
    give_up = time.monotonic() + UNDO_LEASE_WAIT_S
    while True:
        token = await _take_lease(db, key)
        left = give_up - time.monotonic()
        if token or left <= 0:
            return token
        await asyncio.sleep(min(_LEASE_POLL_S, left))


async def _release_lease(db, key: str, token: str) -> None:
    await db[LEASES].delete_one({"_id": key, "token": token})


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def _send_and_confirm(mongo_client, card: dict, line: dict, atype: str) -> tuple[bool | None, str | None]:
    """(landed, Meta's error). A reply that isn't a success doesn't prove the write failed, so it is
    read back. landed is False only when Meta said no. It is None when only a later read can say: Meta
    can't be read, or the send was lost, since a timed-out write can land after this read."""
    user_id, ad_id = card["user_id"], _ad_id(line)
    target = {"object_id": ad_id, "object_type": "ad", "name": line.get("ad_name")}
    error, lost = None, False
    try:
        res = await execute_action(mongo_client, user_id, {"type": atype, "targets": [target]})
        if res.get("succeeded"):
            return True, None
        error = ((res.get("failed") or [{}])[0]).get("error") or "Meta didn't accept this."
        lost = error.startswith("Failed to reach Meta")
    except Exception as e:  # noqa: BLE001 - same as a lost reply: read what really happened
        logger.error("stock card %s: send for %s raised: %s", card["_id"], ad_id, e, exc_info=True)
        error, lost = "Meta didn't accept this.", True
    try:
        state = await get_object_state(user_id, ad_id, "ad", mongo_client)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock card %s: read-back of %s failed: %s", card["_id"], ad_id, e)
        return None, error
    if _landed(atype, state):
        return True, error
    return (None if lost else False), error


async def _log_once(db, card: dict, line: dict, kind: str, *, label: str, title: str, decision: dict,
                    action_type: str | None = None, note: str | None = None) -> None:
    """One activity row per line and kind, however many times recovery gets here. The row's
    applied_action_type is what the daily pause cap counts, so it's written before the line is marked."""
    ref = f"{card['_id']}:{line['id']}"
    if await db[ACTIVITY].find_one({"ref_id": ref, "kind": kind}, {"_id": 1}):
        return
    await log_activity(db, card["user_id"], actor=decision.get("actor") or ACTOR_USER, kind=kind, title=title,
                       client_name=card.get("client_name"), client_group_id=card.get("client_group_id"),
                       ref_id=ref, label=label, source=decision.get("source"), agent_id=card.get("agent_id"),
                       applied_action_type=action_type, note=note)


async def _mirror(db, card: dict, agent: dict | None, line: dict, atype: str, now: datetime) -> None:
    """So the next line's last-live-ad check sees this. Not best-effort: if it raises, the line stays
    unfinished and recovery does it again."""
    await record_status_write(db, card["client_group_id"], [_ad_id(line)], _desired(atype),
                              today=local_today(now, agent_tz(agent)))


async def _finish_applied(db, card: dict, agent: dict | None, line: dict, atype: str, now: datetime, *,
                          wrote: bool, written_at: datetime | None = None) -> None:
    """`written_at` is when the write really went, which recovery can only learn from the line. The Undo
    window runs from there."""
    applied_at = written_at or now
    decision = line.get("decision") or {}
    note = None if wrote else ALREADY_DONE
    await _mirror(db, card, agent, line, atype, now)
    verb = "Paused" if atype == ACTION_PAUSE_ADS else "Turned on"
    label = "Auto-run by Birdy" if decision.get("actor") == ACTOR_BIRDY else f"Approved by {_who(decision)}"
    await _log_once(db, card, line, KIND_ACTION_APPLIED, label=label, title=f"{verb} {line.get('ad_name') or 'an ad'}",
                    decision=decision, action_type=atype if wrote else None, note=note)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_APPLIED, applied_at=applied_at,
                    undo_until=applied_at + UNDO_AUTO_WINDOW, wrote=wrote, note=note, error=None, reason_code=None,
                    cut_at=None)


async def _block(db, card: dict, line: dict, reason: dict) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_BLOCKED,
                    error=_short(reason["message"] + (STAYS_PAUSED if line.get("paused_ad_id") else "")),
                    reason_code=reason["code"])


async def _fail(db, card: dict, line: dict, error: str, **extra) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_FAILED,
                    error=_short(error), reason_code=None, **extra)


async def _lost_the_last_check(db, agent: dict | None, card: dict, line: dict) -> None:
    """The write that stores "something may have been sent" didn't happen, so nothing was. Either the
    card can't be applied now, or another write got in first and the line goes round again."""
    fresh = await db[CARDS].find_one({"_id": card["_id"]})
    reason = await _refusal(db, agent, fresh)
    if reason is not None:
        return await _block(db, card, line, reason)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_QUEUED)
    return None


async def _apply_claimed(db, mongo_client, card: dict, line: dict, now: datetime, sent: list) -> None:
    agent = await _load_agent(db, card)
    refusal = await _refusal(db, agent, card)
    is_add = line["kind"] == "add"
    if refusal is None and not is_add:  # an Add has no ad until it is posted, and only ever makes one
        ids = line.get("meta_ids") or []
        refusal = SEVERAL_ADS if len(ids) > 1 else None if ids else NO_AD
    if refusal is None:
        mode = resolve_mode(_settings(agent), card.get("client_group_id"), line["kind"])
        if mode == "flag":
            await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_FLAGGED, mode="flag",
                            error=None, reason_code=None, note=NOW_FLAG + (STAYS_PAUSED if line.get("paused_ad_id") else ""))
            return None
        if mode != "auto" and (line.get("decision") or {}).get("actor") == ACTOR_BIRDY:
            await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_PROPOSED, mode=mode,
                            decision=None)
            return None
    if is_add and refusal is None:
        from ai.agents.stock.add import apply_add  # add builds on this module
        return await apply_add(db, mongo_client, card, line, agent, now, sent)
    atype = _action_type(line["kind"])
    if refusal is None:
        refusal = await _guardrail_block(db, mongo_client, agent, card, line, atype, now)
    if refusal is not None:
        return await _block(db, card, line, refusal)

    ad_id = _ad_id(line)
    try:
        state = await get_object_state(card["user_id"], ad_id, "ad", mongo_client)
    except Exception as e:  # noqa: BLE001 - nothing has been sent, so this is safe to say plainly
        logger.warning("stock card %s: read of %s failed: %s", card["_id"], ad_id, e)
        return await _fail(db, card, line, "Birdy couldn't read this ad in Meta, so nothing was changed. Try again.")
    if state is None and atype == ACTION_ENABLE_ADS:
        return await _fail(db, card, line, GONE)
    if _is_desired(atype, state):
        # Done already. If an earlier try sent it, that was Birdy's write and it counts.
        return await _finish_applied(db, card, agent, line, atype, now, wrote=bool(line.get("write_started_at")),
                                     written_at=line.get("write_started_at"))

    # The last check: this write is refused if the card closed or changed status, and nothing is
    # sent until it's stored that something may have been.
    started = await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), statuses=_ACTIONABLE,
                              write_started_at=now)
    if started is None:
        return await _lost_the_last_check(db, agent, card, line)
    sent.append(True)
    landed, error = await _send_and_confirm(mongo_client, card, line, atype)
    if landed:
        await _finish_applied(db, card, agent, line, atype, now, wrote=True)
    elif landed is False:
        await _fail(db, card, line, error, write_started_at=None)
    # landed is None: left `applying` with write_started_at, for recovery to read.


async def _run_line(db, mongo_client, card_id: str, line_id: str, now: datetime) -> bool:
    """Take one queued line and see it through. A raise here stays with this line. If this client's lease
    is taken, the line stays queued for the cron. False when this call didn't take the line."""
    row = await db[CARDS].find_one({"_id": card_id}, {"agent_id": 1, "client_group_id": 1})
    if row is None:
        return False
    key = _lease_key(row.get("agent_id"), row.get("client_group_id"))
    token = await _take_lease(db, key)
    if token is None:
        return False
    try:
        card = await _set_line(db, card_id, line_id, expect=(STATE_QUEUED,), state=STATE_APPLYING, applying_at=now,
                               cut_at=None)
        if card is None:
            return False
        line = _find_line(card, line_id)
        sent: list = []
        try:
            await _apply_claimed(db, mongo_client, card, line, now, sent)
        except asyncio.CancelledError:  # the budget ran out
            if sent:  # this invocation is ending, so the next tick can read it safely
                await _set_line(db, card_id, line_id, expect=(STATE_APPLYING,), cut_at=datetime.utcnow())
            else:
                await _set_line(db, card_id, line_id, expect=(STATE_APPLYING,), state=STATE_QUEUED)
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("stock card %s line %s failed: %s", card_id, line_id, e, exc_info=True)
            if not sent:
                await _fail(db, card, line, NOTHING_CHANGED)
            # Once sent, it stays `applying`: only a read of Meta can say what happened.
        return True
    finally:
        await _release_lease(db, key, token)


async def _run_line_safely(db, mongo_client, card_id: str, line_id: str, now: datetime) -> bool:
    try:
        return await _run_line(db, mongo_client, card_id, line_id, now)
    except Exception as e:  # noqa: BLE001 - the store itself failed. The cron picks the line up again
        logger.error("stock card %s line %s: couldn't save its state: %s", card_id, line_id, e, exc_info=True)
        return False


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _no_time_for_a_line(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() + MIN_TIME_FOR_LINE_S >= deadline


async def _drain(db, mongo_client, pairs: list[tuple[str, str]], *, deadline: float | None = None,
                 budget_s: float | None = None, now: datetime | None = None) -> None:
    """Work through queued lines. `deadline` is checked between lines. `budget_s` is hard, for a caller
    that must answer in time, such as Slack: a Graph call can take 30 s, so it is cut off, which is
    no worse than being killed. What's left stays queued for the cron."""
    async def work():
        for card_id, line_id in pairs:
            if _out_of_time(deadline):
                return
            await _run_line_safely(db, mongo_client, card_id, line_id, now or datetime.utcnow())

    if budget_s is None:
        return await work()
    if budget_s <= 0:
        return None
    try:
        await asyncio.wait_for(work(), timeout=budget_s)
    except asyncio.TimeoutError:
        logger.info("stock apply: budget of %ss used, the cron finishes the rest", budget_s)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def _line_results(lines: list[dict]) -> list[dict]:
    return [{"id": ln.get("id"), "state": ln.get("state"), "error": ln.get("error"),
             "reason_code": ln.get("reason_code"), "note": ln.get("note")} for ln in lines]


def _answer(outcome: str, card: dict | None, *, ok: bool = True, reason: str | None = None, **extra) -> dict:
    return {"ok": ok, "outcome": outcome, "reason": reason, "card": card,
            "lines": _line_results((card or {}).get("lines") or []), **extra}


def _outcome(card: dict, line_ids: list[str]) -> dict:
    """What this call's own lines came to."""
    lines = [ln for ln in card.get("lines") or [] if ln.get("id") in line_ids]
    states = [ln.get("state") for ln in lines]
    why = next((ln.get("error") for ln in lines if ln.get("error")), None)
    if any(s in _IN_PROGRESS for s in states):
        return _answer("queued", card)
    if all(s == STATE_FLAGGED for s in states):
        return _answer("flagged", card, reason=NOW_FLAG)
    if all(s == STATE_APPLIED for s in states):
        return _answer("applied", card)
    if all(s == STATE_NEEDS_FILE for s in states):
        return _answer("needs_file", card, ok=False, reason=why)
    if STATE_APPLIED in states:
        return _answer("partial", card, reason=why)
    return _answer("failed" if STATE_FAILED in states else "blocked", card, ok=False, reason=why)


async def _card_refusal(db, card: dict) -> str | None:
    if card.get("preview"):
        return PREVIEW_CARD
    status = card.get("status")
    if status == STATUS_FLAGGED:
        return FLAG_CARD
    if status == STATUS_DISMISSED:
        return DISMISSED["message"]
    if status == STATUS_APPLIED:
        return None  # a repeat press: it returns what happened
    if status not in _ACTIONABLE or await has_newer_card(db, card):
        return SUPERSEDED["message"]
    return None


async def _visible_card(db, user_id: str, card_id: str) -> dict | None:
    card = await db[CARDS].find_one({"_id": card_id, "user_id": user_id})
    return None if card is None or card.get("status") == STATUS_CLOSING else card


async def apply_card(db, mongo_client, user_id: str, card_id: str, *, by: str | None, source: str | None,
                     line_ids: list[str] | None = None, budget_s: float | None = None) -> dict:
    """Approve a card's lines. With no `line_ids`, every proposed line. Naming a line also retries
    it if it failed, was blocked or was waiting for a file. Never writes a Flag line. `budget_s` limits
    the wait, and what doesn't fit is finished by the cron."""
    card = await _visible_card(db, user_id, card_id)
    if card is None:
        return _answer("not_found", None, ok=False)
    known = {ln.get("id") for ln in card.get("lines") or []}
    if line_ids is not None and any(i not in known for i in line_ids):
        return _answer("bad_lines", card, ok=False, reason="Those aren't lines of this card.")
    refusal = await _card_refusal(db, card)
    if refusal:
        return _answer("refused", card, ok=False, reason=refusal)

    def wanted(ln):
        if ln.get("kind") not in WRITABLE_KINDS or ln.get("mode") == "flag":
            return False
        if line_ids is None:
            return ln.get("state") == STATE_PROPOSED
        return ln.get("id") in line_ids and ln.get("state") in _RETRYABLE

    now = datetime.utcnow()
    pairs = await _queue_lines(db, card_id, wanted, _decision(ACTOR_USER, by, source, now))
    if not pairs:
        fresh = await db[CARDS].find_one({"_id": card_id})
        refusal = await _card_refusal(db, fresh)  # lost to a dismissal or a supersede
        if refusal:
            return _answer("refused", fresh, ok=False, reason=refusal)
        looked_at = [ln for ln in fresh.get("lines") or [] if line_ids is None or ln.get("id") in line_ids]
        acted = any(ln.get("state") not in (STATE_PROPOSED, STATE_FLAGGED) for ln in looked_at)
        return _answer("already" if acted else "noop", fresh)
    await _drain(db, mongo_client, pairs, budget_s=budget_s)
    return _outcome(await db[CARDS].find_one({"_id": card_id}), [p[1] for p in pairs])


async def _queue_run(db, run: dict, decision: dict, *, deadline: float | None) -> tuple[list[tuple[str, str]], bool]:
    """Queue the proposed lines of the cards that were showing when Apply all was pressed. A card
    arriving later hasn't been seen by whoever pressed it. Returns (queued, finished)."""
    def wanted(ln):
        return (ln.get("kind") in WRITABLE_KINDS and ln.get("mode") != "flag" and ln.get("state") == STATE_PROPOSED)

    cards = await db[CARDS].find({"run_id": run["_id"], "status": {"$in": list(_ACTIONABLE)}, "preview": {"$ne": True},
                                  "created_at": {"$lte": decision["at"]}}).to_list(length=None)
    pairs: list[tuple[str, str]] = []
    for card in cards:
        if _out_of_time(deadline):
            return pairs, False
        if not await has_newer_card(db, card):
            pairs += await _queue_lines(db, card["_id"], wanted, decision)
    return pairs, True


async def _finish_apply_all(db, run_id: str) -> None:
    await db[RUNS].update_one({"_id": run_id}, {"$set": {"apply_all.pending": False}})


async def apply_run(db, mongo_client, user_id: str, run_id: str, *, by: str | None, source: str | None,
                    budget_s: float = 2.5) -> dict:
    """Apply all. It answers inside `budget_s`: the 2.5 s default is for Slack, where every line queues,
    and the dashboard passes a longer budget so lines start at once. The press is stored first, so
    whatever doesn't fit, even queueing, is finished by the cron."""
    deadline = time.monotonic() + budget_s
    run = await db[RUNS].find_one({"_id": run_id, "user_id": user_id})
    if run is None:
        return {"ok": False, "outcome": "not_found", "reason": None}
    if run.get("kind") == "preview":
        return {"ok": False, "outcome": "refused", "reason": PREVIEW_CARD}
    decision = _decision(ACTOR_USER, by, source, datetime.utcnow())
    await db[RUNS].update_one({"_id": run_id}, {"$set": {"apply_all": {**decision, "pending": True}}})
    pairs, finished = await _queue_run(db, run, decision, deadline=deadline)
    if finished:
        await _finish_apply_all(db, run_id)
    # Slack's default budget is under MIN_TIME_FOR_LINE_S, so no line starts there and the cron runs them.
    await _drain(db, mongo_client, pairs, deadline=deadline - MIN_TIME_FOR_LINE_S, budget_s=deadline - time.monotonic())

    states: dict[str, int] = {}
    by_card: dict[str, list[str]] = {}
    for card_id, line_id in pairs:
        by_card.setdefault(card_id, []).append(line_id)
    if by_card:
        for card in await db[CARDS].find({"_id": {"$in": list(by_card)}}).to_list(length=None):
            for ln in card["lines"]:
                if ln["id"] in by_card[card["_id"]]:
                    states[ln["state"]] = states.get(ln["state"], 0) + 1
    counts = {"applied": states.get(STATE_APPLIED, 0), "blocked": states.get(STATE_BLOCKED, 0),
              "failed": states.get(STATE_FAILED, 0), "needs_file": states.get(STATE_NEEDS_FILE, 0),
              "queued": sum(states.get(s, 0) for s in (STATE_QUEUED, STATE_APPLYING))}
    if counts["queued"] or not finished:
        outcome = "queued"
    elif not pairs:
        outcome = "noop"
    else:
        outcome = ("applied" if counts["applied"] else "failed" if counts["failed"]
                   else "blocked" if counts["blocked"] or counts["needs_file"]
                   else "noop")  # noop: they had all turned into Flag
    return {"ok": True, "outcome": outcome, "reason": None, **counts}


# ---------------------------------------------------------------------------
# Auto
# ---------------------------------------------------------------------------

def _open_alert(run: dict, gid: str) -> bool:
    """Info alerts such as prepaid_no_topups and spend_data_late aren't open alerts."""
    return any(a.get("client_group_id") in (gid, None) and a.get("severity") != "info"
               for a in run.get("alerts") or [])


def _fallback(card: dict, client: dict, run: dict, line: dict, adds_so_far: int, add_limit: int) -> str | None:
    """Why this Auto line must ask a person instead (spec section 7), or None."""
    if card.get("stale") or client.get("stale"):
        return "stale"
    if _open_alert(run, card.get("client_group_id")):
        return "open_alert"
    if line.get("flags"):
        return "flagged_text"
    if card.get("ai_failed"):
        return "ai_failed"
    if line.get("kind") == "add":
        if adds_so_far > add_limit:
            return "over_add_limit"
        if line.get("needs_file") or line.get("state") == STATE_NEEDS_FILE:
            return "needs_file"
    return None


async def _queue_auto(db, card: dict, client: dict, run: dict, settings: dict, add_limit: int,
                      decision: dict) -> list[tuple[str, str]]:
    """In one write: send fall-backs to Ask me, for good, and queue what may run. A fall-back stays
    even when its reason passes, because a person may already be looking at an Apply button. Lines follow
    the current settings: the mode may have changed since the card was written."""
    queued: list[dict] = []

    def change(fresh):
        queued.clear()
        adds, lines, changed = 0, [], False
        for ln in fresh.get("lines") or []:
            auto = ln.get("mode") == "auto"
            if auto and ln.get("kind") == "add":
                adds += 1
            mode = resolve_mode(settings, fresh.get("client_group_id"), ln["kind"])
            if auto and ln.get("state") == STATE_PROPOSED and mode != "auto":
                ln, changed = {**ln, "mode": mode, "state": STATE_FLAGGED if mode == "flag" else STATE_PROPOSED}, True
            elif auto and ln.get("state") in (STATE_PROPOSED, STATE_NEEDS_FILE):
                why = _fallback(fresh, client, run, ln, adds, add_limit)
                if why:
                    ln, changed = {**ln, "mode": "ask", "auto_fallback": why}, True
                elif ln.get("state") == STATE_PROPOSED:
                    ln, changed = {**ln, "state": STATE_QUEUED, "error": None, "decision": decision}, True
                    queued.append(ln)
            lines.append(ln)
        if not changed:
            return None
        fields = {"lines": lines, "status": _status_after(fresh["status"], lines)}
        if queued and not fresh.get("decided_by"):
            fields.update(decided_by=decision, decided_at=decision["at"])
        return fields

    if await _change(db, card["_id"], change, statuses=_ACTIONABLE) is None:
        return []
    return [(card["_id"], ln["id"]) for ln in sorted(queued, key=_work_order)]


async def auto_apply_run(db, mongo_client, run: dict, agent: dict, *, deadline: float | None = None) -> None:
    """The run's Auto changes. Runs again as more cards arrive and may repeat after a kill, so it
    only acts on lines still proposed. A client whose health is unread is left alone: the run comes
    back through here when the health pass succeeds. Lines are queued before any is sent, so
    what doesn't fit before `deadline` goes on from the cron even if the run has finished."""
    if run.get("kind") == "preview" or run.get("reconnect"):
        return
    agent = await db[AGENTS].find_one({"_id": agent["_id"]})  # it may have been turned off this tick
    if not _is_on(agent):
        return
    settings = _settings(agent)
    add_limit = settings["creative"]["auto_max_adds"]
    now = datetime.utcnow()
    decision = _decision(ACTOR_BIRDY, "Birdy", "auto", now)
    cards = await db[CARDS].find({"run_id": run["_id"], "status": {"$in": list(_ACTIONABLE)},
                                  "preview": {"$ne": True}}).to_list(length=None)
    pairs: list[tuple[str, str]] = []
    for card in cards:
        client = (run.get("clients") or {}).get(card.get("client_group_id")) or {}
        if client.get("health_due") or await has_newer_card(db, card):
            continue
        pairs += await _queue_auto(db, card, client, run, settings, add_limit, decision)
    await _drain(db, mongo_client, pairs, deadline=deadline)


# ---------------------------------------------------------------------------
# The cron: finishing and recovering
# ---------------------------------------------------------------------------

async def _read_ad(card: dict, line: dict, mongo_client) -> dict | None:
    return await asyncio.wait_for(get_object_state(card["user_id"], _ad_id(line), "ad", mongo_client),
                                  timeout=RECOVERY_READ_TIMEOUT_S)


async def _recover_applying(db, mongo_client, card: dict, line: dict, now: datetime) -> None:
    """See the module docstring. This never sends anything."""
    if not line.get("write_started_at"):
        tries = (line.get("recoveries") or 0) + 1
        if tries > MAX_RECOVERIES:
            return await _fail(db, card, line, NOTHING_CHANGED)
        await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_QUEUED, recoveries=tries)
        return None
    atype = _action_type(line["kind"])
    try:
        state = await _read_ad(card, line, mongo_client)
    except Exception as e:  # noqa: BLE001 - keep asking, and don't guess
        logger.warning("stock card %s line %s: can't confirm yet: %s", card["_id"], line["id"], e)
        if now - (line.get("write_started_at") or now) > CONFIRM_GIVE_UP:
            await _fail(db, card, line, COULD_NOT_CONFIRM)
        return None
    if _landed(atype, state):
        return await _finish_applied(db, card, await _load_agent(db, card), line, atype, now, wrote=True,
                                     written_at=line["write_started_at"])
    # write_started_at stays: if this lands late, a Retry finds it done and counts it as Birdy's write.
    return await _fail(db, card, line, _not_landed(state))


async def _undo_back(db, card: dict, line: dict, error: str) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), state=STATE_APPLIED, undo_error=_short(error),
                    undo_write_started_at=None)


async def _finish_undone(db, card: dict, agent: dict | None, line: dict, atype: str, now: datetime, *,
                         note: str | None = None) -> None:
    decision = line.get("undo_decision") or {}
    if atype:
        await _mirror(db, card, agent, line, atype, now)
    if line.get("kind") == "add":
        await mark_add_undone(db, card, line, now)
        await db[TEST_ADS].update_one({"id": line.get("test_ad_id"), "user_id": card.get("user_id")},
                                      {"$set": {"meta_status": "PAUSED", "paused_at": now, "updated_at": now}})
    await _log_once(db, card, line, KIND_ACTION_UNDONE, label=f"Undone by {_who(decision)}",
                    title=f"Undid: {line.get('ad_name') or 'an ad'}", decision=decision, note=note)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), state=STATE_UNDONE, undone_at=now,
                    undo_error=None, note=note)


async def _recover_undoing(db, mongo_client, card: dict, line: dict, now: datetime) -> None:
    if not line.get("undo_write_started_at"):
        return await _undo_back(db, card, line, NOTHING_CHANGED)
    atype = _action_type(line["kind"], undo=True)
    try:
        state = await _read_ad(card, line, mongo_client)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock card %s line %s: can't confirm the undo yet: %s", card["_id"], line["id"], e)
        if now - line["undo_write_started_at"] > CONFIRM_GIVE_UP:
            await _undo_back(db, card, line, COULD_NOT_CONFIRM)
        return None
    if _landed(atype, state):
        return await _finish_undone(db, card, await _load_agent(db, card), line, atype, now)
    return await _undo_back(db, card, line, _not_landed(state))


async def _recover_stuck(db, mongo_client, now: datetime, deadline: float | None) -> int:
    cards = await db[CARDS].find({"lines.state": {"$in": [STATE_APPLYING, STATE_UNDOING]}}).sort("updated_at", 1).limit(
        QUEUE_BATCH).to_list(length=QUEUE_BATCH)
    recovered = 0
    for card in cards:
        for line in card.get("lines") or []:
            if _out_of_time(deadline):
                return recovered
            state = line.get("state")
            since = line.get("applying_at") if state == STATE_APPLYING else line.get("undoing_at")
            if (state not in (STATE_APPLYING, STATE_UNDOING) or line.get("kind") not in WRITABLE_KINDS
                    or (since and now - since < STUCK_AFTER and not (state == STATE_APPLYING and line.get("cut_at")))):
                continue
            if line.get("kind") == "add" and state == STATE_APPLYING and line.get("write_started_at"):
                continue  # the pipeline has it, and add.sync_add_lines follows it
            logger.warning("stock card %s line %s stuck in %s since %s", card["_id"], line["id"], state, since)
            recover = _recover_applying if state == STATE_APPLYING else _recover_undoing
            try:
                await recover(db, mongo_client, card, line, now)
                recovered += 1
            except Exception as e:  # noqa: BLE001 - it stays stuck and is tried next minute
                logger.error("stock card %s line %s: recovery failed: %s", card["_id"], line["id"], e, exc_info=True)
    return recovered


async def _expand_apply_all(db, deadline: float | None) -> None:
    """Apply all presses that ran out of budget before queueing every card."""
    for run in await db[RUNS].find({"apply_all.pending": True}).to_list(length=None):
        _, finished = await _queue_run(db, run, run["apply_all"], deadline=deadline)
        if finished:
            await _finish_apply_all(db, run["_id"])


async def process_queued_lines(db, mongo_client, now_utc: datetime, *, deadline: float) -> dict:
    """Every minute: finish Apply all presses, follow Adds in the pipeline, recover stuck lines, then run
    what's queued. A queued line on a card closed meanwhile is refused by the same check every line goes
    through. Recovery gets half the time left, so stuck lines can't starve the queue. `now_utc` only ages
    stuck lines: each line run gets its own clock, so the pause cap counts rows logged earlier in this
    same pass."""
    from ai.agents.stock.add import sync_add_lines  # add builds on this module

    now = _naive_utc(now_utc)
    await _expand_apply_all(db, deadline)
    synced = await sync_add_lines(db, now, mongo_client, deadline=deadline)
    recovered = await _recover_stuck(db, mongo_client, now, time.monotonic() + max(0.0, deadline - time.monotonic()) / 2)
    processed = 0
    done = {"add_synced": synced, "recovered": recovered}
    cards = await db[CARDS].find({"lines.state": STATE_QUEUED}).sort("updated_at", 1).limit(QUEUE_BATCH).to_list(
        length=QUEUE_BATCH)
    for card in cards:
        for line in sorted(card.get("lines") or [], key=_work_order):
            if line.get("state") != STATE_QUEUED or line.get("kind") not in WRITABLE_KINDS:
                continue
            if _no_time_for_a_line(deadline):
                return {**done, "processed": processed}
            if await _run_line_safely(db, mongo_client, card["_id"], line["id"], datetime.utcnow()):
                processed += 1
    return {**done, "processed": processed}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def _undo_claimed(db, mongo_client, card: dict, line: dict, agent: dict, atype: str, now: datetime, sent: list) -> dict:
    block = await _guardrail_block(db, mongo_client, agent, card, line, atype, now, undo=True)
    if block is not None:
        await _undo_back(db, card, line, block["message"])
        return {"outcome": "blocked", "reason": block["message"]}
    ad_id = _ad_id(line)
    try:
        state = await get_object_state(card["user_id"], ad_id, "ad", mongo_client)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock card %s: read of %s failed: %s", card["_id"], ad_id, e)
        message = "Birdy couldn't read this ad in Meta, so nothing was changed. Try again."
        await _undo_back(db, card, line, message)
        return {"outcome": "failed", "reason": message}
    if _landed(atype, state):  # someone got there first
        await _finish_undone(db, card, agent, line, atype, now, note=ALREADY_DONE)
        return {"outcome": "undone", "reason": None}
    if state is None:
        await _undo_back(db, card, line, GONE)
        return {"outcome": "failed", "reason": GONE}
    if await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), undo_write_started_at=now) is None:
        await _undo_back(db, card, line, NOTHING_CHANGED)  # the marker isn't stored, so nothing may be sent
        return {"outcome": "failed", "reason": NOTHING_CHANGED}
    sent.append(True)
    landed, error = await _send_and_confirm(mongo_client, card, line, atype)
    if landed:
        await _finish_undone(db, card, agent, line, atype, now)
        return {"outcome": "undone", "reason": None}
    if landed is False:
        await _undo_back(db, card, line, error)
        return {"outcome": "failed", "reason": _short(error)}
    return {"outcome": "queued", "reason": None}  # recovery reads Meta


async def _undo_refusal(db, agent: dict | None, card: dict) -> str | None:
    """Undo stays open when the agent is off, so a person can reverse what it did after turning it off.
    It is refused only when another agent now holds the client."""
    if agent is None:
        return AGENT_OFF["message"]
    held_by = await claims.holder(db, card.get("user_id"), card.get("client_group_id"))
    return OTHER_AGENT if held_by not in (None, agent["_id"]) else None


async def _undo_write(db, mongo_client, card: dict, line_id: str, agent: dict | None, wrote: bool, decision: dict,
                      now: datetime) -> dict:
    card_id = card["_id"]
    claimed = await _set_line(db, card_id, line_id, expect=(STATE_APPLIED,), state=STATE_UNDOING, undoing_at=now,
                              undo_error=None, undo_write_started_at=None, undo_decision=decision, cut_at=None)
    if claimed is None:  # another press got it
        fresh = await db[CARDS].find_one({"_id": card_id})
        if (_find_line(fresh, line_id) or {}).get("state") == STATE_UNDONE:
            return _answer("already", fresh)
        return _answer("busy", fresh, ok=False, reason=BUSY)
    line = _find_line(claimed, line_id)
    if not wrote:
        await _finish_undone(db, claimed, agent, line, None, now, note=UNDO_NOT_WRITTEN)
        return _answer("undone", await db[CARDS].find_one({"_id": card_id}))
    sent: list = []
    try:
        res = await _undo_claimed(db, mongo_client, claimed, line, agent, _action_type(line["kind"], undo=True), now, sent)
    except Exception as e:  # noqa: BLE001
        logger.error("stock card %s line %s: undo failed: %s", card_id, line_id, e, exc_info=True)
        if sent:  # only a read of Meta can say what happened
            res = {"outcome": "queued", "reason": None}
        else:
            await _undo_back(db, claimed, line, NOTHING_CHANGED)
            res = {"outcome": "failed", "reason": NOTHING_CHANGED}
    fresh = await db[CARDS].find_one({"_id": card_id})
    return _answer(res["outcome"], fresh, ok=res["outcome"] in ("undone", "queued"), reason=res["reason"])


async def undo_line(db, mongo_client, user_id: str, card_id: str, line_id: str, *, by: str | None,
                    source: str | None) -> dict:
    """Reverse one applied line, within UNDO_AUTO_WINDOW of it applying. It only restores what Birdy
    changed, so it skips the pause cap, the live ad limit and taught exceptions, and still works with
    the agent off. It keeps the other checks. If Birdy wrote nothing for this line, it sends nothing, since the ad was
    changed by someone else."""
    card = await _visible_card(db, user_id, card_id)
    line = _find_line(card, line_id) if card else None
    if card is None or line is None or card.get("preview"):
        return _answer("not_found", None, ok=False)
    state = line.get("state")
    if state == STATE_UNDONE:
        return _answer("already", card)
    if state == STATE_UNDOING:
        return _answer("busy", card, ok=False, reason=BUSY)
    if line.get("kind") == "add" and state in (STATE_QUEUED, STATE_APPLYING):
        return _answer("busy", card, ok=False, reason=STILL_POSTING)
    if state != STATE_APPLIED or line.get("kind") not in WRITABLE_KINDS:
        return _answer("not_undoable", card, ok=False, reason="Only an applied line can be undone.")
    now = datetime.utcnow()
    until = line.get("undo_until") or (line.get("applied_at") or datetime.min) + UNDO_AUTO_WINDOW
    if now > until:
        return _answer("expired", card, ok=False, reason="This can only be undone for 24 hours.")
    wrote = bool(line.get("wrote"))
    agent = await _load_agent(db, card)
    decision = _decision(ACTOR_USER, by, source, now)
    if not wrote:
        return await _undo_write(db, mongo_client, card, line_id, agent, False, decision, now)
    refusal = await _undo_refusal(db, agent, card)
    if refusal:
        return _answer("blocked", card, ok=False, reason=refusal)
    key = _lease_key(card.get("agent_id"), card.get("client_group_id"))
    token = await _wait_for_lease(db, key)
    if token is None:
        return _answer("busy", card, ok=False, reason=BUSY)
    try:
        return await _undo_write(db, mongo_client, card, line_id, agent, True, decision, now)
    finally:
        await _release_lease(db, key, token)


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------

async def dismiss_card(db, user_id: str, card_id: str, *, by: str | None, source: str | None,
                       reason: str | None = None) -> dict:
    """Decline what's left on a card. Lines already applied stay applied and keep their Undo. Refused
    while a line is being applied, or a dismissed card would hide a write still going out."""
    card = await _visible_card(db, user_id, card_id)
    if card is None:
        return _answer("not_found", None, ok=False)
    if card.get("preview"):
        return _answer("refused", card, ok=False, reason="A preview card can't be dismissed.")
    now = datetime.utcnow()
    decision = _decision(ACTOR_USER, by, source, now)
    busy = []

    def change(fresh):
        busy.clear()
        if any(ln.get("state") in _IN_PROGRESS for ln in fresh.get("lines") or []):
            busy.append(True)
            return None
        return {"status": STATUS_DISMISSED, "dismiss_reason": reason, "decided_by": decision, "decided_at": now}

    done = await _change(db, card_id, change, statuses=(STATUS_OPEN, STATUS_FLAGGED, STATUS_PARTLY_APPLIED))
    fresh = await db[CARDS].find_one({"_id": card_id})
    if done is None:
        if busy:
            return _answer("busy", fresh, ok=False, reason=BUSY)
        status = fresh.get("status")
        if status == STATUS_DISMISSED:
            return _answer("already", fresh)
        return _answer("already", fresh, ok=False,
                       reason=ALREADY_APPLIED if status == STATUS_APPLIED else SUPERSEDED["message"])
    await log_activity(db, user_id, actor=ACTOR_USER, kind=KIND_SUGGESTION_DISMISSED,
                       title=f"Dismissed: {card.get('client_name') or 'a card'}", client_name=card.get("client_name"),
                       client_group_id=card.get("client_group_id"), ref_id=card_id, label=f"Declined by {_who(decision)}",
                       source=source, agent_id=card.get("agent_id"), note=reason)
    return _answer("dismissed", fresh)
