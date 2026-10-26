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
"""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.agents.runner import run_agent
from ai.agents.store import AGENTS
from core.database import DB_NAME
from dependencies import get_mongo_client

logger = logging.getLogger(__name__)

MAX_DATA_AGE = timedelta(hours=3)
GRACE = timedelta(hours=2)
FRESHNESS_FIELD = "last_meta_refresh"
IN_FLIGHT = ("pending", "in_progress")


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _schedule(agent: dict) -> tuple[int, int, ZoneInfo]:
    sched = (agent.get("scope") or {}).get("schedule") or {}
    hh, mm = (sched.get("time") or "07:00").split(":")
    return int(hh), int(mm), ZoneInfo(sched.get("tz") or "Europe/London")


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


async def run_due_agents(mongo_client, *, now_utc: datetime) -> dict:
    now_utc = _aware_utc(now_utc)
    db = mongo_client[DB_NAME]
    out = {"ran": 0, "skipped": 0, "refreshed": 0, "errors": 0}

    for agent in await db[AGENTS].find({"status": "active"}).to_list(length=None):
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

            result = await run_agent(db, mongo_client, agent, as_of=today, force_stale=too_old)
            at = datetime.utcnow()
            last_run = _last_run(result, at, len((agent.get("scope") or {}).get("client_group_ids") or []))
            await db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"last_run": last_run, "last_run_at": at}})
            out["ran"] += 1
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


async def run_agents_tick() -> dict:
    async with get_mongo_client() as mongo_client:
        return await run_due_agents(mongo_client, now_utc=datetime.now(timezone.utc))
