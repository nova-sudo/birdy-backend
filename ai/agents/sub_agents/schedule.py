"""
ai/agents/sub_agents/schedule.py
--------------------------------
Scheduled runs for specialist sub-agents: which are due, a once-per-local-day claim, and the record
of how the run went. jobs/agent_jobs.run_agents_tick calls run_due_sub_agents after the stock agents,
inside the same deadline, with ai/team/scheduled.run_one (the nested, bounded agent run). The owner
can also run one now (start_manual_run, POST /api/agents/sub/{id}/run): the same run_one, stored as its
own row (local_date "manual:<run id>", trigger "manual"), so it never counts as the day's scheduled run.

Due = active, has a schedule, today (local) is one of its days, local now >= its time, and no run
for today's local date is finished. Same time handling as the custom agents (jobs/agent_jobs: DST gap
lands on the first valid instant after it). A claim that never finished goes stale after STALE_CLAIM
and is retried, at most MAX_RUN_ATTEMPTS times a day; then the day is recorded as an error.

A run row: {_id, sub_agent_id, user_id, local_date, trigger, status claimed|done|error, claimed_at,
finished_at, attempts, result {status, summary, answer, credits, clients, reason?}, proposals [...]}.
Its proposals are pending until the owner approves or dismisses them (routers/chat.py); nothing in a
run is ever applied by itself.
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
MIN_RUN_SECONDS = 60                   # a run is not started with less of the tick left (ai/team/scheduled)
MANUAL = "manual"
RUNS_LIMIT_MAX = 100
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


LAST_RUN_KEYS = ("status", "summary", "credits")


async def finish_run(db, run: dict, result: dict, now_utc: datetime) -> None:
    now = _aware_utc(now_utc).replace(tzinfo=None)
    result = dict(result or {})
    proposals = [dict(p, status=p.get("status") or "pending") for p in result.pop("proposals", None) or []]
    status = ERROR if result.get("status") == "error" else DONE
    await db[RUNS].update_one({"_id": run["_id"]},
                              {"$set": {"status": status, "result": result, "finished_at": now,
                                        "proposals": proposals}})
    last = {k: result.get(k) for k in LAST_RUN_KEYS}
    last["status"] = last["status"] or ("error" if status == ERROR else "done")
    await db[SUB_AGENTS].update_one({"_id": run["sub_agent_id"]},
                                    {"$set": {"last_run_at": now, "last_run": {**last, "at": now}}})


async def run_due_sub_agents(mongo_client, *, now_utc: datetime, run_one, deadline: float | None = None) -> dict:
    """Run every due specialist once today through `run_one` (the delegation branch's nested run,
    see the module note). Past `deadline` (time.monotonic()) nothing new is claimed."""
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    out = {"ran": 0, "skipped": 0, "errors": 0, "deferred": 0}
    for doc, local_date in await due_sub_agents(db, now_utc):
        if deadline is not None and deadline - time.monotonic() < MIN_RUN_SECONDS:
            # Not enough of the tick left for a whole run: the next tick takes it.
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


# ---------------------------------------------------------------------------
# The owner's own runs, and the runs list
# ---------------------------------------------------------------------------

def run_api(row: dict) -> dict:
    """A run as the app gets it: {id, status, started_at, finished_at, summary, answer, credits,
    proposals}. status: running | done | error | skipped."""
    from ai.agents.sub_agents.core import _iso_dt
    result = row.get("result") or {}
    if row.get("status") == CLAIMED:
        status = "running"
    elif row.get("status") == ERROR:
        status = "error"
    else:
        status = result.get("status") if result.get("status") in ("done", "skipped", "error") else "done"
    return {"id": row["_id"], "status": status, "trigger": row.get("trigger") or "schedule",
            "started_at": _iso_dt(row.get("claimed_at") or row.get("created_at")),
            "finished_at": _iso_dt(row.get("finished_at")), "summary": result.get("summary"),
            "answer": result.get("answer"), "credits": result.get("credits"),
            "proposals": [dict(p, can_undo=False) for p in row.get("proposals") or []]}


async def list_runs(db, user_id: str, sub_agent_id: str, limit: int = 20) -> list[dict]:
    """A sub-agent's runs, newest first (a custom rules agent has none here)."""
    from ai.agents.sub_agents.core import _not_found, get_sub_agent
    if await get_sub_agent(db, user_id, sub_agent_id) is None:
        raise _not_found()
    limit = max(1, min(int(limit or 20), RUNS_LIMIT_MAX))
    rows = await (db[RUNS].find({"sub_agent_id": sub_agent_id, "user_id": user_id})
                  .sort("claimed_at", -1).limit(limit).to_list(length=limit))
    return [run_api(r) for r in rows]


async def start_manual_run(db, mongo_client, user_id: str, sub_agent_id: str, *, run_one,
                           budget_seconds: float) -> dict:
    """The owner presses Run: the same run_one as the schedule, now. Returns the finished run (run_api).
    Raises SubAgentError: 404 not found, 409 managed_elsewhere (a custom rules agent), 409 wrong_status
    (not active), 409 already_running, 402 out_of_credits."""
    import credits
    from ai.agents.sub_agents.core import SubAgentError, _specialist_or_raise
    doc = await _specialist_or_raise(db, user_id, sub_agent_id)
    if doc.get("status") != ACTIVE:
        raise SubAgentError(409, "wrong_status", "Only an active sub-agent can run")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if await db[RUNS].find_one({"sub_agent_id": sub_agent_id, "status": CLAIMED,
                                "claimed_at": {"$gte": now - STALE_CLAIM}}, {"_id": 1}):
        raise SubAgentError(409, "already_running", "This sub-agent is running now")
    if await credits.is_blocked(db, user_id):
        raise SubAgentError(402, "out_of_credits", "You are out of Birdy Credits")
    run_id = str(uuid.uuid4())
    row = {"_id": run_id, "sub_agent_id": sub_agent_id, "user_id": user_id, "local_date": f"{MANUAL}:{run_id}",
           "trigger": MANUAL, "status": CLAIMED, "claimed_at": now, "attempts": 1, "created_at": now}
    await db[RUNS].insert_one(row)
    from ai.team.scheduled import today_for
    sub = _normalize(doc)
    try:
        result = await run_one(db, mongo_client, sub, local_date=today_for(sub),
                               deadline=time.monotonic() + budget_seconds)
    except Exception as e:
        logger.error("sub-agents: manual run of %s failed: %s", sub_agent_id, e, exc_info=True)
        result = {"status": "error", "reason": "exception", "summary": "Could not finish"}
    await finish_run(db, row, result, datetime.now(timezone.utc))
    return run_api(await db[RUNS].find_one({"_id": run_id}))
