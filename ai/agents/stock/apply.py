"""
ai/agents/stock/apply.py
------------------------
Making a card's Remove and Bring back lines happen in Meta, and undoing them (spec section 7). This
changes real client ad accounts, so every step is stored before the next one starts.

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
                        Not landed: `failed`, saying so; a person can Retry.
                        Unreadable: stays `applying` and is read again each minute. After
                        CONFIRM_GIVE_UP it is `failed`, saying Birdy couldn't confirm it. A Retry reads
                        first, so that can't send twice either.

Recovery only touches lines older than STUCK_AFTER, longer than an invocation can live. A write
whose reply was an error is read back the same way, since a timeout can hide a write that landed.
`undoing` works the same way, using `undo_write_started_at`.

Refusals: statuses are an allow-list (`open`, plus `partly_applied`), `closing` and `superseded` are
never applied, and a card with a newer card for its client is refused. If a card closes after the
send has gone, the line still records what really happened, and its Undo still works.

Add lines are left as they are here. They reach Meta through the posting pipeline.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from ai.agents.guardrails import _naive_utc, agent_tz, check_adset_contents_live, check_guardrails, local_today
from ai.agents.meta_state import AD_STATE, record_status_write
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.stock.cards import (STATE_APPLIED, STATE_APPLYING, STATE_BLOCKED, STATE_FAILED, STATE_FLAGGED,
                                   STATE_NEEDS_FILE, STATE_PROPOSED, STATE_QUEUED, STATE_UNDOING, STATE_UNDONE,
                                   STATUS_APPLIED, STATUS_CLOSING, STATUS_DISMISSED, STATUS_FLAGGED, STATUS_OPEN,
                                   STATUS_PARTLY_APPLIED, STATUS_SUPERSEDED, guarded_update)
from ai.agents.stock.checks import in_scope
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.store import AGENTS, CARDS, RUNS, list_memory
from ai.suggestions.actions import _is_desired, execute_action
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from ai.suggestions.store import (ACTIVITY, ACTOR_BIRDY, ACTOR_USER, KIND_ACTION_APPLIED, KIND_ACTION_UNDONE,
                                  KIND_SUGGESTION_DISMISSED, log_activity)
from services.meta_status_service import get_object_state

logger = logging.getLogger(__name__)

STUCK_AFTER = timedelta(minutes=6)     # an invocation lives 300 s at most, so nothing alive is this old
CONFIRM_GIVE_UP = timedelta(hours=2)   # how long a sent write can wait for Meta to be readable
MAX_RECOVERIES = 2                     # kills before the send that a line survives
QUEUE_BATCH = 200                      # cards looked at per cron pass; the rest wait a minute
ERROR_MAX = 300
_RACE_TRIES = 8

WRITABLE_KINDS = ("remove", "revive")
_ACTIONABLE = (STATUS_OPEN, STATUS_PARTLY_APPLIED)
_LIVE = (STATUS_OPEN, STATUS_FLAGGED, STATUS_PARTLY_APPLIED, STATUS_APPLIED, STATUS_SUPERSEDED, STATUS_DISMISSED)
_RETRYABLE = (STATE_PROPOSED, STATE_FAILED, STATE_BLOCKED)    # only when a person names the line
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
    return 1 if line.get("kind") == "revive" else 0


async def _queue_lines(db, card_id: str, wanted, decision: dict) -> list[tuple[str, str]]:
    """The atomic claim: wanted lines move to `queued` in one write. Returns what this call queued,
    in working order. Empty when another caller has them or the card can't be applied."""
    queued: list[dict] = []

    def change(card):
        queued.clear()
        lines = []
        for ln in card.get("lines") or []:
            if wanted(ln):
                ln = {**ln, "state": STATE_QUEUED, "error": None, "reason_code": None, "decision": decision}
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
    """The shape check_guardrails and check_adset_contents_live read. They are given the stored
    agent, so its `_id` turns on the not_acting_agent check."""
    return {"action": {"type": atype, "targets": [{"object_id": _ad_id(line), "object_type": "ad",
                                                   "name": line.get("ad_name")}]},
            "client_group_id": card.get("client_group_id"), "client_name": card.get("client_name"),
            "user_id": card.get("user_id"), "agent_id": card.get("agent_id"), "title": line.get("ad_name")}


def _ad_id(line: dict) -> str | None:
    ids = line.get("meta_ids") or []
    return str(ids[0]) if ids else None


async def _over_live_limit(db, agent: dict, card: dict, ad_id: str) -> dict | None:
    """Live ads never go over max_ads_live (spec section 6), however long ago the card was written."""
    limit = _settings(agent)["creative"]["max_ads_live"]
    gid = card.get("client_group_id")
    group = await db["client_groups"].find_one({"id": gid, "user_id": card.get("user_id")}) or {}
    rows = await db[AD_STATE].find({"client_group_id": gid, "effective_status": "ACTIVE"}).to_list(length=None)
    live = {r["ad_id"] for r in rows if in_scope(r.get("campaign_id"), group)}
    if ad_id in live or len(live) < limit:
        return None
    return {"code": "max_ads_live",
            "message": f"Turning this on would make {len(live) + 1} ads live, over the limit of {limit}."}


async def _guardrail_block(db, mongo_client, agent: dict, card: dict, line: dict, atype: str,
                           now: datetime) -> dict | None:
    doc = _target_doc(card, line, atype)
    g = await check_guardrails(db, agent, doc, now_utc=now)
    if g.ok:
        memories = await list_memory(db, agent["_id"], active_only=True)
        g = await check_adset_contents_live(db, mongo_client, agent, doc, memories=memories, now_utc=now)
    if not g.ok:
        return {"code": g.reason, "message": g.message}
    return await _over_live_limit(db, agent, card, _ad_id(line)) if atype == ACTION_ENABLE_ADS else None


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

async def _send_and_confirm(mongo_client, card: dict, line: dict, atype: str) -> tuple[bool | None, str | None]:
    """(landed, Meta's error). A reply that isn't a success doesn't prove the write failed, so it is
    read back. landed is None when Meta can't say: only a later read can."""
    user_id, ad_id = card["user_id"], _ad_id(line)
    target = {"object_id": ad_id, "object_type": "ad", "name": line.get("ad_name")}
    error = None
    try:
        res = await execute_action(mongo_client, user_id, {"type": atype, "targets": [target]})
        if res.get("succeeded"):
            return True, None
        error = ((res.get("failed") or [{}])[0]).get("error") or "Meta didn't accept this."
    except Exception as e:  # noqa: BLE001 - same as a lost reply: read what really happened
        logger.error("stock card %s: send for %s raised: %s", card["_id"], ad_id, e, exc_info=True)
        error = "Meta didn't accept this."
    try:
        state = await get_object_state(user_id, ad_id, "ad", mongo_client)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock card %s: read-back of %s failed: %s", card["_id"], ad_id, e)
        return None, error
    return _landed(atype, state), error


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
                          wrote: bool) -> None:
    decision = line.get("decision") or {}
    note = None if wrote else ALREADY_DONE
    await _mirror(db, card, agent, line, atype, now)
    verb = "Paused" if atype == ACTION_PAUSE_ADS else "Turned on"
    label = "Auto-run by Birdy" if decision.get("actor") == ACTOR_BIRDY else f"Approved by {_who(decision)}"
    await _log_once(db, card, line, KIND_ACTION_APPLIED, label=label, title=f"{verb} {line.get('ad_name') or 'an ad'}",
                    decision=decision, action_type=atype if wrote else None, note=note)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_APPLIED, applied_at=now,
                    undo_until=now + UNDO_AUTO_WINDOW, wrote=wrote, note=note, error=None, reason_code=None)


async def _block(db, card: dict, line: dict, reason: dict) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_BLOCKED,
                    error=reason["message"], reason_code=reason["code"])


async def _fail(db, card: dict, line: dict, error: str, **extra) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_FAILED,
                    error=_short(error), reason_code=None, **extra)


async def _apply_claimed(db, mongo_client, card: dict, line: dict, now: datetime, sent: list) -> None:
    agent = await _load_agent(db, card)
    refusal = await _refusal(db, agent, card)
    if refusal is None and not _ad_id(line):
        refusal = {"code": "no_ad", "message": "This line doesn't name an ad, so nothing was changed."}
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
        return await _finish_applied(db, card, agent, line, atype, now, wrote=bool(line.get("write_started_at")))

    # The last check: this write is refused if the card closed or changed status, and nothing is
    # sent until it's stored that something may have been.
    started = await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), statuses=_ACTIONABLE,
                              write_started_at=now)
    if started is None:
        fresh = await db[CARDS].find_one({"_id": card["_id"]})
        reason = await _refusal(db, agent, fresh)
        if reason is not None:
            return await _block(db, card, line, reason)
        await _set_line(db, card["_id"], line["id"], expect=(STATE_APPLYING,), state=STATE_QUEUED)
        return None
    sent.append(True)
    landed, error = await _send_and_confirm(mongo_client, card, line, atype)
    if landed:
        await _finish_applied(db, card, agent, line, atype, now, wrote=True)
    elif landed is False:
        await _fail(db, card, line, error, write_started_at=None)
    # landed is None: left `applying` with write_started_at, for recovery to read.


async def _run_line(db, mongo_client, card_id: str, line_id: str, now: datetime) -> None:
    """Take one queued line and see it through. A raise here stays with this line."""
    card = await _set_line(db, card_id, line_id, expect=(STATE_QUEUED,), state=STATE_APPLYING, applying_at=now)
    if card is None:
        return
    line = _find_line(card, line_id)
    sent: list = []
    try:
        await _apply_claimed(db, mongo_client, card, line, now, sent)
    except asyncio.CancelledError:  # the request's budget ran out. Nothing sent, so it goes back
        if not sent:
            await _set_line(db, card_id, line_id, expect=(STATE_APPLYING,), state=STATE_QUEUED)
        raise
    except Exception as e:  # noqa: BLE001
        logger.error("stock card %s line %s failed: %s", card_id, line_id, e, exc_info=True)
        if not sent:
            await _fail(db, card, line, NOTHING_CHANGED)
        # Once sent, it stays `applying`: only a read of Meta can say what happened.


async def _run_line_safely(db, mongo_client, card_id: str, line_id: str, now: datetime) -> None:
    try:
        await _run_line(db, mongo_client, card_id, line_id, now)
    except Exception as e:  # noqa: BLE001 - the store itself failed. The cron picks the line up again
        logger.error("stock card %s line %s: couldn't save its state: %s", card_id, line_id, e, exc_info=True)


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


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
    if all(s == STATE_APPLIED for s in states):
        return _answer("applied", card)
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
    it if it failed or was blocked. Never writes a Flag line, or an Add. `budget_s` limits the wait,
    and what doesn't fit is finished by the cron."""
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
    """Apply all. Called from Slack, so it answers inside `budget_s`. The press is stored first, so
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
    await _drain(db, mongo_client, pairs, budget_s=deadline - time.monotonic())

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
              "failed": states.get(STATE_FAILED, 0),
              "queued": sum(states.get(s, 0) for s in (STATE_QUEUED, STATE_APPLYING))}
    if counts["queued"] or not finished:
        outcome = "queued"
    elif not pairs:
        outcome = "noop"
    else:
        outcome = "applied" if counts["applied"] else "failed" if counts["failed"] else "blocked"
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


async def _queue_auto(db, card: dict, client: dict, run: dict, add_limit: int, decision: dict) -> list[tuple[str, str]]:
    """In one write: send fall-backs to Ask me, for good, and queue what may run. A fall-back stays
    even when its reason passes, because a person may already be looking at an Apply button."""
    queued: list[dict] = []

    def change(fresh):
        queued.clear()
        adds, lines, changed = 0, [], False
        for ln in fresh.get("lines") or []:
            auto = ln.get("mode") == "auto"
            if auto and ln.get("kind") == "add":
                adds += 1
            if auto and ln.get("state") in (STATE_PROPOSED, STATE_NEEDS_FILE):
                why = _fallback(fresh, client, run, ln, adds, add_limit)
                if why:
                    ln, changed = {**ln, "mode": "ask", "auto_fallback": why}, True
                elif ln.get("kind") in WRITABLE_KINDS and ln.get("state") == STATE_PROPOSED:
                    ln, changed = {**ln, "state": STATE_QUEUED, "error": None, "decision": decision}, True
                    queued.append(ln)
            lines.append(ln)
        if not changed:
            return None
        fields = {"lines": lines}
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
    add_limit = _settings(agent)["creative"]["auto_max_adds"]
    now = datetime.utcnow()
    decision = _decision(ACTOR_BIRDY, "Birdy", "auto", now)
    cards = await db[CARDS].find({"run_id": run["_id"], "status": {"$in": list(_ACTIONABLE)},
                                  "preview": {"$ne": True}}).to_list(length=None)
    pairs: list[tuple[str, str]] = []
    for card in cards:
        client = (run.get("clients") or {}).get(card.get("client_group_id")) or {}
        if client.get("health_due") or await has_newer_card(db, card):
            continue
        pairs += await _queue_auto(db, card, client, run, add_limit, decision)
    await _drain(db, mongo_client, pairs, deadline=deadline)


# ---------------------------------------------------------------------------
# The cron: finishing and recovering
# ---------------------------------------------------------------------------

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
        state = await get_object_state(card["user_id"], _ad_id(line), "ad", mongo_client)
    except Exception as e:  # noqa: BLE001 - keep asking, and don't guess
        logger.warning("stock card %s line %s: can't confirm yet: %s", card["_id"], line["id"], e)
        if now - (line.get("write_started_at") or now) > CONFIRM_GIVE_UP:
            await _fail(db, card, line, COULD_NOT_CONFIRM)
        return None
    if _landed(atype, state):
        return await _finish_applied(db, card, await _load_agent(db, card), line, atype, now, wrote=True)
    return await _fail(db, card, line, _not_landed(state), write_started_at=None)


async def _undo_back(db, card: dict, line: dict, error: str) -> None:
    await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), state=STATE_APPLIED, undo_error=_short(error),
                    undo_write_started_at=None)


async def _finish_undone(db, card: dict, agent: dict | None, line: dict, atype: str, now: datetime, *,
                         note: str | None = None) -> None:
    decision = line.get("undo_decision") or {}
    if atype:
        await _mirror(db, card, agent, line, atype, now)
    await _log_once(db, card, line, KIND_ACTION_UNDONE, label=f"Undone by {_who(decision)}",
                    title=f"Undid: {line.get('ad_name') or 'an ad'}", decision=decision, note=note)
    await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), state=STATE_UNDONE, undone_at=now,
                    undo_error=None, note=note)


async def _recover_undoing(db, mongo_client, card: dict, line: dict, now: datetime) -> None:
    if not line.get("undo_write_started_at"):
        return await _undo_back(db, card, line, NOTHING_CHANGED)
    atype = _action_type(line["kind"], undo=True)
    try:
        state = await get_object_state(card["user_id"], _ad_id(line), "ad", mongo_client)
    except Exception as e:  # noqa: BLE001
        logger.warning("stock card %s line %s: can't confirm the undo yet: %s", card["_id"], line["id"], e)
        if now - line["undo_write_started_at"] > CONFIRM_GIVE_UP:
            await _undo_back(db, card, line, COULD_NOT_CONFIRM)
        return None
    if _landed(atype, state):
        return await _finish_undone(db, card, await _load_agent(db, card), line, atype, now)
    return await _undo_back(db, card, line, _not_landed(state))


async def _recover_stuck(db, mongo_client, now: datetime, deadline: float | None) -> int:
    cards = await db[CARDS].find({"lines.state": {"$in": [STATE_APPLYING, STATE_UNDOING]}}).limit(QUEUE_BATCH).to_list(
        length=QUEUE_BATCH)
    recovered = 0
    for card in cards:
        for line in card.get("lines") or []:
            if _out_of_time(deadline):
                return recovered
            state = line.get("state")
            since = line.get("applying_at") if state == STATE_APPLYING else line.get("undoing_at")
            if (state not in (STATE_APPLYING, STATE_UNDOING) or line.get("kind") not in WRITABLE_KINDS
                    or (since and now - since < STUCK_AFTER)):
                continue
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
    """Every minute: finish Apply all presses, recover stuck lines, then run what's queued. A queued
    line on a card closed meanwhile is refused by the same check every line goes through."""
    now = _naive_utc(now_utc)
    await _expand_apply_all(db, deadline)
    recovered = await _recover_stuck(db, mongo_client, now, deadline)
    processed = 0
    cards = await db[CARDS].find({"lines.state": STATE_QUEUED}).sort("updated_at", 1).limit(QUEUE_BATCH).to_list(
        length=QUEUE_BATCH)
    for card in cards:
        for line in sorted(card.get("lines") or [], key=_work_order):
            if line.get("state") != STATE_QUEUED or line.get("kind") not in WRITABLE_KINDS:
                continue
            if _out_of_time(deadline):
                return {"recovered": recovered, "processed": processed}
            await _run_line_safely(db, mongo_client, card["_id"], line["id"], now)
            processed += 1
    return {"recovered": recovered, "processed": processed}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

async def _undo_claimed(db, mongo_client, card: dict, line: dict, agent: dict, atype: str, now: datetime, sent: list) -> dict:
    block = await _guardrail_block(db, mongo_client, agent, card, line, atype, now)
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
    await _set_line(db, card["_id"], line["id"], expect=(STATE_UNDOING,), undo_write_started_at=now)
    sent.append(True)
    landed, error = await _send_and_confirm(mongo_client, card, line, atype)
    if landed:
        await _finish_undone(db, card, agent, line, atype, now)
        return {"outcome": "undone", "reason": None}
    if landed is False:
        await _undo_back(db, card, line, error)
        return {"outcome": "failed", "reason": _short(error)}
    return {"outcome": "queued", "reason": None}  # recovery reads Meta


async def undo_line(db, mongo_client, user_id: str, card_id: str, line_id: str, *, by: str | None,
                    source: str | None) -> dict:
    """Reverse one applied line, within UNDO_AUTO_WINDOW of it applying. A write like any other, so
    the guardrails run: Undo is refused once the client has moved or the agent is off. If Birdy
    wrote nothing for this line, it sends nothing, since the ad was changed by someone else."""
    card = await _visible_card(db, user_id, card_id)
    line = _find_line(card, line_id) if card else None
    if card is None or line is None or card.get("preview"):
        return _answer("not_found", None, ok=False)
    state = line.get("state")
    if state == STATE_UNDONE:
        return _answer("already", card)
    if state == STATE_UNDOING:
        return _answer("busy", card, ok=False, reason=BUSY)
    if state != STATE_APPLIED or line.get("kind") not in WRITABLE_KINDS:
        return _answer("not_undoable", card, ok=False, reason="Only an applied line can be undone.")
    now = datetime.utcnow()
    until = line.get("undo_until") or (line.get("applied_at") or datetime.min) + UNDO_AUTO_WINDOW
    if now > until:
        return _answer("expired", card, ok=False, reason="This can only be undone for 24 hours.")
    wrote = bool(line.get("wrote"))
    agent = await _load_agent(db, card)
    if wrote and not _is_on(agent):
        return _answer("blocked", card, ok=False, reason=AGENT_OFF["message"])

    claimed = await _set_line(db, card_id, line_id, expect=(STATE_APPLIED,), state=STATE_UNDOING, undoing_at=now,
                              undo_error=None, undo_write_started_at=None,
                              undo_decision=_decision(ACTOR_USER, by, source, now))
    if claimed is None:  # another press got it
        return _answer("busy", await db[CARDS].find_one({"_id": card_id}), ok=False, reason=BUSY)
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
