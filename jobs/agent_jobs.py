"""
jobs/agent_jobs.py
------------------
The BirdyHub agents-tick: every 15 minutes, run each active agent that is due.

Due = local now >= today's local schedule.time AND last_run_at is not today (local).
Spring-forward gap (a time that does not exist locally) resolves to the first valid
instant after it, so the agent still runs once that day.

Freshness = the oldest `client_groups.last_meta_refresh` among watched clients (the
field meta_refresh_manager stamps when a refresh reaches a terminal state). Data older
than 3h inside the 2h grace window after the scheduled time: queue a refresh and wait.
After the grace window: run anyway with force_stale (items are created, nothing is
auto-applied). run_agent refreshes Meta state itself, so it is not refreshed here.

Time budget: Vercel kills the function at 300s (vercel.json uses `builds`, which
cannot set maxDuration). A tick stops claiming new agents after TICK_BUDGET_SECONDS
and leaves them for the next 15-minute tick; the proposal scan is skipped past it.
A claim that was taken but never finished (the invocation was killed mid-run) goes
stale after STALE_CLAIM and is retried, at most MAX_RUN_ATTEMPTS times a day; after
that the day is recorded as last_run {status: "error", reason: "timed_out"}.

Stock agents run on their own state machine (ai/agents/stock/runner.py) from
run_due_stock_agents: the same due check, but the run starts on time with no agent-wide wait,
because its first job is alerts. Waiting for spend data is per client, inside the run. Once a run
exists, every tick resumes it until it is done, and only then is last_run_at stamped.

agents-outcomes (daily 05:00 UTC): stores the 7-day outcome on applied agent actions
(see ai/agents/outcomes.py).
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.agents import learning
from ai.agents import outcomes
from ai.agents.runner import run_agent
from ai.agents.stock import runner as stock_runner
from ai.agents.store import AGENTS, TYPE_STOCK
from core.database import DB_NAME
from dependencies import get_mongo_client

logger = logging.getLogger(__name__)

MAX_DATA_AGE = timedelta(hours=3)
GRACE = timedelta(hours=2)
FRESHNESS_FIELD = "last_meta_refresh"
IN_FLIGHT = ("pending", "in_progress")
TICK_BUDGET_SECONDS = 180          # of Vercel's 300s function timeout
STALE_CLAIM = timedelta(minutes=30)
MAX_RUN_ATTEMPTS = 3


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _schedule(agent: dict) -> tuple[int, int, ZoneInfo]:
    sched = (agent.get("scope") or {}).get("schedule") or {}
    try:
        hh, mm = (sched.get("time") or "07:00").split(":")
        hh, mm = int(hh), int(mm)
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError("time out of range")
        return hh, mm, ZoneInfo(sched.get("tz") or "Europe/London")
    except Exception as e:
        raise ValueError(f"invalid schedule {sched!r}: {e}") from e


def _scheduled_utc(agent: dict, now_utc: datetime) -> tuple[datetime, "datetime.date"]:
    """(today's scheduled instant in UTC, today's local date). A nonexistent local
    time (DST gap) lands on the first valid instant after the gap."""
    hh, mm, tz = _schedule(agent)
    today = now_utc.astimezone(tz).date()
    first = datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz)
    cand = first.astimezone(timezone.utc)
    if cand.astimezone(tz).replace(tzinfo=None) != first.replace(tzinfo=None):
        # DST gap: fold=0 and fold=1 land an equal distance either side of the
        # transition instant, so their midpoint is the first valid moment after it.
        other = first.replace(fold=1).astimezone(timezone.utc)
        cand = min(cand, other) + abs(cand - other) / 2
    return cand, today


def _is_due(agent: dict, now_utc: datetime) -> bool:
    _, _, tz = _schedule(agent)
    sched_utc, today = _scheduled_utc(agent, now_utc)
    if now_utc < sched_utc:
        return False
    last = agent.get("last_run_at")
    return not (last and _aware_utc(last).astimezone(tz).date() == today)


async def _groups(db, agent: dict) -> list[dict]:
    gids = list((agent.get("scope") or {}).get("client_group_ids") or [])
    if not gids:
        return []
    return await db["client_groups"].find(
        {"id": {"$in": gids}, "user_id": agent["user_id"], "meta_ad_account_id": {"$nin": [None, ""]}}
    ).to_list(length=None)


def _oldest_refresh(groups: list[dict]) -> datetime | None:
    """Oldest refresh stamp across clients; None if any never refreshed (treated as very old)."""
    stamps = [g.get(FRESHNESS_FIELD) for g in groups]
    if not stamps:
        return datetime.max.replace(tzinfo=timezone.utc)  # nothing to refresh -> fresh
    if any(s is None for s in stamps):
        return None
    return min(_aware_utc(s) for s in stamps)


async def _enqueue_refreshes(db, mongo_client, groups: list[dict], now_utc: datetime) -> int:
    """Queue (not run) a Meta refresh for each stale client without one already in flight."""
    from services.meta_refresh_manager import create_refresh_job

    queued = 0
    for g in groups:
        stamp = g.get(FRESHNESS_FIELD)
        if stamp is not None and now_utc - _aware_utc(stamp) <= MAX_DATA_AGE:
            continue
        if await db["meta_refresh_jobs"].find_one({"group_id": g["id"], "status": {"$in": list(IN_FLIGHT)}}):
            continue
        try:
            await create_refresh_job(g["id"], g["user_id"], g, mongo_client, status="pending")
            queued += 1
        except Exception as e:
            logger.error("agents-tick: could not queue refresh for %s: %s", g.get("id"), e, exc_info=True)
    return queued


def _last_run(result, at: datetime, watched: int) -> dict:
    token = [x for x in result.skipped if x.startswith("meta_token:")]
    if token and len(token) >= watched:
        return {"status": "skipped_token", "at": at}
    if token:
        return {"status": "partial", "at": at, "skipped": list(result.skipped)}
    return {"status": "ok", "at": at, "created": result.created, "auto_applied": result.auto_applied,
            "blocked": result.blocked, "stale": result.stale}


async def _claim(db, agent: dict, today_iso: str, now_utc: datetime) -> bool:
    """Atomic once-per-local-day claim, so overlapping ticks cannot both run the
    agent. A claim from today that is older than STALE_CLAIM never finished (a
    finished run stamps last_run_at, so the agent is no longer due): take it over,
    up to MAX_RUN_ATTEMPTS a day."""
    fresh = await db[AGENTS].find_one_and_update(
        {"_id": agent["_id"], "status": "active", "run_claimed_for": {"$ne": today_iso}},
        {"$set": {"run_claimed_for": today_iso, "run_claimed_at": now_utc, "run_attempts": 1}},
    )
    if fresh is not None:
        return True
    retry = await db[AGENTS].find_one_and_update(
        {"_id": agent["_id"], "status": "active", "run_claimed_for": today_iso,
         "run_claimed_at": {"$lt": now_utc - STALE_CLAIM},
         "$or": [{"run_attempts": {"$exists": False}}, {"run_attempts": {"$lt": MAX_RUN_ATTEMPTS}}]},
        {"$set": {"run_claimed_at": now_utc}, "$inc": {"run_attempts": 1}},
    )
    if retry is not None:
        logger.warning("agents-tick: retrying agent %s, its last run never finished", agent.get("_id"))
        return True
    # Out of attempts and still unfinished: record it, so the day isn't lost silently.
    at = datetime.utcnow()
    gave_up = await db[AGENTS].update_one(
        {"_id": agent["_id"], "run_claimed_for": today_iso, "run_attempts": {"$gte": MAX_RUN_ATTEMPTS},
         "run_claimed_at": {"$lt": now_utc - STALE_CLAIM}},
        {"$set": {"last_run": {"status": "error", "reason": "timed_out", "at": at}, "last_run_at": at}},
    )
    if gave_up.modified_count:
        logger.error("agents-tick: agent %s never finished in %d attempts today", agent.get("_id"),
                     MAX_RUN_ATTEMPTS)
    return False


async def run_due_agents(mongo_client, *, now_utc: datetime, deadline: float | None = None) -> dict:
    """`deadline` (time.monotonic()): past it, no new agent is claimed and the
    rest wait for the next tick."""
    now_utc = _aware_utc(now_utc)
    db = mongo_client[DB_NAME]
    out = {"ran": 0, "skipped": 0, "refreshed": 0, "errors": 0, "deferred": 0}

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() > deadline

    for agent in await db[AGENTS].find({"status": "active", "type": {"$ne": TYPE_STOCK}}).to_list(length=None):
        try:
            _schedule(agent)
        except ValueError as e:
            # Pre-validation docs may hold a bad schedule: skip this agent, don't record a run.
            logger.error("agents-tick: skipping agent %s: %s", agent.get("_id"), e)
            out["skipped"] += 1
            continue
        try:
            if not _is_due(agent, now_utc):
                continue
            groups = await _groups(db, agent)
            oldest = _oldest_refresh(groups)
            too_old = oldest is None or now_utc - oldest > MAX_DATA_AGE
            sched_utc, today = _scheduled_utc(agent, now_utc)
            if too_old and now_utc < sched_utc + GRACE:
                out["refreshed"] += await _enqueue_refreshes(db, mongo_client, groups, now_utc)
                out["skipped"] += 1
                continue

            if out_of_time():
                out["deferred"] += 1  # due, not claimed: the next tick runs it
                continue
            # A run that raises still records last_run_at (below), so a failing
            # agent is not retried the same day; only an unfinished one is.
            if not await _claim(db, agent, today.isoformat(), now_utc):
                out["skipped"] += 1
                continue

            result = await run_agent(db, mongo_client, agent, as_of=today, force_stale=too_old)
            at = datetime.utcnow()
            last_run = _last_run(result, at, len(groups))  # clients with a Meta account
            await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"last_run": last_run, "last_run_at": at}})
            out["ran"] += 1
            if out_of_time():
                logger.warning("agents-tick: out of time, proposal scan skipped for %s", agent.get("_id"))
                continue
            try:
                await learning.scan_proposals(db, mongo_client, agent, now=at)
            except Exception as e:  # a learning failure never fails the run
                logger.error("agents-tick: proposal scan failed for %s: %s", agent.get("_id"), e, exc_info=True)
        except Exception as e:
            logger.error("agents-tick: agent %s failed: %s", agent.get("_id"), e, exc_info=True)
            out["errors"] += 1
            try:
                at = datetime.utcnow()
                await db[AGENTS].update_one(
                    {"_id": agent["_id"]},
                    {"$set": {"last_run": {"status": "error", "at": at}, "last_run_at": at}})
            except Exception:
                logger.error("agents-tick: could not record error for %s", agent.get("_id"), exc_info=True)
    logger.info("[agents-tick] %s", out)
    return out


def _stock_last_run(run: dict, at: datetime) -> dict:
    summary = run.get("summary") or {}
    status = "skipped_token" if run.get("reconnect") else "partial" if summary.get("could_not_check") else "ok"
    return {"status": status, "at": at, "run_id": run["_id"], "kind": run.get("kind"),
            "cards": summary.get("cards", 0), "stale": bool(summary.get("stale"))}


async def _resume_previews(mongo_client, now_utc: datetime, deadline: float | None) -> None:
    """Finish previews a request left unfinished (draft agents aren't ticked otherwise). Stale ones
    end first. A preview never posts to Slack or Meta."""
    db = mongo_client[DB_NAME]
    for row in await db[stock_runner.RUNS].find(
            {"kind": stock_runner.PREVIEW, "phase": {"$ne": stock_runner.DONE}}).to_list(length=None):
        if deadline is not None and time.monotonic() > deadline:
            return
        try:
            agent = await db[AGENTS].find_one({"_id": row["agent_id"], "type": TYPE_STOCK})
            if agent is None:
                continue
            await stock_runner.end_stale_previews(db, agent, now_utc)
            run = await db[stock_runner.RUNS].find_one({"_id": row["_id"]})
            if run and run["phase"] != stock_runner.DONE:
                await stock_runner.advance_run(db, mongo_client, run, agent, deadline=deadline, now_utc=now_utc)
        except Exception as e:  # noqa: BLE001 - the preview records its own failures
            logger.error("agents-tick: preview %s failed: %s", row.get("_id"), e, exc_info=True)


async def run_due_stock_agents(mongo_client, *, now_utc: datetime, deadline: float | None = None) -> dict:
    """Start each active stock agent's run on time and resume unfinished ones, the agent advanced
    longest ago first, so one agency can't use the whole budget every tick. `deadline`
    (time.monotonic()): past it nothing new starts and the rest waits for the next tick."""
    now_utc = _aware_utc(now_utc)
    db = mongo_client[DB_NAME]
    out = {"ran": 0, "skipped": 0, "errors": 0, "deferred": 0}

    def out_of_time() -> bool:
        return deadline is not None and time.monotonic() > deadline

    await _resume_previews(mongo_client, now_utc, deadline)
    agents = await db[AGENTS].find({"status": "active", "type": TYPE_STOCK}).sort("stock_advanced_at", 1).to_list(length=None)
    for agent in agents:
        try:
            _schedule(agent)
        except ValueError as e:
            logger.error("agents-tick: skipping stock agent %s: %s", agent.get("_id"), e)
            out["skipped"] += 1
            continue
        try:
            sched_utc, today = _scheduled_utc(agent, now_utc)
            await stock_runner.end_overdue_runs(db, agent, now_utc)
            run = await stock_runner.find_open_run(db, agent["_id"])
            if run is None:
                if not _is_due(agent, now_utc):
                    continue
                run = await stock_runner.find_run(db, agent["_id"], today.isoformat())
            if out_of_time():
                out["deferred"] += 1
                continue
            if run is None:
                run = await stock_runner.get_or_create_run(db, agent, now_utc, scheduled_utc=sched_utc)
            if run["phase"] != stock_runner.DONE:
                await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"stock_advanced_at": datetime.utcnow()}})
                run = await stock_runner.advance_run(db, mongo_client, run, agent, deadline=deadline, now_utc=now_utc)
                # Still claimed means another tick holds it and this one did nothing.
                out["ran" if run.get("claimed_at") is None else "skipped"] += 1
            if run["phase"] == stock_runner.DONE:
                # A run finishing after local midnight belongs to the day it started, or the new
                # day's run would read as already done.
                at = now_utc.replace(tzinfo=None) if run["local_date"] == today.isoformat() else run["as_of"]
                await db[AGENTS].update_one({"_id": agent["_id"]},
                                            {"$set": {"last_run": _stock_last_run(run, at), "last_run_at": at}})
        except Exception as e:  # the run records its own failures; a retry comes next tick
            logger.error("agents-tick: stock agent %s failed: %s", agent.get("_id"), e, exc_info=True)
            out["errors"] += 1
    logger.info("[agents-tick] stock %s", out)
    return out


async def run_agents_tick() -> dict:
    deadline = time.monotonic() + TICK_BUDGET_SECONDS
    async with get_mongo_client() as mongo_client:
        out = await run_due_agents(mongo_client, now_utc=datetime.now(timezone.utc), deadline=deadline)
        try:
            stock = await run_due_stock_agents(mongo_client, now_utc=datetime.now(timezone.utc), deadline=deadline)
        except Exception as e:
            logger.error("[agents-tick] stock agents raised: %s", e, exc_info=True)
            stock = {"error": str(e)}
        return {**out, "stock": stock}


async def run_outcomes(mongo_client, now_utc: datetime) -> dict:
    """Measure the 7-day outcome of every applied agent action that is due one."""
    return await outcomes.run_outcomes(mongo_client, now_utc)


async def run_agents_outcomes() -> dict:
    async with get_mongo_client() as mongo_client:
        return await run_outcomes(mongo_client, datetime.now(timezone.utc))
