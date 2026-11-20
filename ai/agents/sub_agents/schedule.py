"""
ai/agents/sub_agents/schedule.py
--------------------------------
Scheduled runs for specialist sub-agents: which are due, a once-per-local-day claim, and the record
of how the run went. NOT wired into the 15-minute tick (jobs/agent_jobs.run_agents_tick) yet.

Why not: a specialist's run is a model run with its own instructions, tools and lessons, scoped to its
clients. That is the nested, bounded chat run the agent-hierarchy branch is building in the
orchestrator (no second session save, no archive row, its own credit line). This module has
everything the tick needs except that call, which comes in as `run_one`:

    async def run_one(db, mongo_client, sub_agent: dict, *, local_date: str, deadline: float | None) -> dict
        # returns {"status": "ok" | "error" | "skipped", "summary": str?, "credits": float?, ...}

Wiring, once the runner exists, is one line in run_agents_tick (after the stock agents, inside the
same deadline):  await run_due_sub_agents(mongo_client, now_utc=..., deadline=deadline, run_one=...)

Due = active, has a schedule, today (local) is one of its days, local now >= its time, and no run
for today's local date is finished. Same time handling as the custom agents (jobs/agent_jobs: DST gap
lands on the first valid instant after it). A claim that never finished goes stale after STALE_CLAIM
and is retried, at most MAX_RUN_ATTEMPTS times a day; then the day is recorded as an error.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ai.agents.sub_agents.core import ACTIVE, DAYS, KIND_SPECIALIST, SUB_AGENTS, _normalize

logger = logging.getLogger(__name__)

RUNS = "sub_agent_runs"
STALE_CLAIM = timedelta(minutes=30)
MAX_RUN_ATTEMPTS = 3
CLAIMED, DONE, ERROR = "claimed", "done", "error"


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _as_agent(sub: dict) -> dict:
    """The custom-agent shape jobs/agent_jobs' schedule helpers read."""
    sched = sub.get("schedule") or {}
    return {"scope": {"schedule": {"time": sched.get("time"), "tz": sched.get("timezone")}}}


def scheduled_utc(sub: dict, now_utc: datetime):
    """(today's scheduled instant in UTC, today's local date) for a sub-agent with a schedule."""
    from jobs.agent_jobs import _scheduled_utc
    return _scheduled_utc(_as_agent(sub), _aware_utc(now_utc))


def is_due(sub: dict, now_utc: datetime, *, done_today: bool = False) -> bool:
    """Pure check, no database. `done_today`: a run for today's local date already finished."""
    if sub.get("status") != ACTIVE or sub.get("kind", KIND_SPECIALIST) != KIND_SPECIALIST:
        return False
    sched = sub.get("schedule")
    if not sched or not sched.get("time"):
        return False
    try:
        sched_utc, today = scheduled_utc(sub, now_utc)
    except ValueError:
        return False
    if DAYS[today.weekday()] not in (sched.get("days") or DAYS):
        return False
    return _aware_utc(now_utc) >= sched_utc and not done_today


async def due_sub_agents(db, now_utc: datetime) -> list[tuple[dict, str]]:
    """[(stored sub-agent doc, local date "YYYY-MM-DD")] for every specialist due now, longest since
    its last run first."""
    out = []
    docs = await db[SUB_AGENTS].find(
        {"status": ACTIVE, "kind": KIND_SPECIALIST, "schedule": {"$ne": None}}).to_list(length=None)
    for doc in docs:
        try:
            _, today = scheduled_utc(doc, now_utc)
        except ValueError:
            logger.error("sub-agents: bad schedule on %s", doc.get("_id"))
            continue
        local_date = today.isoformat()
        run = await db[RUNS].find_one({"sub_agent_id": doc["_id"], "local_date": local_date})
        finished = bool(run and run.get("status") in (DONE, ERROR))
        if is_due(doc, now_utc, done_today=finished):
            out.append((doc, local_date))
    out.sort(key=lambda p: _aware_utc(p[0].get("last_run_at") or datetime.min.replace(tzinfo=timezone.utc)))
    return out


async def claim_run(db, sub: dict, local_date: str, now_utc: datetime) -> dict | None:
    """Take today's run for this sub-agent, so two overlapping ticks can't both run it. Returns the
    run row, or None (finished, held by another tick, or out of attempts: then the day is closed)."""
    now = _aware_utc(now_utc).replace(tzinfo=None)
    row = {"_id": str(uuid.uuid4()), "sub_agent_id": sub["_id"], "user_id": sub["user_id"],
           "local_date": local_date, "status": CLAIMED, "claimed_at": now, "attempts": 1,
           "created_at": now}
    if await db[RUNS].find_one({"sub_agent_id": sub["_id"], "local_date": local_date}, {"_id": 1}) is None:
        try:
            await db[RUNS].insert_one(row)  # the unique (sub_agent_id, local_date) index settles a race
            return row
        except DuplicateKeyError:
            return None
    retry = await db[RUNS].find_one_and_update(
        {"sub_agent_id": sub["_id"], "local_date": local_date, "status": CLAIMED,
         "claimed_at": {"$lt": now - STALE_CLAIM}, "attempts": {"$lt": MAX_RUN_ATTEMPTS}},
        {"$set": {"claimed_at": now}, "$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
    if retry is not None:
        logger.warning("sub-agents: retrying %s, its last run never finished", sub["_id"])
        return retry
    gave_up = await db[RUNS].update_one(
        {"sub_agent_id": sub["_id"], "local_date": local_date, "status": CLAIMED,
         "claimed_at": {"$lt": now - STALE_CLAIM}, "attempts": {"$gte": MAX_RUN_ATTEMPTS}},
        {"$set": {"status": ERROR, "result": {"status": "error", "reason": "timed_out"}, "finished_at": now}})
    if gave_up.modified_count:
        await db[SUB_AGENTS].update_one({"_id": sub["_id"]}, {"$set": {"last_run_at": now}})
    return None


async def finish_run(db, run: dict, result: dict, now_utc: datetime) -> None:
    now = _aware_utc(now_utc).replace(tzinfo=None)
    status = ERROR if (result or {}).get("status") == "error" else DONE
    await db[RUNS].update_one({"_id": run["_id"]},
                              {"$set": {"status": status, "result": result or {}, "finished_at": now}})
    await db[SUB_AGENTS].update_one({"_id": run["sub_agent_id"]},
                                    {"$set": {"last_run_at": now, "last_run": {**(result or {}), "at": now}}})


async def run_due_sub_agents(mongo_client, *, now_utc: datetime, run_one, deadline: float | None = None) -> dict:
    """Run every due specialist once today through `run_one` (the delegation branch's nested run,
    see the module note). Past `deadline` (time.monotonic()) nothing new is claimed."""
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    out = {"ran": 0, "skipped": 0, "errors": 0, "deferred": 0}
    for doc, local_date in await due_sub_agents(db, now_utc):
        if deadline is not None and time.monotonic() > deadline:
            out["deferred"] += 1
            continue
        run = await claim_run(db, doc, local_date, now_utc)
        if run is None:
            out["skipped"] += 1
            continue
        # Paused or deleted between the scan and the claim: don't run it.
        fresh = await db[SUB_AGENTS].find_one({"_id": doc["_id"], "status": ACTIVE})
        if fresh is None:
            await db[RUNS].delete_one({"_id": run["_id"]})
            out["skipped"] += 1
            continue
        try:
            result = await run_one(db, mongo_client, _normalize(fresh), local_date=local_date, deadline=deadline)
            out["ran"] += 1
        except Exception as e:
            logger.error("sub-agents: %s failed: %s", doc["_id"], e, exc_info=True)
            result = {"status": "error", "reason": "exception"}
            out["errors"] += 1
        await finish_run(db, run, result, datetime.now(timezone.utc))
    return out
