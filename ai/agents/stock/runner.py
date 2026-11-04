"""
ai/agents/stock/runner.py
-------------------------
The stock agent's daily run as a stored state machine (spec section 6). A run can't finish in one
Vercel invocation, so each tick claims it, does what fits before the deadline, saves after every
step and releases it.

Phases: alerts, catch_up, checks, writing, publishing, waiting, done. From `waiting` a run loops
back for any client that has become ready, and publishes again for what changed since, so
`publish_run` and `auto_apply_run` must be safe to repeat and act only on what's new.

A client's `state` says what is left for it, so a resumed run never repeats finished work:
  pending    needs its ad state refreshed and its health read (markers `refreshed`, `health_done`)
  healthy    needs its spend alerts
  waiting    `wait_for`: "history" (its ad rows are late, top-up requested) or "refresh" (retry due)
  alerted    needs its checks
  needs_writing   needs its AI card
  done, alerts_only, on_hold, no_zones, no_data, data_late, failed    finished

Alerts never wait on spend data. The token is checked first, then every client's health and prepaid
alerts are read. A late client then waits, alone, for up to GRACE, and the others get their spend
alerts. `post_urgent_alerts` is called at the end of every alerts pass, however it stopped, and when
a run ends incomplete, with the alerts so far. It's never called for a preview.

Failures:
- A client's error is retried on later ticks; `failed` only comes after CLIENT_ATTEMPTS.
- A refresh that keeps failing doesn't fail the client: it carries on with `stale`.
- A health read that fails is retried on later ticks (`health_due`), up to CLIENT_ATTEMPTS, without
  holding the client's card. Its `check_failed` only appears when the tries run out or the run ends,
  and until then the summary lists the client in `health_unread`.
- A Meta rate limit costs nobody an attempt. One on an ad account makes that account's clients wait
  (`retry_after`) while other accounts carry on. One on the app or user stops this agent's tick.
- A claim older than STALE_CLAIM belongs to a killed invocation. The kill counts against the
  clients named in `in_flight`, so one slow client fails alone. A kill with nobody in flight, or
  a phase that raises, counts in `run.attempts`, which progress resets; at MAX_RUN_ATTEMPTS the
  run ends and reports what's missing.

A card is stored as `closing`, then older open cards are superseded, then it opens. So there are
never two open cards for a client, whatever fails between those steps, and a stored card is never
paid for twice.
"""

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.agents import claims
from ai.agents import history as ad_history
from ai.agents.meta_state import (SCOPE_ACCOUNT, MetaRateLimit, MetaTokenError, backfill_paused_at, read_meta_state,
                                  store_meta_state)
from ai.agents.snapshot import load_ad_days, load_state
from ai.agents.stock.alerts import ACCOUNT_TZ_FIELD, _alert, _history_state, run_alerts
from ai.agents.stock.apply import auto_apply_run
from ai.agents.stock.candidates import build_candidates
from ai.agents.stock.cards import STATUS_CLOSING, build_card, supersede_open
from ai.agents.stock import lessons, scoreboard
from ai.agents.stock.checks import client_facts, follow_ups
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.stock.windows import full_day_window, last_full_day
from ai.agents.stock.writer import write_card
from ai.agents.store import CARDS, RUNS
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

ALERTS, CATCH_UP, CHECKS, WRITING, PUBLISHING, WAITING_PHASE, DONE = (
    "alerts", "catch_up", "checks", "writing", "publishing", "waiting", "done")
BATCH, ALERT_ONLY, PREVIEW = "batch", "alert_only", "preview"
PENDING, HEALTHY, WAITING, ALERTED, NEEDS_WRITING, FINISHED, FAILED = (
    "pending", "healthy", "waiting", "alerted", "needs_writing", "done", "failed")
ALERTS_ONLY, ON_HOLD, NO_ZONES, NO_DATA, DATA_LATE = "alerts_only", "on_hold", "no_zones", "no_data", "data_late"
UNFINISHED = (PENDING, HEALTHY, WAITING, ALERTED, NEEDS_WRITING)
WAIT_HISTORY, WAIT_REFRESH = "history", "refresh"

GRACE = timedelta(hours=2)           # how long one late client waits for its ad rows
OVERDUE_AFTER = timedelta(hours=24)  # from the scheduled time, when the next day's run is due
PREVIEW_MAX_AGE = timedelta(hours=3)  # a preview still open after this ends, so a new request starts fresh
CLIENT_ATTEMPTS = 3
MAX_RUN_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=10)  # so a retry lands on a later tick
STALE_CLAIM = timedelta(minutes=10)  # an invocation lives 300 s at most, and every save renews the claim
DEADLINE_MARGIN_S = 20               # a read may run this long past the deadline
CLIENT_READ_TIMEOUT_S = 100          # started just before the 180 s budget, this still ends inside the 300 s limit
ERROR_MAX = 300
RECONNECT = "Meta needs reconnecting. Reconnect to check this client."
OUT_OF_TIME = "Birdy ran out of time before finishing this client."
RATE_LIMITED = "Meta's rate limit stopped Birdy finishing this client today."


# ---------------------------------------------------------------------------
# Replaced by later tasks
# ---------------------------------------------------------------------------

async def post_urgent_alerts(db, run: dict, agent: dict) -> None:
    """Post what can't wait for the report: disabled accounts, payment, rejected ads. Task 15."""


CATCH_UP_MAX_S = 60   # when the tick gave no deadline; the phase never holds a run up for longer


async def catch_up_run(db, mongo_client, run: dict, agent: dict) -> None:
    """Batch days: match flagged Adds a person made by hand, score due ads, look for patterns worth a rule.
    (Edits and dismiss reasons become lessons when they happen, not here.) It stops at the tick's deadline,
    each step is on its own so one failing leaves the others, and nothing here touches a client's run."""
    deadline = run.get("tick_deadline")
    if deadline is None:
        deadline = time.monotonic() + CATCH_UP_MAX_S
    now = _run_now(run)
    for name, step in (("match flagged adds", lambda: scoreboard.record_flagged_adds(db, agent, now, deadline=deadline)),
                       ("score due ads", lambda: lessons.score_due(db, agent, now, deadline=deadline)),
                       ("scan patterns", lambda: lessons.scan_patterns(db, agent, now, deadline=deadline))):
        if _out_of_time(deadline):
            return
        try:
            await step()
        except Exception as e:  # noqa: BLE001 - the others still run, and the phase never fails the day
            logger.error("stock run %s: catch-up step %r failed: %s", run["_id"], name, e, exc_info=True)


async def publish_run(db, run: dict, agent: dict, cards: list[dict]) -> None:
    """Post the summary and one reply per card to Slack. Called again when more cards arrive."""


# ---------------------------------------------------------------------------
# Creating runs
# ---------------------------------------------------------------------------

def _settings(agent: dict) -> dict:
    return merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})


def _batches_on(settings: dict, local_date: date) -> list[str]:
    return [b["id"] for b in settings.get("batches") or [] if local_date.weekday() in (b.get("weekdays") or [])]


def run_kind(settings: dict, local_date: date) -> tuple[str, str | None]:
    """(batch, its id) on a batch's weekday, else (alert_only, None). Batches may share a weekday:
    the first one names the run, and the run still checks the clients of all of them."""
    ids = _batches_on(settings, local_date)
    return (BATCH, ids[0]) if ids else (ALERT_ONLY, None)


def _local_date(agent: dict, now_utc: datetime) -> date:
    tz = ((agent.get("scope") or {}).get("schedule") or {}).get("tz") or "Europe/London"
    return now_utc.astimezone(ZoneInfo(tz)).date()


def _run_now(run: dict) -> datetime:
    """The instant the run began. Every window comes from it, so ticks can't shift the dates."""
    return run["as_of"].replace(tzinfo=timezone.utc)


def _agency_day(run: dict) -> date:
    return date.fromisoformat(run["local_date"])


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _new_run(agent: dict, now_utc: datetime, kind: str | None, scheduled_utc: datetime | None) -> dict:
    settings = _settings(agent)
    today = _local_date(agent, now_utc)
    derived, batch_id = run_kind(settings, today)
    kind = kind or derived
    batch_ids = _batches_on(settings, today) if kind == BATCH else []
    clients = {}
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        cfg = settings["clients"].get(gid) or {}
        carded = kind == PREVIEW or cfg.get("batch_id") in batch_ids
        clients[gid] = {"state": ON_HOLD if cfg.get("on_hold") else PENDING, "error": None, "carded": carded,
                        "attempts": 0, "stale": False}
    as_of = _naive(now_utc)
    return {"_id": str(uuid.uuid4()), "agent_id": agent["_id"], "user_id": agent["user_id"],
            "local_date": today.isoformat(), "kind": kind, "batch_id": batch_id if kind == BATCH else None,
            "phase": ALERTS, "clients": clients, "alerts": [], "summary": {},
            "slack": {"channel_id": None, "ts": None}, "claimed_at": None, "attempts": 0, "in_flight": [],
            "reconnect": False, "as_of": as_of, "scheduled_at": _naive(scheduled_utc) if scheduled_utc else as_of,
            "created_at": datetime.utcnow(),
            **({"open_preview": True} if kind == PREVIEW else {"scheduled": True})}


async def find_run(db, agent_id: str, local_date: str) -> dict | None:
    return await db[RUNS].find_one({"agent_id": agent_id, "local_date": local_date, "scheduled": True})


async def find_open_run(db, agent_id: str) -> dict | None:
    """The agent's oldest unfinished scheduled run. It may have started before local midnight."""
    rows = await (db[RUNS].find({"agent_id": agent_id, "scheduled": True, "phase": {"$ne": DONE}})
                  .sort("as_of", 1).limit(1).to_list(length=1))
    return rows[0] if rows else None


async def get_or_create_run(db, agent: dict, now_utc: datetime, *, kind: str | None = None,
                            scheduled_utc: datetime | None = None) -> dict:
    """The day's scheduled run, unique on (agent_id, local_date). A preview doesn't take that slot:
    an unfinished preview is returned, and otherwise a new one starts."""
    fresh = _new_run(agent, now_utc, kind, scheduled_utc)
    if kind == PREVIEW:
        await end_stale_previews(db, agent, now_utc)
        key = {"agent_id": agent["_id"], "kind": PREVIEW, "phase": {"$ne": DONE}}
        if open_preview := await db[RUNS].find_one(key):
            return open_preview
        try:
            await db[RUNS].insert_one(fresh)  # the unique open_preview index lets one concurrent insert win
        except Exception as e:
            if "duplicate key" not in str(e).lower():
                raise
            return await db[RUNS].find_one(key) or fresh
        return fresh
    key = {"agent_id": agent["_id"], "local_date": fresh["local_date"], "scheduled": True}
    try:
        await db[RUNS].update_one(key, {"$setOnInsert": {k: v for k, v in fresh.items() if k not in key}}, upsert=True)
    except Exception as e:
        if "duplicate key" not in str(e).lower():
            raise  # otherwise a concurrent tick created it first
    return await db[RUNS].find_one(key)


# ---------------------------------------------------------------------------
# Claims and saving
# ---------------------------------------------------------------------------

def _read_timeout(deadline: float | None) -> float:
    """A Meta read or store step gets CLIENT_READ_TIMEOUT_S, or less near the deadline, so the
    invocation ends shortly after it whatever budget the caller chose."""
    if deadline is None:
        return CLIENT_READ_TIMEOUT_S
    return max(1.0, min(CLIENT_READ_TIMEOUT_S, deadline - time.monotonic() + DEADLINE_MARGIN_S))


def _out_of_time(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


async def _claim(db, run_id: str) -> dict | None:
    """Claim a run that isn't done. None when another tick holds it."""
    now = datetime.utcnow()
    before = await db[RUNS].find_one_and_update(
        {"_id": run_id, "phase": {"$ne": DONE},
         "$or": [{"claimed_at": None}, {"claimed_at": {"$lt": now - STALE_CLAIM}}]},
        {"$set": {"claimed_at": now}})
    if before is None:
        return None
    return {**before, "claimed_at": now, "taken_over": before.get("claimed_at") is not None}


async def _save(db, run: dict, **fields) -> None:
    """Set top-level run fields, in memory and stored, and renew the claim."""
    if "claimed_at" not in fields:
        fields["claimed_at"] = datetime.utcnow()
    if fields.get("phase") == DONE:
        fields["open_preview"] = False  # frees the agent's one open-preview slot
    run.update(fields)
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": fields})


async def _save_client(db, run: dict, gid: str, **fields) -> None:
    """Set a client's fields and renew the claim. Moving to a new state is progress, unless the
    caller says how many attempts it has used: the client's and the run's counts start again."""
    now = datetime.utcnow()
    top = {"claimed_at": now}
    if "state" in fields and "attempts" not in fields:
        fields = {"attempts": 0, "retry_after": None, "error": None, **fields}
        top["attempts"] = 0
    run["clients"][gid].update(fields)
    run.update(top)
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        **top, **{f"clients.{gid}.{k}": v for k, v in fields.items()}}})


def _short(e: Exception) -> str:
    return (str(e) or type(e).__name__)[:ERROR_MAX]


def _now(run: dict) -> datetime:
    return run["tick_now"]


def _due(client: dict, run: dict) -> bool:
    return not client.get("retry_after") or client["retry_after"] <= _now(run)


def _clients_in(run: dict, *states) -> list[str]:
    return [g for g, c in run["clients"].items() if c["state"] in states and _due(c, run)]


async def _give_up(db, run: dict, gid: str, error: str, *, attempts: int | None = None) -> bool:
    """Every way a client stops short ends here. A stored card is paid for, so it gets one more try
    at opening first: True when it did. Otherwise the client is failed, with the reason."""
    try:
        if await db[CARDS].find_one({"run_id": run["_id"], "client_group_id": gid}, {"_id": 1}):
            await _close_with_card(db, run, gid)
            return True
    except Exception as e:  # noqa: BLE001 - the card stays closing: not applicable, older one still open
        logger.error("stock run %s: could not open the card for %s: %s", run["_id"], gid, e, exc_info=True)
    await _save_client(db, run, gid, state=FAILED, error=error, facts=None, draft=None,
                       **({} if attempts is None else {"attempts": attempts}))
    return False


async def _retry_client(db, run: dict, gid: str, e: Exception) -> None:
    """A client's step failed: try again on a later tick, and only give up after CLIENT_ATTEMPTS."""
    logger.warning("stock run %s: client %s failed: %s", run["_id"], gid, e, exc_info=True)
    n = (run["clients"][gid].get("attempts") or 0) + 1
    if n >= CLIENT_ATTEMPTS:
        await _give_up(db, run, gid, _short(e), attempts=n)
    else:
        await _save_client(db, run, gid, error=_short(e), attempts=n, retry_after=_now(run) + RETRY_AFTER)


def _alert_key(a: dict) -> tuple:
    """One alert per client and code. check_failed carries different problems under one code, so
    its detail is part of the key."""
    return a.get("client_group_id"), a.get("code"), a.get("detail") if a.get("code") == "check_failed" else None


async def _add_alerts(db, run: dict, new: list[dict]) -> None:
    merged = {_alert_key(a): a for a in run.get("alerts") or []}
    merged.update({_alert_key(a): a for a in new})
    await _save(db, run, alerts=list(merged.values()))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

async def _needs_reconnect(db, run: dict, detail: str) -> None:
    """The token is dead for every client of this user: record it and stop reading."""
    for gid, c in run["clients"].items():
        if c["state"] in UNFINISHED:
            await _give_up(db, run, gid, RECONNECT)
        if c.get("health_due"):
            await _save_client(db, run, gid, health_due=None)
    await _add_alerts(db, run, [_alert(None, "meta_reconnect", "critical", "Meta needs reconnecting", detail,
                                       run["local_date"])])
    await _save(db, run, reconnect=True)


def _token_for(mongo_client):
    async def token_for(user_id):
        return ((await get_facebook_token(user_id, mongo_client)) or {}).get("access_token")
    return token_for


async def _refresh_failed(db, run: dict, gid: str, group: dict, e: Exception) -> None:
    """A batch client gets more tries while the others go on. After that, or when nothing is
    waiting on a card, it carries on with what's stored and its card says so."""
    logger.warning("stock run %s: state refresh failed for %s: %s", run["_id"], gid, e)
    client = run["clients"][gid]
    n = (client.get("attempts") or 0) + 1
    if run["kind"] == BATCH and client["carded"] and n < CLIENT_ATTEMPTS:
        await _save_client(db, run, gid, attempts=n, error=_short(e), wait_for=WAIT_REFRESH,
                           retry_after=_now(run) + RETRY_AFTER)
        return
    await _save_client(db, run, gid, attempts=n, error=_short(e), refreshed=True, stale=True, wait_for=None)
    name = group.get("name") or gid
    await _add_alerts(db, run, [_alert(gid, "check_failed", "warning", f"Couldn't refresh {name}",
                                       f"Ad statuses could not be read from Meta, so stored ones were used: {_short(e)}",
                                       run["local_date"])])


def _health_unread(client: dict, run: dict) -> bool:
    return not client.get("health_done") and (not client.get("health_due") or client["health_due"] <= _now(run))


def _health_settled(client: dict) -> bool:
    """Read, given up on, or with a retry booked: either way the client's card doesn't wait for it."""
    return bool(client.get("health_done") or client.get("health_due"))


def _health_due_now(run: dict) -> list[str]:
    """Clients past the read stage with a health retry due. A pending client's comes with its read."""
    return [g for g, c in run["clients"].items()
            if c["state"] != PENDING and c.get("health_due") and c["health_due"] <= _now(run)]


def _limit_wait(limit: MetaRateLimit) -> timedelta:
    return timedelta(seconds=limit.retry_after_s) if limit.retry_after_s else RETRY_AFTER


async def _health_failed(db, run: dict, gid: str, alert: dict) -> None:
    """A read failed. Book a retry, with no hold on the client's card, and only report it when the
    tries run out, so a blip leaves no false alert. A preview can't wait for later ticks."""
    n = (run["clients"][gid].get("health_attempts") or 0) + 1
    alert = {k: v for k, v in alert.items() if k != "read_failed"}
    if run["kind"] != PREVIEW and n < CLIENT_ATTEMPTS:
        await _save_client(db, run, gid, health_attempts=n, health_failure=alert, health_due=_now(run) + RETRY_AFTER)
    else:
        await _add_alerts(db, run, [alert])
        await _save_client(db, run, gid, health_attempts=n, health_failure=None, health_due=None, health_done=True)


async def _read_health(db, mongo_client, run: dict, agent: dict, gids: list[str]) -> MetaRateLimit | None:
    """One account's health and prepaid alerts. Returns its rate limit, if it has one. A dead token
    or an app-wide limit goes to the caller."""
    try:
        alerts = await run_alerts(db, agent, _token_for(mongo_client), _run_now(run), gids, parts="health",
                                  today=run["local_date"], reraise=(MetaTokenError, MetaRateLimit))
    except MetaTokenError:
        raise
    except MetaRateLimit as e:
        if e.scope != SCOPE_ACCOUNT:
            raise
        return e
    except Exception as e:  # noqa: BLE001 - run_alerts reports its own read failures; this is the rest
        logger.error("stock run %s: health pass failed for %s: %s", run["_id"], gids, e, exc_info=True)
        alerts = [{**_alert(g, "check_failed", "warning", "Couldn't check this client",
                            "Birdy couldn't read this account's health.", run["local_date"]),
                   "read_failed": True} for g in gids]
    failed = {a["client_group_id"]: a for a in alerts if a.get("read_failed")}
    await _add_alerts(db, run, [a for a in alerts if not a.get("read_failed")])
    for gid in gids:
        if gid in failed:
            await _health_failed(db, run, gid, failed[gid])
        else:
            await _save_client(db, run, gid, health_done=True, health_due=None, health_failure=None, rate_limited=None)
    return None


async def _retry_health(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Health reads booked for a later tick, whatever those clients have got to since. False when
    the deadline stopped it."""
    accounts: dict[str | None, list[str]] = {}
    for gid in _health_due_now(run):
        group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
        accounts.setdefault(group.get("meta_ad_account_id"), []).append(gid)
    for gids in accounts.values():
        if _out_of_time(deadline):
            return False
        await _save(db, run, in_flight=gids)
        limit = await _read_health(db, mongo_client, run, agent, gids)
        for gid in gids if limit else []:
            await _save_client(db, run, gid, health_due=_now(run) + _limit_wait(limit), rate_limited=_short(limit))
    return True


async def _read_account(db, mongo_client, run: dict, agent: dict, token: str, account: str | None,
                        entries: list[tuple[str, dict]], deadline) -> bool:
    """One read of the account, stored for each client on it, then its health, once. Saved after
    each step. False when the deadline stopped it. A limit on this account makes its clients wait,
    at no cost, and the reads are separate, so a throttled one doesn't skip the other. A dead token
    or an app-wide limit goes to the caller."""
    gids = [g for g, _ in entries]
    await _save(db, run, in_flight=gids)
    limit = None
    stale_ones = [(g, grp) for g, grp in entries if account and not run["clients"][g].get("refreshed")]
    if stale_ones:
        data = None
        try:
            data = await asyncio.wait_for(read_meta_state(account, token), _read_timeout(deadline))
        except MetaTokenError:
            raise
        except MetaRateLimit as e:
            if e.scope != SCOPE_ACCOUNT:
                raise
            limit = e
        except Exception as e:  # noqa: BLE001 - a failed refresh must not skip the health read
            for gid, group in stale_ones:
                await _refresh_failed(db, run, gid, group, e)
        for gid, group in stale_ones if data else []:
            if _out_of_time(deadline):
                return False
            try:
                await asyncio.wait_for(_store(db, run, gid, account, data), _read_timeout(deadline))
                await _save_client(db, run, gid, refreshed=True, stale=False, wait_for=None, error=None)
            except Exception as e:  # noqa: BLE001 - as above
                await _refresh_failed(db, run, gid, group, e)
    if _out_of_time(deadline):
        return False

    unread = [g for g in gids if _health_unread(run["clients"][g], run)]
    if unread:
        limit = await _read_health(db, mongo_client, run, agent, unread) or limit
    if limit:
        for gid in gids:
            await _save_client(db, run, gid, rate_limited=_short(limit), retry_after=_now(run) + _limit_wait(limit))
        return True

    for gid, group in entries:
        client = run["clients"][gid]
        if client.get("rate_limited"):
            await _save_client(db, run, gid, rate_limited=None)
        if not account:
            await _save_client(db, run, gid, state=FAILED, error="This client has no Meta ad account connected.")
        elif client["state"] == PENDING and client.get("refreshed") and _health_settled(client):
            await _save_client(db, run, gid, state=HEALTHY, wait_for=None, error=client.get("error"))
        elif client["state"] == PENDING and client.get("wait_for") == WAIT_REFRESH and _health_settled(client):
            await _save_client(db, run, gid, state=WAITING, attempts=client.get("attempts") or 0)
    return True


async def _store(db, run: dict, gid: str, account: str, data: tuple) -> None:
    ads, adsets = data
    await store_meta_state(db, run["user_id"], gid, account, ads, adsets, today=_agency_day(run))
    await backfill_paused_at(db, run["user_id"], gid)


async def _should_wait(db, run: dict, gid: str, group: dict) -> bool:
    """Only a batch client that will get a card waits, and only for rows that really are coming.
    Past the grace period it stops waiting, but the top-up is still asked for, since a client held
    up by a rate limit may get here for the first time then."""
    if run["kind"] != BATCH or not run["clients"][gid]["carded"]:
        return False
    tz = group.get(ACCOUNT_TZ_FIELD)
    if await _history_state(db, gid, last_full_day(_run_now(run), tz), tz) != "late":
        return False
    requested = await ad_history.request_topups(db, [gid])
    return bool(requested) and _now(run) < run["as_of"] + GRACE


async def _spend_stage(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Late clients start waiting. Everyone else gets spend alerts, one account at a time, from
    stored rows: nothing here asks Meta."""
    accounts: dict[str, list[str]] = {}
    for gid in _clients_in(run, HEALTHY):
        group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
        if await _should_wait(db, run, gid, group):
            await _save_client(db, run, gid, state=WAITING, wait_for=WAIT_HISTORY)
        else:
            accounts.setdefault(group.get("meta_ad_account_id") or gid, []).append(gid)
    for gids in accounts.values():
        if _out_of_time(deadline):
            return False
        await _save(db, run, in_flight=gids)
        try:
            await _add_alerts(db, run, await run_alerts(db, agent, _token_for(mongo_client), _run_now(run), gids,
                                                        parts="spend", today=run["local_date"]))
        except Exception as e:  # noqa: BLE001 - this client's spend alerts come on a later tick
            for gid in gids:
                await _retry_client(db, run, gid, e)
            continue
        for gid in gids:
            await _save_client(db, run, gid, state=ALERTED if run["clients"][gid]["carded"] else ALERTS_ONLY)
    return True


async def _alerts_pass(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Booked retries, then the token, so a dead one is reported at once. Then each account's state
    and health, and the spend alerts. False when the deadline stopped it."""
    if not await _retry_health(db, mongo_client, run, agent, deadline):
        return False
    pending = _clients_in(run, PENDING)
    if pending:
        token = await _token_for(mongo_client)(run["user_id"])
        if not token:
            await _needs_reconnect(db, run, "Birdy has no Meta connection for this agency, so nothing was checked.")
            return True
        accounts: dict[str | None, list[tuple[str, dict]]] = {}
        for gid in pending:
            group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
            accounts.setdefault(group.get("meta_ad_account_id"), []).append((gid, group))
        for account, entries in accounts.items():
            if _out_of_time(deadline):
                return False
            if not await _read_account(db, mongo_client, run, agent, token, account, entries, deadline):
                return False
    return await _spend_stage(db, mongo_client, run, agent, deadline)


async def _alerts_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """One alerts pass. However it ends, what it found goes to the urgent hook: the hook keeps track
    of what it has already posted, so being called each pass is safe."""
    try:
        go_on = await _alerts_pass(db, mongo_client, run, agent, deadline)
        if go_on and run.get("rate_limited"):
            await _save(db, run, rate_limited=None)
    except MetaTokenError as e:
        await _needs_reconnect(db, run, f"Meta rejected the connection: {_short(e)}")
        go_on = True
    except MetaRateLimit as e:
        logger.warning("stock run %s: stopping this tick: %s", run["_id"], e)
        await _save(db, run, rate_limited=_short(e))
        go_on = False
    except Exception:
        await _call_urgent_hook(db, run, agent)
        raise
    await _call_urgent_hook(db, run, agent)
    return go_on


async def _call_urgent_hook(db, run: dict, agent: dict) -> None:
    if run["kind"] == PREVIEW:
        return
    await _save(db, run, in_flight=[])  # a kill in the hook isn't any client's fault
    try:
        await post_urgent_alerts(db, run, agent)
    except Exception as e:  # noqa: BLE001 - the alerts stay on the run, and the report shows them
        logger.error("stock run %s: urgent alerts failed: %s", run["_id"], e, exc_info=True)
        await _save(db, run, publish_error=_short(e))


# ---------------------------------------------------------------------------
# Checks and writing
# ---------------------------------------------------------------------------

async def _is_acting(db, run: dict, agent: dict, gid: str) -> bool:
    """Only the claim's holder acts. Every other watcher reports with all its lines as Flags: an
    Only flag client, one another agent holds, or one nobody holds (spec section 4). A preview
    runs before the agent claims anything, so an unclaimed client shows the modes it would get."""
    if gid in ((agent.get("scope") or {}).get("flag_only_client_group_ids") or []):
        return False
    holder = await claims.holder(db, run["user_id"], gid)
    return holder == agent["_id"] or (run["kind"] == PREVIEW and holder is None)


def _window(run: dict, agent: dict, group: dict):
    days = _settings(agent)["creative"]["data_window_days"]
    return full_day_window(_run_now(run), group.get(ACCOUNT_TZ_FIELD), days)


async def _group(db, run: dict, gid: str) -> dict:
    group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]})
    if group is None:
        raise ValueError("This client no longer exists.")
    return group


async def _last_card(db, agent_id: str, gid: str, run_id: str) -> dict | None:
    rows = await (db[CARDS].find({"agent_id": agent_id, "client_group_id": gid, "run_id": {"$ne": run_id},
                                  "preview": {"$ne": True}, "status": {"$ne": STATUS_CLOSING}})
                  .sort("created_at", -1).limit(1).to_list(length=1))
    return rows[0] if rows else None


async def _follow_ups(db, run: dict, last: dict | None, facts: dict) -> list[dict]:
    """Never raises: this runs after the AI call, and a missing tick isn't worth wasting it."""
    if last is None:
        return []
    try:
        window = facts["window"]
        since = last["created_at"].date() if isinstance(last.get("created_at"), datetime) \
            else date.fromisoformat(window["prev_start"])
        gid = facts["client_group_id"]
        states, _ = await load_state(db, gid)
        days = await load_ad_days(db, run["user_id"], gid, since, date.fromisoformat(window["end"]))
        return follow_ups(last, states, days)
    except Exception as e:  # noqa: BLE001
        logger.error("stock run %s: follow-ups failed for %s: %s", run["_id"], facts.get("client_group_id"), e,
                     exc_info=True)
        return []


async def _close_with_card(db, run: dict, gid: str) -> None:
    """The card is stored: close any older open card, open this one, then record that the client
    is finished. Repeatable, so a failure anywhere here never costs a second AI call."""
    if run["kind"] != PREVIEW:
        await supersede_open(db, run["agent_id"], gid, run["_id"])
    key = {"run_id": run["_id"], "client_group_id": gid}
    card = await db[CARDS].find_one(key)
    if card["status"] == STATUS_CLOSING:
        await db[CARDS].update_one(key, {"$set": {"status": card["final_status"]}})
    await _save_client(db, run, gid, state=FINISHED, card_id=card["_id"], facts=None, draft=None)


async def _save_card(db, run: dict, agent: dict, facts: dict, last: dict | None, draft: dict | None,
                     candidates: dict | None, flag_only: bool) -> None:
    gid = facts["client_group_id"]
    names = [g.get("name") async for g in db["client_groups"].find({"user_id": run["user_id"]}, {"name": 1})]
    card = build_card(facts, facts["decision"], draft, _settings(agent), run["_id"], candidates=candidates,
                      flag_only=flag_only, client_names=names)
    card.update({"_id": str(uuid.uuid4()), "agent_id": agent["_id"], "user_id": run["user_id"],
                 "stale": bool(run["clients"][gid].get("stale")), "follow_ups": await _follow_ups(db, run, last, facts),
                 "final_status": card["status"], "status": STATUS_CLOSING, "created_at": datetime.utcnow(),
                 **({"preview": True} if run["kind"] == PREVIEW else {})})
    key = {"run_id": run["_id"], "client_group_id": gid}
    await db[CARDS].update_one(key, {"$setOnInsert": {k: v for k, v in card.items() if k not in key}}, upsert=True)
    await _close_with_card(db, run, gid)


async def _no_history_reason(db, gid: str) -> str:
    doc = (await ad_history.statuses(db, [gid]))[gid]
    if doc.get("status") == ad_history.STATUS_ERROR:
        return "No data: Birdy couldn't read this client's ad history from Meta."
    return "No data yet: this client's ad history is still syncing."


async def _check_client(db, run: dict, agent: dict, gid: str) -> None:
    await _save(db, run, in_flight=[gid])
    if await db[CARDS].find_one({"run_id": run["_id"], "client_group_id": gid}, {"_id": 1}):
        await _close_with_card(db, run, gid)  # the save got this far on an earlier try
        return
    group = await _group(db, run, gid)
    window = _window(run, agent, group)
    facts = await client_facts(db, agent, group, window)
    decision = facts["decision"]
    name = group.get("name") or gid
    if decision == "no_zones":
        reason = f"{name} has no zones for its account's currency, so it was not judged. Set them in setup."
        await _add_alerts(db, run, [_alert(gid, "no_zones", "warning", f"{name} needs zones", reason,
                                           run["local_date"])])
        await _save_client(db, run, gid, state=NO_ZONES, reason=reason)
    elif decision == "no_history":
        await _save_client(db, run, gid, state=NO_DATA, reason=await _no_history_reason(db, gid))
    elif decision == "data_late":
        await _save_client(db, run, gid, state=DATA_LATE,
                           reason=f"Ad data hasn't synced through {window.end.isoformat()} yet, so there's no card "
                                  "today. Judging it on missing days could call a working account stopped.")
    elif decision == "propose":
        await _save_client(db, run, gid, state=NEEDS_WRITING, facts=facts)
    else:
        candidates = await build_candidates(db, agent, group, window) if decision == "hold_downtrend" else None
        last = await _last_card(db, agent["_id"], gid, run["_id"])
        await _save_card(db, run, agent, facts, last, None, candidates, not await _is_acting(db, run, agent, gid))


async def _checks_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    for gid in _clients_in(run, ALERTED):
        if _out_of_time(deadline):
            return False
        try:
            await _check_client(db, run, agent, gid)
        except Exception as e:  # noqa: BLE001 - one client's failure stays with that client
            await _retry_client(db, run, gid, e)
    return True


async def _write_client(db, run: dict, agent: dict, gid: str) -> None:
    await _save(db, run, in_flight=[gid])
    if await db[CARDS].find_one({"run_id": run["_id"], "client_group_id": gid}, {"_id": 1}):
        await _close_with_card(db, run, gid)  # stopped after saving the card: its AI call is spent
        return
    facts = run["clients"][gid]["facts"]
    group = await _group(db, run, gid)
    candidates = await build_candidates(db, agent, group, _window(run, agent, group))
    last = await _last_card(db, agent["_id"], gid, run["_id"])
    flag_only = not await _is_acting(db, run, agent, gid)  # it may have moved since the checks
    draft = run["clients"][gid].get("draft")
    if draft is None:  # saved straight away, so a failure from here on doesn't pay for a second call
        draft = await write_card(db, agent, facts, candidates, await lessons.lessons_for_prompt(db, agent["_id"], gid), last_card=last,
                                 open_questions=(last or {}).get("open_questions") or [])
        await _save_client(db, run, gid, draft=draft)
    await _save_card(db, run, agent, facts, last, draft, candidates, flag_only)


async def _writing_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    for gid in _clients_in(run, NEEDS_WRITING):
        if _out_of_time(deadline):
            return False
        try:
            await _write_client(db, run, agent, gid)
        except Exception as e:  # noqa: BLE001 - one client's failure stays with that client
            await _retry_client(db, run, gid, e)
    return True


# ---------------------------------------------------------------------------
# Catch up, publishing and waiting
# ---------------------------------------------------------------------------

async def _catch_up_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Tried once. Nobody is in flight, so a kill here charges no client, and the next tick moves
    on: scoring old ads never holds up today's checks."""
    await _save(db, run, in_flight=[])
    if not run.get("catch_up_started"):
        await _save(db, run, catch_up_started=True)
        try:
            await catch_up_run(db, mongo_client, run, agent)
        except Exception as e:  # noqa: BLE001
            logger.error("stock run %s: catch-up failed: %s", run["_id"], e, exc_info=True)
    await _save(db, run, caught_up=True)
    return True


def _summary(run: dict, cards: list[dict], missing: list[str] | None = None) -> dict:
    could_not = [{"client_group_id": g, "reason": c.get("error")} for g, c in run["clients"].items()
                 if c["state"] == FAILED]
    no_card = [{"client_group_id": g, "state": c["state"], "reason": c.get("reason")}
               for g, c in run["clients"].items() if c["state"] in (NO_ZONES, NO_DATA, DATA_LATE)]
    waiting = [g for g, c in run["clients"].items() if c["state"] in UNFINISHED]
    stale = [g for g, c in run["clients"].items() if c.get("stale")]
    health_unread = [g for g, c in run["clients"].items() if c.get("health_due")]
    tags = {t: sum(1 for c in cards if c.get("tag") == t) for t in ("changes", "no_changes", "green")}
    return {"clients": len(run["clients"]), "cards": len(cards), **tags, "alerts": len(run.get("alerts") or []),
            "could_not_check": could_not, "no_card": no_card, "waiting": waiting, "missing": missing or [],
            "health_unread": health_unread, "stale": bool(stale), "stale_clients": stale, "reconnect": bool(run.get("reconnect"))}


async def _run_cards(db, run: dict) -> list[dict]:
    return await db[CARDS].find({"run_id": run["_id"], "status": {"$ne": STATUS_CLOSING}}).to_list(length=None)


async def _publish(db, run: dict, agent: dict, cards: list[dict]) -> None:
    try:
        await publish_run(db, run, agent, cards)
    except Exception as e:  # noqa: BLE001 - the report stays available without Slack
        logger.error("stock run %s: publish failed: %s", run["_id"], e, exc_info=True)
        await _save(db, run, publish_error=_short(e))


def _states(run: dict) -> dict:
    return {g: c["state"] for g, c in run["clients"].items()}


async def _publishing_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    await _save(db, run, in_flight=[])
    cards = await _run_cards(db, run)
    await _save(db, run, summary=_summary(run, cards), published_states=_states(run))
    if run["kind"] == PREVIEW:
        return True
    await _publish(db, run, agent, cards)
    if not run.get("reconnect"):  # with a dead token, no writes
        await auto_apply_run(db, mongo_client, run, agent, deadline=deadline)
    return True


async def _waiting_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Look again at clients waiting for a later tick. False when nothing can move yet."""
    for gid in _clients_in(run, WAITING):
        client = run["clients"][gid]
        if client.get("wait_for") == WAIT_REFRESH:
            await _save_client(db, run, gid, state=PENDING, attempts=client.get("attempts") or 0, retry_after=None)
            continue
        group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
        if not await _should_wait(db, run, gid, group):
            await _save_client(db, run, gid, state=HEALTHY, wait_for=None)
    return bool(_clients_in(run, PENDING, HEALTHY, ALERTED, NEEDS_WRITING) or _health_due_now(run)) \
        or not any(c["state"] in UNFINISHED or c.get("health_due") for c in run["clients"].values())


async def _end_incomplete(db, run: dict, agent: dict) -> dict:
    """Out of attempts: finish now and say what's missing. A stored card still closes the older
    one. The stored phase is done before publishing, so even a publish that kills the invocation
    again can't keep the run alive."""
    missing, attempts = [], run.get("attempts") or 0
    for gid in [g for g, c in run["clients"].items() if c["state"] in UNFINISHED]:
        limited = run["clients"][gid].get("rate_limited") or run.get("rate_limited")
        if not await _give_up(db, run, gid, RATE_LIMITED if limited else OUT_OF_TIME):
            missing.append(gid)
    await _report_unread_health(db, run)
    cards = await _run_cards(db, run)
    await _save(db, run, phase=DONE, claimed_at=None, in_flight=[], finished_at=datetime.utcnow(), ended_early=True,
                attempts=attempts, summary=_summary(run, cards, missing))
    logger.error("stock run %s ended after %s attempts, missing %s", run["_id"], attempts, missing)
    if run["kind"] != PREVIEW:
        await _call_urgent_hook(db, run, agent)
        await _publish(db, run, agent, cards)
        await _save(db, run, claimed_at=None)
    return run


async def _report_unread_health(db, run: dict) -> None:
    """Health retries the run won't get to. Their failures weren't reported while a retry could
    still clear them, so they are now."""
    for gid, c in run["clients"].items():
        if not c.get("health_due"):
            continue
        group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
        await _add_alerts(db, run, [c.get("health_failure") or _alert(
            gid, "check_failed", "warning", f"Couldn't check {group.get('name') or gid}",
            c.get("rate_limited") or "Meta could not be read.", run["local_date"])])
        await _save_client(db, run, gid, health_due=None, health_failure=None)


async def end_overdue_runs(db, agent: dict, now_utc: datetime) -> None:
    """A run still unfinished 24 hours after its scheduled time ends here, when the next one is
    due. Measured from that time, not local midnight, so a late evening run keeps its full day."""
    cutoff = _naive(now_utc) - OVERDUE_AFTER
    q = {"agent_id": agent["_id"], "scheduled": True, "phase": {"$ne": DONE}, "scheduled_at": {"$lte": cutoff}}
    for stale in await db[RUNS].find(q, {"_id": 1}).to_list(length=None):
        run = await _claim(db, stale["_id"])
        if run is not None:
            run["tick_now"] = _naive(now_utc)
            await _end_incomplete(db, run, agent)


async def end_stale_previews(db, agent: dict, now_utc: datetime) -> None:
    """A preview waiting longer than PREVIEW_MAX_AGE (a throttled account can keep it `waiting`
    for good, and scheduled-run cleanup doesn't cover previews) ends here, saying what's missing."""
    cutoff = _naive(now_utc) - PREVIEW_MAX_AGE
    q = {"agent_id": agent["_id"], "kind": PREVIEW, "phase": {"$ne": DONE}, "as_of": {"$lte": cutoff}}
    for stale in await db[RUNS].find(q, {"_id": 1}).to_list(length=None):
        run = await _claim(db, stale["_id"])
        if run is not None:
            run["tick_now"] = _naive(now_utc)
            await _end_incomplete(db, run, agent)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

def _next_phase(run: dict) -> str:
    phase = run["phase"]
    if phase == ALERTS:
        if run.get("reconnect") or run["kind"] == ALERT_ONLY:
            return PUBLISHING
        return CATCH_UP if run["kind"] == BATCH and not run.get("caught_up") else CHECKS
    if phase == WAITING_PHASE:
        if _clients_in(run, PENDING, HEALTHY) or _health_due_now(run):
            return ALERTS
        if _clients_in(run, ALERTED, NEEDS_WRITING):
            return CHECKS
        return PUBLISHING if _states(run) != run.get("published_states") else DONE
    return {CATCH_UP: CHECKS, CHECKS: WRITING, WRITING: PUBLISHING, PUBLISHING: WAITING_PHASE}[phase]


async def _step(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """One phase. False when it stopped at the deadline, or has nothing it can do until later."""
    phase = {ALERTS: _alerts_phase, CATCH_UP: _catch_up_phase, CHECKS: _checks_phase, WRITING: _writing_phase,
             PUBLISHING: _publishing_phase, WAITING_PHASE: _waiting_phase}[run["phase"]]
    return await phase(db, mongo_client, run, agent, deadline)


async def _count_takeover(db, run: dict) -> None:
    """The last invocation was killed. The clients it was working on take the blame, so the rest
    aren't affected. With none in flight it counts against the run."""
    in_flight = [g for g in run.get("in_flight") or [] if run["clients"].get(g, {}).get("state") in UNFINISHED]
    logger.warning("stock run %s: taking over a killed invocation, in flight: %s", run["_id"], in_flight)
    if not in_flight:
        await _save(db, run, attempts=(run.get("attempts") or 0) + 1)
        return
    for gid in in_flight:
        n = (run["clients"][gid].get("attempts") or 0) + 1
        if n >= CLIENT_ATTEMPTS:
            await _give_up(db, run, gid, OUT_OF_TIME, attempts=n)
        else:
            await _save_client(db, run, gid, attempts=n)
    await _save(db, run, in_flight=[])


async def advance_run(db, mongo_client, run: dict, agent: dict, *, deadline: float | None,
                      now_utc: datetime | None = None) -> dict:
    """Claim the run, do what fits before `deadline` (time.monotonic()) and release it. Returns the
    run as saved. A run another tick holds comes back still claimed and untouched. `now_utc` is
    this tick's time, for retries and the grace window."""
    claimed = await _claim(db, run["_id"])
    if claimed is None:
        return await db[RUNS].find_one({"_id": run["_id"]}) or run
    run = claimed
    run["tick_now"] = _naive(now_utc or datetime.now(timezone.utc))
    run["tick_deadline"] = deadline
    try:
        if run.pop("taken_over", False):
            await _count_takeover(db, run)
        if (run.get("attempts") or 0) >= MAX_RUN_ATTEMPTS:
            return _finished(await _end_incomplete(db, run, agent))
        while run["phase"] != DONE and not _out_of_time(deadline):
            if not await _step(db, mongo_client, run, agent, deadline):
                break
            next_phase = _next_phase(run)
            await _save(db, run, phase=next_phase, attempts=0,
                        **({"finished_at": datetime.utcnow()} if next_phase == DONE else {}))
    except Exception as e:  # noqa: BLE001 - counted, so a run that always raises still ends
        logger.error("stock run %s failed in %s: %s", run["_id"], run["phase"], e, exc_info=True)
        await _save(db, run, attempts=(run.get("attempts") or 0) + 1, last_error=_short(e), claimed_at=None, in_flight=[])
        return _finished(run)
    await _save(db, run, claimed_at=None, in_flight=[])
    return _finished(run)


def _finished(run: dict) -> dict:
    run.pop("tick_now", None)
    run.pop("tick_deadline", None)
    return run
