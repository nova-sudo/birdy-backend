"""
routers/cron.py
---------------
Vercel-cron-driven refresh endpoints.

Each endpoint is invoked by Vercel on a schedule defined in vercel.json,
authenticated via the `Authorization: Bearer ${CRON_SECRET}` header that
Vercel automatically injects on cron invocations.

The endpoints are deliberately thin — they orchestrate work that lives
in services/ and jobs/. Each call has a hard ~300s budget from Vercel's
serverless function timeout, so anything that exceeds that must be split
into resumable ticks (see meta_refresh_manager.execute_refresh's
max_seconds parameter).

Endpoints (Vercel crons invoke with GET):
    GET /api/cron/meta-tick         — every 1 min  (ongoing presets, 5h cadence)
    GET /api/cron/ghl-tick          — every 1 min  (GHL data, 1h cadence)
    GET /api/cron/meta-static-tick  — 1st of month (Last Month/Quarter/Year)
    GET /api/cron/ghl-tokens        — every 30 min (token rotation)
    GET /api/cron/alerts            — hourly       (alert evaluation)
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Header, HTTPException

from core.constants import META_ONGOING_PRESETS, META_STATIC_PRESETS
from core.database import DB_NAME
from dependencies import get_mongo_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cron", tags=["cron"])


# ---------------------------------------------------------------------------
# Tuning knobs
# ---------------------------------------------------------------------------

# How many groups to claim per tick (parallel within one Vercel invocation).
# Bounded by 300s function timeout / worst-case single-group runtime.
META_GROUPS_PER_TICK = 5
GHL_GROUPS_PER_TICK = 5

# Wall-clock budget passed to execute_refresh per claimed Meta job.
# Leaves headroom for the function's own startup/shutdown overhead.
META_BUDGET_PER_JOB_SECONDS = 250

# Cadence cutoffs — how stale a group must be before the tick picks it up.
META_ONGOING_CUTOFF_HOURS = 5
META_STATIC_CUTOFF_HOURS = 24 * 28  # ~monthly; static presets only change at period boundaries
GHL_CUTOFF_HOURS = 1

# Stale-claim recovery — a group stuck in "running" for longer than this is
# considered crashed and may be retried.
GHL_STALE_CLAIM_MINUTES = 10


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _verify_cron_auth(authorization: str | None):
    """
    Vercel injects `Authorization: Bearer ${CRON_SECRET}` on cron invocations
    when the CRON_SECRET env var is set in the project.
    """
    expected_secret = os.getenv("CRON_SECRET")
    if not expected_secret:
        # If unset, fail closed — production deploys MUST configure this.
        logger.error("CRON_SECRET env var is not set — refusing cron invocation")
        raise HTTPException(status_code=503, detail="CRON_SECRET not configured")
    if authorization != f"Bearer {expected_secret}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Meta refresh — 5-hour cadence for ongoing presets
# ---------------------------------------------------------------------------

@router.get("/meta-tick")
async def meta_tick(authorization: str | None = Header(default=None)):
    """
    Drives the resumable Meta refresh state machine. Runs every minute.

    Each tick does two things:
      1. Schedule: enqueue refresh jobs for groups whose Meta cache is
         older than META_ONGOING_CUTOFF_HOURS and don't already have an
         open job.
      2. Work: atomically claim up to N jobs (pending, partial, or
         crash-recovered) and advance them in parallel within a per-job
         wall-clock budget. Jobs that run out of budget yield "partial"
         and resume on the next tick.
    """
    _verify_cron_auth(authorization)

    from services.meta_refresh_manager import (
        schedule_stale_groups,
        claim_next_jobs,
        execute_refresh,
    )

    tick_start = time.monotonic()
    async with get_mongo_client() as mongo_client:
        # 1. Schedule new work
        scheduled = await schedule_stale_groups(
            mongo_client,
            cutoff_hours=META_ONGOING_CUTOFF_HOURS,
            presets=META_ONGOING_PRESETS,
            limit=20,
        )

        # 2. Claim and work
        jobs = await claim_next_jobs(mongo_client, n=META_GROUPS_PER_TICK)
        if jobs:
            await asyncio.gather(
                *[
                    execute_refresh(
                        j["job_id"],
                        mongo_client,
                        max_seconds=META_BUDGET_PER_JOB_SECONDS,
                    )
                    for j in jobs
                ],
                return_exceptions=True,  # one bad job doesn't take down the tick
            )

    elapsed = time.monotonic() - tick_start
    return {
        "ok": True,
        "scheduled": len(scheduled),
        "worked": len(jobs),
        "elapsed_seconds": round(elapsed, 2),
    }


@router.get("/meta-static-tick")
async def meta_static_tick(authorization: str | None = Header(default=None)):
    """
    Refreshes the static Meta presets (Last Month, Last Quarter, Last Year).
    Fires once a month on the 1st at 01:00 UTC — those windows only change
    at period boundaries, so a monthly sweep is sufficient.

    Uses the same state machine as meta-tick. Because this only fires once a
    month, we schedule everything that's stale and then work through it.
    Each invocation processes up to META_GROUPS_PER_TICK groups; if there
    are more, subsequent meta-tick invocations will pick up the leftovers
    (they share the same meta_refresh_jobs queue).
    """
    _verify_cron_auth(authorization)

    from services.meta_refresh_manager import (
        schedule_stale_groups,
        claim_next_jobs,
        execute_refresh,
    )

    tick_start = time.monotonic()
    async with get_mongo_client() as mongo_client:
        # Schedule every group that hasn't had a static refresh in ~28 days.
        # Higher limit because this only runs once a month.
        scheduled = await schedule_stale_groups(
            mongo_client,
            cutoff_hours=META_STATIC_CUTOFF_HOURS,
            presets=META_STATIC_PRESETS,
            limit=1000,
        )

        # Work on a few right now; meta-tick will mop up the rest over time.
        jobs = await claim_next_jobs(mongo_client, n=META_GROUPS_PER_TICK)
        if jobs:
            await asyncio.gather(
                *[
                    execute_refresh(
                        j["job_id"],
                        mongo_client,
                        max_seconds=META_BUDGET_PER_JOB_SECONDS,
                    )
                    for j in jobs
                ],
                return_exceptions=True,
            )

    elapsed = time.monotonic() - tick_start
    return {
        "ok": True,
        "scheduled": len(scheduled),
        "worked": len(jobs),
        "elapsed_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# GHL data refresh — 1-hour cadence (matches the legacy APScheduler)
# ---------------------------------------------------------------------------

@router.get("/ghl-tick")
async def ghl_tick(authorization: str | None = Header(default=None)):
    """
    Refreshes GHL data for the stalest groups. Runs every minute.

    GHL doesn't have a separate job state machine (it's faster per group
    than Meta), so this uses an inline claim via a status flag on the
    client_groups document:

      - Eligible: ghl_location_id set, last_ghl_refresh older than cutoff
                  (or never), and ghl_refresh_status != "running" (or
                  "running" but stuck for >10 min)
      - Claim:   $set ghl_refresh_status="running", _ghl_claimed_at=now
      - On success: ghl_refresh_status="complete", last_ghl_refresh=now
      - On failure: ghl_refresh_status="error" — existing cache stays put

    Data-preservation: fetch_and_cache_ghl_data_optimized writes per-preset
    via dot-notation (verified upstream), so a failure on one preset
    doesn't clobber siblings.
    """
    _verify_cron_auth(authorization)

    from services.ghl_service import fetch_and_cache_ghl_data_optimized

    tick_start = time.monotonic()
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        client_groups = db["client_groups"]
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=GHL_CUTOFF_HOURS)
        stale_lock_before = now - timedelta(minutes=GHL_STALE_CLAIM_MINUTES)

        # Atomically claim up to N groups
        claimed: list[dict] = []
        for _ in range(GHL_GROUPS_PER_TICK):
            doc = await client_groups.find_one_and_update(
                {
                    "ghl_location_id": {"$exists": True, "$ne": None},
                    "$or": [
                        {"last_ghl_refresh": {"$lt": cutoff}},
                        {"last_ghl_refresh": {"$exists": False}},
                        {"last_ghl_refresh": None},
                    ],
                    # Either not running, or running-but-stuck
                    "$and": [
                        {
                            "$or": [
                                {"ghl_refresh_status": {"$ne": "running"}},
                                {"_ghl_claimed_at": {"$lt": stale_lock_before}},
                                {"_ghl_claimed_at": {"$exists": False}},
                            ]
                        }
                    ],
                },
                {
                    "$set": {
                        "ghl_refresh_status": "running",
                        "_ghl_claimed_at": now,
                    }
                },
                sort=[("last_ghl_refresh", 1)],  # oldest first
                projection={
                    "id": 1, "user_id": 1, "name": 1,
                    "ghl_location_id": 1,
                },
            )
            if not doc:
                break
            claimed.append(doc)

        async def _refresh_one(group: dict):
            group_id = group["id"]
            user_id = group["user_id"]
            name = group.get("name", group_id)
            try:
                await fetch_and_cache_ghl_data_optimized(
                    group_id=group_id,
                    ghl_location_id=group["ghl_location_id"],
                    user_id=user_id,
                    mongo_client=mongo_client,
                    is_initial_load=True,
                )
                await client_groups.update_one(
                    {"id": group_id},
                    {
                        "$set": {
                            "ghl_refresh_status": "complete",
                            "last_ghl_refresh": datetime.utcnow(),
                        },
                        "$unset": {"_ghl_claimed_at": ""},
                    },
                )
                logger.info(f"[ghl-tick] OK '{name}'")
                return True
            except Exception as e:
                err = str(e)[:200]
                logger.error(f"[ghl-tick] FAIL '{name}': {err}")
                await client_groups.update_one(
                    {"id": group_id},
                    {
                        "$set": {
                            "ghl_refresh_status": "error",
                            "ghl_refresh_error": err,
                        },
                        "$unset": {"_ghl_claimed_at": ""},
                    },
                )
                return False

        if claimed:
            results = await asyncio.gather(
                *[_refresh_one(g) for g in claimed],
                return_exceptions=True,
            )
            ok_count = sum(1 for r in results if r is True)
        else:
            ok_count = 0

    elapsed = time.monotonic() - tick_start
    return {
        "ok": True,
        "claimed": len(claimed),
        "succeeded": ok_count,
        "elapsed_seconds": round(elapsed, 2),
    }


# ---------------------------------------------------------------------------
# GHL token rotation — 30-minute cadence
# ---------------------------------------------------------------------------

@router.get("/ghl-tokens")
async def ghl_tokens(authorization: str | None = Header(default=None)):
    """
    Regenerates GHL location tokens. Delegates to the existing job
    function so behavior stays identical to the legacy APScheduler path.
    """
    _verify_cron_auth(authorization)

    from jobs.ghl_jobs import refresh_tokens_for_all_users

    tick_start = time.monotonic()
    try:
        await refresh_tokens_for_all_users()
    except Exception as e:
        logger.error(f"[ghl-tokens] Refresh job raised: {e}", exc_info=True)
        # Still return 200 — Vercel retries failing crons aggressively and we
        # don't want a transient blip to spam-retry. The job logs its own errors.

    return {"ok": True, "elapsed_seconds": round(time.monotonic() - tick_start, 2)}


# ---------------------------------------------------------------------------
# Alert evaluation — hourly
# ---------------------------------------------------------------------------

@router.get("/alerts")
async def alerts(authorization: str | None = Header(default=None)):
    """
    Evaluates all active alerts. Delegates to the existing job function.
    """
    _verify_cron_auth(authorization)

    from jobs.alert_jobs import evaluate_all_alerts

    tick_start = time.monotonic()
    try:
        await evaluate_all_alerts()
    except Exception as e:
        logger.error(f"[alerts] Evaluation job raised: {e}", exc_info=True)

    return {"ok": True, "elapsed_seconds": round(time.monotonic() - tick_start, 2)}
