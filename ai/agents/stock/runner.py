"""
ai/agents/stock/runner.py
-------------------------
The stock agent's daily run as a stored state machine (spec section 6). A run can't finish in one
Vercel invocation, so each tick claims it, does what fits before the deadline, saves after every
client and releases it. Phases: alerts, catch_up, checks, writing, publishing, done.

A client's `state` in `run.clients` says what is left for it, so a resumed run never repeats
finished work:
  pending -> alerted -> needs_writing -> done     the path of a client getting an AI card
  alerts_only, on_hold, held_elsewhere, no_zones, no_data, data_late, failed    finished, no card

A claim older than STALE_CLAIM belongs to a killed invocation and is taken over. `attempts` counts
those takeovers and runs that raised; at MAX_RUN_ATTEMPTS the run ends and reports what's missing.

Publishing can run twice if an invocation dies mid-way, so `publish_run` and `auto_apply_run`
must be safe to repeat (Slack's stored ts and each line's atomic claim make them so).
"""

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.agents import claims
from ai.agents.meta_state import MetaTokenError, backfill_paused_at, refresh_meta_state
from ai.agents.snapshot import load_ad_days, load_state
from ai.agents.stock.alerts import ACCOUNT_TZ_FIELD, _alert, _history_state, run_alerts
from ai.agents.stock.candidates import build_candidates
from ai.agents.stock.cards import build_card, supersede_open
from ai.agents.stock.checks import client_facts, follow_ups
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings
from ai.agents.stock.windows import full_day_window, last_full_day
from ai.agents.stock.writer import write_card
from ai.agents.store import CARDS, RUNS, list_memory
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

ALERTS, CATCH_UP, CHECKS, WRITING, PUBLISHING, DONE = "alerts", "catch_up", "checks", "writing", "publishing", "done"
BATCH, ALERT_ONLY, PREVIEW = "batch", "alert_only", "preview"
PENDING, ALERTED, NEEDS_WRITING, FINISHED, FAILED = "pending", "alerted", "needs_writing", "done", "failed"
ALERTS_ONLY, ON_HOLD, HELD_ELSEWHERE, NO_ZONES, NO_DATA, DATA_LATE = (
    "alerts_only", "on_hold", "held_elsewhere", "no_zones", "no_data", "data_late")
UNFINISHED = (PENDING, ALERTED, NEEDS_WRITING)

MAX_RUN_ATTEMPTS = 3
STALE_CLAIM = timedelta(minutes=10)  # an invocation lives 300 s at most, and each client's save renews the claim
CLIENT_READ_TIMEOUT_S = 100          # started just before the 180 s budget, this still ends inside the 300 s limit
ERROR_MAX = 300
RECONNECT = "Meta needs reconnecting. Reconnect to check this client."
OUT_OF_TIME = "Birdy ran out of time before finishing this client."


# ---------------------------------------------------------------------------
# Replaced by later tasks
# ---------------------------------------------------------------------------

async def catch_up_run(db, mongo_client, run: dict, agent: dict) -> None:
    """Batch days: read edits and comments since the last run, score due ads."""


async def publish_run(db, run: dict, agent: dict, cards: list[dict]) -> None:
    """Post the summary and one reply per card to Slack."""


async def auto_apply_run(db, mongo_client, run: dict, agent: dict) -> None:
    """Make the run's Auto changes."""


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


def _new_run(agent: dict, now_utc: datetime, kind: str | None, stale: bool) -> dict:
    settings = _settings(agent)
    today = _local_date(agent, now_utc)
    derived, batch_id = run_kind(settings, today)
    kind = kind or derived
    batch_ids = _batches_on(settings, today) if kind == BATCH else []
    clients = {}
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        cfg = settings["clients"].get(gid) or {}
        carded = kind == PREVIEW or cfg.get("batch_id") in batch_ids
        clients[gid] = {"state": ON_HOLD if cfg.get("on_hold") else PENDING, "error": None, "carded": carded}
    now = datetime.utcnow()
    return {"_id": str(uuid.uuid4()), "agent_id": agent["_id"], "user_id": agent["user_id"],
            "local_date": today.isoformat(), "kind": kind, "batch_id": batch_id if kind == BATCH else None,
            "phase": ALERTS, "clients": clients, "alerts": [], "summary": {},
            "slack": {"channel_id": None, "ts": None}, "claimed_at": None, "attempts": 0, "stale": stale,
            "reconnect": False, "as_of": now_utc.astimezone(timezone.utc).replace(tzinfo=None), "created_at": now,
            **({} if kind == PREVIEW else {"scheduled": True})}


async def find_run(db, agent_id: str, local_date: str) -> dict | None:
    return await db[RUNS].find_one({"agent_id": agent_id, "local_date": local_date, "scheduled": True})


async def get_or_create_run(db, agent: dict, now_utc: datetime, *, kind: str | None = None,
                            stale: bool = False) -> dict:
    """The day's scheduled run, unique on (agent_id, local_date). A preview doesn't take that slot:
    an unfinished preview is returned, and otherwise a new one starts."""
    fresh = _new_run(agent, now_utc, kind, stale)
    if kind == PREVIEW:
        open_preview = await db[RUNS].find_one({"agent_id": agent["_id"], "kind": PREVIEW, "phase": {"$ne": DONE}})
        if open_preview:
            return open_preview
        await db[RUNS].insert_one(fresh)
        return fresh
    key = {"agent_id": agent["_id"], "local_date": fresh["local_date"], "scheduled": True}
    try:
        await db[RUNS].update_one(key, {"$setOnInsert": {k: v for k, v in fresh.items() if k not in key}}, upsert=True)
    except Exception as e:
        if "duplicate key" not in str(e).lower():
            raise  # otherwise a concurrent tick created it first
    return await db[RUNS].find_one(key)


async def late_clients(db, groups: list[dict], now_utc: datetime) -> list[str]:
    """Clients whose ad rows have synced, but not through their account's last full day."""
    out = []
    for g in groups:
        tz = g.get(ACCOUNT_TZ_FIELD)
        if await _history_state(db, g["id"], last_full_day(now_utc, tz), tz) == "late":
            out.append(g["id"])
    return out


# ---------------------------------------------------------------------------
# Claims and saving
# ---------------------------------------------------------------------------

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
    attempts = before.get("attempts") or 0
    if before.get("claimed_at") is not None:
        attempts += 1
        logger.warning("stock run %s: taking over a killed invocation (attempt %d)", run_id, attempts)
        await db[RUNS].update_one({"_id": run_id}, {"$set": {"attempts": attempts}})
    return {**before, "claimed_at": now, "attempts": attempts}


async def _save(db, run: dict, **fields) -> None:
    """Set top-level run fields, in memory and stored."""
    run.update(fields)
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": fields})


async def _save_client(db, run: dict, gid: str, **fields) -> None:
    """Set a client's fields and renew the claim: this tick is alive."""
    now = datetime.utcnow()
    run["clients"][gid].update(fields)
    run["claimed_at"] = now
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "claimed_at": now, **{f"clients.{gid}.{k}": v for k, v in fields.items()}}})


def _short(e: Exception) -> str:
    return (str(e) or type(e).__name__)[:ERROR_MAX]


async def _fail_client(db, run: dict, gid: str, e: Exception) -> None:
    logger.warning("stock run %s: client %s failed: %s", run["_id"], gid, e, exc_info=True)
    await _save_client(db, run, gid, state=FAILED, error=_short(e), facts=None)


def _with_alerts(run: dict, new: list[dict]) -> list[dict]:
    """One alert per client and code, so a repeating alert or a retried phase shows once."""
    merged = {(a.get("client_group_id"), a.get("code")): a for a in run.get("alerts") or []}
    merged.update({(a.get("client_group_id"), a.get("code")): a for a in new})
    return list(merged.values())


async def _add_alerts(db, run: dict, new: list[dict]) -> None:
    await _save(db, run, alerts=_with_alerts(run, new))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

async def _needs_reconnect(db, run: dict, detail: str) -> None:
    """The token is dead for every client of this user: record it and stop reading."""
    today = _run_now(run).date().isoformat()
    alert = _alert(None, "meta_reconnect", "critical", "Meta needs reconnecting", detail, today)
    for gid, c in run["clients"].items():
        if c["state"] in UNFINISHED:
            await _save_client(db, run, gid, state=FAILED, error=RECONNECT)
    await _save(db, run, reconnect=True, alerts=_with_alerts(run, [alert]))


async def _refresh(db, mongo_client, run: dict, gid: str, account: str) -> None:
    async def read():
        await refresh_meta_state(db, mongo_client, run["user_id"], gid, account, today=_run_now(run).date())
        await backfill_paused_at(db, run["user_id"], gid)

    await asyncio.wait_for(read(), CLIENT_READ_TIMEOUT_S)


async def _alerts_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """Refresh each client's ad state, then read its alerts. Nothing else keeps meta_ad_state
    current to the morning: the queued meta refresh writes other collections, and the history
    pass only reads it daily. Clients sharing an ad account go together, so it's read once."""
    pending = [g for g, c in run["clients"].items() if c["state"] == PENDING]
    if not pending:
        return True

    async def token_for(user_id):
        return ((await get_facebook_token(user_id, mongo_client)) or {}).get("access_token")

    if not await token_for(run["user_id"]):
        await _needs_reconnect(db, run, "Birdy has no Meta connection for this agency, so nothing was checked.")
        return True

    accounts: dict[str, list[tuple[str, dict]]] = {}
    for gid in pending:
        group = await db["client_groups"].find_one({"id": gid, "user_id": run["user_id"]}) or {}
        accounts.setdefault(group.get("meta_ad_account_id") or f"none:{gid}", []).append((gid, group))

    for entries in accounts.values():
        if _out_of_time(deadline):
            return False
        readable = []
        for gid, group in entries:
            account = group.get("meta_ad_account_id")
            try:
                if account:
                    await _refresh(db, mongo_client, run, gid, account)
                readable.append(gid)
            except MetaTokenError as e:
                await _needs_reconnect(db, run, f"Meta rejected the connection: {_short(e)}")
                return True
            except Exception as e:  # noqa: BLE001 - one client's failure stays with that client
                await _fail_client(db, run, gid, e)
                name = group.get("name") or gid
                await _add_alerts(db, run, [_alert(gid, "check_failed", "warning", f"Couldn't check {name}",
                                                   f"Meta could not be read: {_short(e)}",
                                                   _run_now(run).date().isoformat())])
        if not readable:
            continue
        try:
            await _add_alerts(db, run, await run_alerts(db, agent, token_for, _run_now(run), readable))
        except Exception as e:  # noqa: BLE001 - run_alerts reports its own read failures; this is the rest
            for gid in readable:
                await _fail_client(db, run, gid, e)
            continue
        groups = dict(entries)
        for gid in readable:
            if not groups[gid].get("meta_ad_account_id"):
                await _save_client(db, run, gid, state=FAILED, error="This client has no Meta ad account connected.")
            else:
                await _save_client(db, run, gid, state=ALERTED if run["clients"][gid]["carded"] else ALERTS_ONLY)
    return True


# ---------------------------------------------------------------------------
# Checks and writing
# ---------------------------------------------------------------------------

async def _acting_as(db, run: dict, agent: dict, gid: str) -> str:
    """"acting" holds the client's claim. "flag_only" watches it with no claim, or was told Only
    flag: its lines are all Flags. "held" belongs to another agent and gets no card here. A
    preview changes nothing. It runs before the agent claims anything, so an unclaimed client
    shows the modes it would get, and a held one shows as Flags."""
    holder = await claims.holder(db, run["user_id"], gid)
    preview = run["kind"] == PREVIEW
    if gid in ((agent.get("scope") or {}).get("flag_only_client_group_ids") or []):
        return "flag_only"
    if holder == agent["_id"] or (preview and holder is None):
        return "acting"
    return "flag_only" if preview or holder is None else "held"


async def _mark_held(db, run: dict, gid: str) -> None:
    await _save_client(db, run, gid, state=HELD_ELSEWHERE, facts=None,
                       reason="Another agent acts on this client, so this agent made no card for it.")


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
                                  "preview": {"$ne": True}})
                  .sort("created_at", -1).limit(1).to_list(length=1))
    return rows[0] if rows else None


async def _lessons(db, agent_id: str, gid: str) -> list[str]:
    """Active lessons for this client or the whole agency, newest first."""
    rows = await list_memory(db, agent_id, active_only=True)
    keep = [m for m in rows if m.get("status") != "overruled" and m.get("client_group_id") in (None, gid)]
    return [m["text"] for m in reversed(keep) if m.get("text")]


async def _follow_ups(db, run: dict, last: dict | None, facts: dict) -> list[dict]:
    if last is None:
        return []
    window = facts["window"]
    since = last["created_at"].date() if isinstance(last.get("created_at"), datetime) \
        else date.fromisoformat(window["prev_start"])
    gid = facts["client_group_id"]
    states, _ = await load_state(db, gid)
    days = await load_ad_days(db, run["user_id"], gid, since, date.fromisoformat(window["end"]))
    return follow_ups(last, states, days)


async def _close_with_card(db, run: dict, gid: str, card_id: str | None = None, **fields) -> None:
    """The card is stored: close any older open card, then record that this client is finished.
    Repeatable, so a kill anywhere in here never causes a second AI call."""
    if run["kind"] != PREVIEW:
        await supersede_open(db, run["agent_id"], gid, run["_id"])
    if card_id is None:
        card_id = (await db[CARDS].find_one({"run_id": run["_id"], "client_group_id": gid}, {"_id": 1}))["_id"]
    await _save_client(db, run, gid, state=FINISHED, card_id=card_id, facts=None, **fields)


async def _save_card(db, run: dict, agent: dict, facts: dict, last: dict | None, draft: dict | None,
                     candidates: dict | None, flag_only: bool) -> None:
    gid = facts["client_group_id"]
    card = build_card(facts, facts["decision"], draft, _settings(agent), run["_id"], candidates=candidates,
                      flag_only=flag_only)
    card.update({"_id": str(uuid.uuid4()), "agent_id": agent["_id"], "user_id": run["user_id"],
                 "stale": bool(run.get("stale")), "follow_ups": await _follow_ups(db, run, last, facts),
                 "created_at": datetime.utcnow(), **({"preview": True} if run["kind"] == PREVIEW else {})})
    key = {"run_id": run["_id"], "client_group_id": gid}
    await db[CARDS].update_one(key, {"$setOnInsert": {k: v for k, v in card.items() if k not in key}}, upsert=True)
    await _close_with_card(db, run, gid)


async def _check_client(db, run: dict, agent: dict, gid: str) -> None:
    acting = await _acting_as(db, run, agent, gid)
    if acting == "held":
        await _mark_held(db, run, gid)
        return
    group = await _group(db, run, gid)
    window = _window(run, agent, group)
    facts = await client_facts(db, agent, group, window)
    decision = facts["decision"]
    name = group.get("name") or gid
    if decision == "no_zones":
        reason = f"{name} has no zones for its account's currency, so it was not judged. Set them in setup."
        await _add_alerts(db, run, [_alert(gid, "no_zones", "warning", f"{name} needs zones", reason,
                                           _run_now(run).date().isoformat())])
        await _save_client(db, run, gid, state=NO_ZONES, reason=reason)
    elif decision == "no_history":
        await _save_client(db, run, gid, state=NO_DATA,
                           reason="No data yet: this client's ad history is still syncing.")
    elif decision == "data_late":
        await _save_client(db, run, gid, state=DATA_LATE,
                           reason=f"Ad data hasn't synced through {window.end.isoformat()} yet, so there's no card "
                                  "today. Judging it on missing days could call a working account stopped.")
    elif decision == "propose":
        await _save_client(db, run, gid, state=NEEDS_WRITING, facts=facts)
    else:
        candidates = await build_candidates(db, agent, group, window) if decision == "hold_downtrend" else None
        last = await _last_card(db, agent["_id"], gid, run["_id"])
        await _save_card(db, run, agent, facts, last, None, candidates, acting == "flag_only")


async def _checks_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    for gid in [g for g, c in run["clients"].items() if c["state"] == ALERTED]:
        if _out_of_time(deadline):
            return False
        try:
            await _check_client(db, run, agent, gid)
        except Exception as e:  # noqa: BLE001 - one client's failure stays with that client
            await _fail_client(db, run, gid, e)
    return True


async def _write_client(db, run: dict, agent: dict, gid: str) -> None:
    if await db[CARDS].find_one({"run_id": run["_id"], "client_group_id": gid}, {"_id": 1}):
        await _close_with_card(db, run, gid)  # killed after saving the card: its AI call is spent
        return
    acting = await _acting_as(db, run, agent, gid)  # it may have moved since the checks
    if acting == "held":
        await _mark_held(db, run, gid)
        return
    facts = run["clients"][gid]["facts"]
    group = await _group(db, run, gid)
    candidates = await build_candidates(db, agent, group, _window(run, agent, group))
    last = await _last_card(db, agent["_id"], gid, run["_id"])
    draft = await write_card(db, agent, facts, candidates, await _lessons(db, agent["_id"], gid), last_card=last,
                             open_questions=(last or {}).get("open_questions") or [])
    await _save_card(db, run, agent, facts, last, draft, candidates, acting == "flag_only")


async def _writing_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    for gid in [g for g, c in run["clients"].items() if c["state"] == NEEDS_WRITING]:
        if _out_of_time(deadline):
            return False
        try:
            await _write_client(db, run, agent, gid)
        except Exception as e:  # noqa: BLE001 - one client's failure stays with that client
            await _fail_client(db, run, gid, e)
    return True


# ---------------------------------------------------------------------------
# Catch up and publishing
# ---------------------------------------------------------------------------

async def _catch_up_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    try:
        await catch_up_run(db, mongo_client, run, agent)
    except Exception as e:  # noqa: BLE001 - scoring old ads never holds up today's checks
        logger.error("stock run %s: catch-up failed: %s", run["_id"], e, exc_info=True)
    return True


def _summary(run: dict, cards: list[dict], missing: list[str] | None = None) -> dict:
    could_not = [{"client_group_id": g, "reason": c.get("error")} for g, c in run["clients"].items()
                 if c["state"] == FAILED]
    no_card = [{"client_group_id": g, "state": c["state"], "reason": c.get("reason")}
               for g, c in run["clients"].items() if c["state"] in (HELD_ELSEWHERE, NO_ZONES, NO_DATA, DATA_LATE)]
    tags = {t: sum(1 for c in cards if c.get("tag") == t) for t in ("changes", "no_changes", "green")}
    return {"clients": len(run["clients"]), "cards": len(cards), **tags, "alerts": len(run.get("alerts") or []),
            "could_not_check": could_not, "no_card": no_card, "missing": missing or [],
            "stale": bool(run.get("stale")), "reconnect": bool(run.get("reconnect"))}


async def _run_cards(db, run: dict) -> list[dict]:
    return await db[CARDS].find({"run_id": run["_id"]}).to_list(length=None)


async def _publish(db, run: dict, agent: dict, cards: list[dict]) -> None:
    try:
        await publish_run(db, run, agent, cards)
    except Exception as e:  # noqa: BLE001 - the report stays available without Slack
        logger.error("stock run %s: publish failed: %s", run["_id"], e, exc_info=True)
        await _save(db, run, publish_error=_short(e))


async def _publishing_phase(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    cards = await _run_cards(db, run)
    await _save(db, run, summary=_summary(run, cards))
    if run["kind"] == PREVIEW:
        return True
    await _publish(db, run, agent, cards)
    if not run.get("reconnect"):  # with a dead token, no writes
        await auto_apply_run(db, mongo_client, run, agent)
    return True


async def _end_incomplete(db, run: dict, agent: dict) -> dict:
    """Out of attempts: finish now and say what's missing. The stored phase is done first, so
    even a publish that kills the invocation again can't keep the run alive."""
    missing = [g for g, c in run["clients"].items() if c["state"] in UNFINISHED]
    for gid in missing:
        await _save_client(db, run, gid, state=FAILED, error=OUT_OF_TIME, facts=None)
    cards = await _run_cards(db, run)
    await _save(db, run, phase=DONE, claimed_at=None, finished_at=datetime.utcnow(), ended_early=True,
                summary=_summary(run, cards, missing))
    logger.error("stock run %s ended after %s attempts, missing %s", run["_id"], run.get("attempts"), missing)
    if run["kind"] != PREVIEW:
        await _publish(db, run, agent, cards)
    return run


async def end_overdue_runs(db, agent: dict, local_date: str) -> None:
    """A run from an earlier day that never finished ends here. Its window is out of date and a
    late card from it would supersede nothing useful."""
    q = {"agent_id": agent["_id"], "scheduled": True, "phase": {"$ne": DONE}, "local_date": {"$lt": local_date}}
    for stale in await db[RUNS].find(q, {"_id": 1}).to_list(length=None):
        run = await _claim(db, stale["_id"])
        if run is not None:
            await _end_incomplete(db, run, agent)


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

def _next_phase(run: dict) -> str:
    if run["phase"] == ALERTS:
        if run.get("reconnect") or run["kind"] == ALERT_ONLY:
            return PUBLISHING
        return CATCH_UP if run["kind"] == BATCH else CHECKS
    return {CATCH_UP: CHECKS, CHECKS: WRITING, WRITING: PUBLISHING, PUBLISHING: DONE}[run["phase"]]


async def _step(db, mongo_client, run: dict, agent: dict, deadline) -> bool:
    """One phase. False when it stopped at the deadline with work left."""
    phase = {ALERTS: _alerts_phase, CATCH_UP: _catch_up_phase, CHECKS: _checks_phase, WRITING: _writing_phase,
             PUBLISHING: _publishing_phase}[run["phase"]]
    return await phase(db, mongo_client, run, agent, deadline)


async def advance_run(db, mongo_client, run: dict, agent: dict, *, deadline: float | None) -> dict:
    """Claim the run, do what fits before `deadline` (time.monotonic()) and release it. Returns the
    run as saved. A run another tick holds, or one already done, comes back untouched."""
    claimed = await _claim(db, run["_id"])
    if claimed is None:
        return await db[RUNS].find_one({"_id": run["_id"]}) or run
    run = claimed
    if run["attempts"] >= MAX_RUN_ATTEMPTS:
        return await _end_incomplete(db, run, agent)
    try:
        while run["phase"] != DONE and not _out_of_time(deadline):
            if not await _step(db, mongo_client, run, agent, deadline):
                break
            next_phase = _next_phase(run)
            fields = {"phase": next_phase}
            if next_phase == DONE:
                fields["finished_at"] = datetime.utcnow()
            await _save(db, run, **fields)
    except Exception as e:  # noqa: BLE001 - counted, so a run that always raises still ends
        logger.error("stock run %s failed in %s: %s", run["_id"], run["phase"], e, exc_info=True)
        await _save(db, run, attempts=run["attempts"] + 1, last_error=_short(e), claimed_at=None)
        return run
    await _save(db, run, claimed_at=None)
    return run
