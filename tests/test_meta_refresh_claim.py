"""
tests/test_meta_refresh_claim.py
--------------------------------
Job claiming, and the crash-loop it used to permit.

A job that died mid-execution was reclaimed without counting the reclaim, so a
job that died *every* time was picked up forever: `attempt` stayed at 1,
MAX_ATTEMPTS was never reached, and nothing ever went terminal. Because
schedule_stale_groups skips any group holding an open job, the group silently
stopped refreshing while still reporting status "complete" — one sat frozen for
three months serving stale spend to the dashboard.

These pin the two properties that close it: crashes are counted, and a job that
burns its crash budget is swept out of the way instead of blocking its group.
"""

from datetime import datetime, timedelta

import pytest

from services.meta_refresh_manager import MAX_RECLAIMS, claim_next_jobs


def apply_pipeline(doc, pipeline):
    """Evaluate the tiny subset of aggregation-update syntax the claim uses."""
    def val(expr, d):
        if isinstance(expr, str) and expr.startswith("$"):
            return d.get(expr[1:])
        if not isinstance(expr, dict):
            return expr
        if "$ifNull" in expr:
            a, b = expr["$ifNull"]
            got = val(a, d)
            return b if got is None else got
        if "$cond" in expr:
            c, t, f = expr["$cond"]
            return val(t, d) if val(c, d) else val(f, d)
        if "$eq" in expr:
            a, b = expr["$eq"]
            return val(a, d) == val(b, d)
        if "$ne" in expr:
            a, b = expr["$ne"]
            return val(a, d) != val(b, d)
        if "$and" in expr:
            return all(val(x, d) for x in expr["$and"])
        if "$add" in expr:
            return sum(val(x, d) for x in expr["$add"])
        raise AssertionError(f"unsupported expr {expr}")

    for stage in pipeline:
        # Mongo evaluates every expression in a $set stage against the stage's
        # INPUT document, so `$status` is the pre-claim status even though the
        # same stage overwrites it. Snapshot first, then assign.
        snapshot = dict(doc)
        for k, v in stage["$set"].items():
            doc[k] = val(v, snapshot)
    return doc


class FakeJobs:
    def __init__(self, jobs):
        self.jobs = jobs
        self.swept = []

    async def update_many(self, filt, update):
        n = 0
        for j in self.jobs:
            if (j.get("status") == filt["status"]
                    and j.get("_claimed_at") is not None
                    and j["_claimed_at"] < filt["_claimed_at"]["$lt"]
                    and j.get("_reclaim_count", 0) >= MAX_RECLAIMS):
                j.update(update["$set"])
                self.swept.append(j["job_id"])
                n += 1

        class R:
            modified_count = n
        return R()

    async def find_one_and_update(self, filt, update, sort=None):
        for j in self.jobs:
            if j.get("status") != "in_progress":
                continue
            if j.get("_reclaim_count", 0) >= MAX_RECLAIMS:
                continue          # reason 4 excludes a spent crash budget
            before = dict(j)
            apply_pipeline(j, update)
            return before
        return None


class FakeDb:
    def __init__(self, jobs):
        self._jobs = jobs

    def __getitem__(self, _n):
        return self._jobs


class FakeMongo:
    def __init__(self, jobs):
        self._db = FakeDb(jobs)

    def __getitem__(self, _n):
        return self._db


def stuck_job(reclaims=0, age_minutes=60):
    return {
        "job_id": "j1",
        "group_id": "g1",
        "status": "in_progress",
        "attempt": 1,
        "max_attempts": 3,
        "_claimed_at": datetime.utcnow() - timedelta(minutes=age_minutes),
        "_reclaim_count": reclaims,
    }


@pytest.mark.asyncio
async def test_reclaiming_a_crashed_job_counts_the_crash():
    """The bug: this counter did not exist, so the loop had no end."""
    jobs = FakeJobs([stuck_job(reclaims=0)])

    await claim_next_jobs(FakeMongo(jobs), 1)

    assert jobs.jobs[0]["_reclaim_count"] == 1


@pytest.mark.asyncio
async def test_a_crash_does_not_consume_the_step_retry_budget():
    """`attempt` tracks steps that ran and errored. A crash says nothing about
    the steps, so it must not eat into that budget — the two are separate."""
    jobs = FakeJobs([stuck_job(reclaims=1)])

    await claim_next_jobs(FakeMongo(jobs), 1)

    assert jobs.jobs[0]["attempt"] == 1
    assert jobs.jobs[0]["_reclaim_count"] == 2


@pytest.mark.asyncio
async def test_a_job_that_burns_its_crash_budget_is_swept_to_failed():
    """Left in_progress it would block schedule_stale_groups forever, which is
    how a group goes months without a refresh while reporting 'complete'."""
    jobs = FakeJobs([stuck_job(reclaims=MAX_RECLAIMS)])

    await claim_next_jobs(FakeMongo(jobs), 1)

    assert jobs.jobs[0]["status"] == "failed"
    assert jobs.swept == ["j1"]
    assert "giving up" in jobs.jobs[0]["error"]


@pytest.mark.asyncio
async def test_the_loop_terminates():
    """The property that actually matters: repeated crashes end. Before the
    fix this ran forever."""
    jobs = FakeJobs([stuck_job(reclaims=0)])

    for _ in range(MAX_RECLAIMS + 2):
        await claim_next_jobs(FakeMongo(jobs), 1)
        # Every claim dies mid-execution and ages back out.
        if jobs.jobs[0]["status"] == "in_progress":
            jobs.jobs[0]["_claimed_at"] = datetime.utcnow() - timedelta(minutes=60)

    assert jobs.jobs[0]["status"] == "failed"
