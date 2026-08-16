"""
services/meta_refresh_manager.py
---------------------------------
Resilient Meta refresh orchestrator with granular per-preset tracking
and automatic retry of failed steps.

Two execution models share this module:

  1. Inline (on-demand)  — start_refresh() creates a job and runs it to
     completion in the same coroutine. Used by the on-demand refresh
     endpoint and by group creation.

  2. Tick-driven (cron)  — schedule_stale_groups() creates jobs for
     stale groups, claim_next_jobs() atomically claims work, and
     execute_refresh(max_seconds=...) advances each job within a
     wall-clock budget. The Vercel /api/cron/meta-tick endpoint
     drives this.

Both share the same meta_refresh_jobs state machine and per-preset
status tracking, so a partial job created inline can be resumed by a
tick (and vice-versa).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from core.constants import META_CACHE_PRESETS
from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_service import (
    MetaAuthError,
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)
from integrations.facebook_utils.facebook_leads import (
    fetch_and_cache_facebook_leads_FIXED,
)

logger = logging.getLogger(__name__)

RETRY_DELAY_MINUTES = 10
MAX_ATTEMPTS = 3

# A job that has been in_progress for longer than this is considered stuck
# (function crash, OOM, network drop) and may be reclaimed by the next tick.
STALE_CLAIM_MINUTES = 10

# Minimum remaining budget required to start the leads phase. Leads can be
# slow, so if we're below this we skip and let the next tick handle it.
LEADS_MIN_BUDGET_SECONDS = 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_refresh_job(
    group_id: str,
    user_id: str,
    group: dict,
    mongo_client,
    presets: list[str] | None = None,
    status: str = "pending",
) -> str:
    """
    Insert a meta_refresh_jobs document for the group. Does NOT execute
    any fetches — the caller is responsible for either:
      - calling execute_refresh() inline (start_refresh does this), or
      - letting the cron tick claim and advance it.

    Args:
        presets: list of preset keys to refresh. Defaults to all presets.
                 Pass META_ONGOING_PRESETS for the 5-hour cron, or
                 META_STATIC_PRESETS for the monthly cron.
        status:  initial job status. "pending" leaves it for the tick to
                 claim; "in_progress" claims it immediately for inline use.

    Returns the job_id.
    """
    if presets is None:
        presets = META_CACHE_PRESETS

    db = mongo_client[DB_NAME]
    now = datetime.utcnow()
    job_id = f"refresh_{group_id}_{int(now.timestamp())}"

    ad_account_id = group.get("meta_ad_account_id")
    currency = group.get("ad_account_currency")

    # Build initial job document with the requested presets pending
    presets_status = {
        preset: {"status": "pending", "attempt": 0, "error": None}
        for preset in presets
    }

    job_doc = {
        "job_id": job_id,
        "group_id": group_id,
        "user_id": user_id,
        "ad_account_id": ad_account_id,
        "currency": currency,
        "group_name": group.get("name", ""),
        "status": status,
        "created_at": now,
        "updated_at": now,
        "attempt": 1,
        "max_attempts": MAX_ATTEMPTS,
        "next_retry_at": None,
        "_claimed_at": now if status == "in_progress" else None,
        "preset_keys": presets,  # for observability
        "steps": {
            "presets": presets_status,
            "leads": {"status": "pending", "attempt": 0, "error": None},
            "lead_counts": {"status": "pending", "attempt": 0, "error": None},
        },
    }

    await db["meta_refresh_jobs"].insert_one(job_doc)

    # Mark as refreshing without clobbering existing preset data — if the
    # refresh fails partway, stale-but-valid data must remain in place.
    # Per-preset writes use dot notation, so successful presets overwrite
    # cleanly while failed ones keep their previous values.
    await db["client_groups"].update_one(
        {"id": group_id},
        {"$set": {
            "facebook_cache._refreshing": True,
            "meta_refresh_status": "running",
        }},
    )

    logger.info(
        f"[{job_id}] Created Meta refresh job for '{group.get('name', group_id)}' "
        f"({len(presets)} presets, status={status})"
    )
    return job_id


async def start_refresh(
    group_id: str,
    user_id: str,
    group: dict,
    mongo_client,
    presets: list[str] | None = None,
) -> str:
    """
    Inline path: create a job and execute it to completion in the same
    coroutine. Used by:
      - on-demand POST /api/client-groups/{id}/refresh/meta
      - group creation in routers/client_groups.py

    For the cron-driven path, use create_refresh_job + the tick orchestrator.
    """
    job_id = await create_refresh_job(
        group_id, user_id, group, mongo_client,
        presets=presets, status="in_progress",
    )
    await execute_refresh(job_id, mongo_client)
    return job_id


async def execute_refresh(
    job_id: str,
    mongo_client,
    max_seconds: int | None = None,
):
    """
    Execute or resume a refresh job. Only attempts steps that aren't already "success".

    Args:
        max_seconds: optional wall-clock budget. If provided, the function
                     returns early once exceeded — successful presets so far
                     are persisted, remaining work stays "pending", and the
                     job is left in "partial" state for the next tick.
                     When None (inline mode), runs to completion.
    """
    db = mongo_client[DB_NAME]
    jobs_col = db["meta_refresh_jobs"]

    start_time = time.monotonic()

    def remaining_budget() -> float:
        if max_seconds is None:
            return float("inf")
        return max_seconds - (time.monotonic() - start_time)

    def out_of_budget() -> bool:
        return remaining_budget() <= 0

    job = await jobs_col.find_one({"job_id": job_id})
    if not job:
        logger.error(f"[{job_id}] Job not found")
        return

    group_id = job["group_id"]
    user_id = job["user_id"]
    ad_account_id = job["ad_account_id"]
    currency = job["currency"]
    steps = job["steps"]

    async def yield_partial(reason: str):
        """Save current progress as 'partial' and exit without finalizing.

        Tags this partial with _partial_kind="budget" so claim_next_jobs
        knows the yield was due to hitting the tick's wall-clock budget
        (a normal, expected event on large accounts), NOT a real failure.
        Budget-partials don't consume the attempt budget and are eligible
        for immediate reclaim on the next tick.
        """
        await jobs_col.update_one(
            {"job_id": job_id},
            {"$set": {
                "status": "partial",
                "_partial_kind": "budget",
                "_claimed_at": None,
                "next_retry_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }},
        )
        # Keep client_group in "running" so UI knows refresh is still ongoing
        await db["client_groups"].update_one(
            {"id": group_id},
            {"$set": {"meta_refresh_status": "running"}},
        )
        logger.info(f"[{job_id}] Yielding partial ({reason}) — next tick will resume")

    # Validate token first
    token = await get_facebook_token(user_id, mongo_client)
    if not token or not token.get("access_token"):
        logger.error(f"[{job_id}] No valid Facebook token")
        await _mark_all_auth_error(jobs_col, job_id, "No Facebook token", db=db, group_id=group_id)
        await _finalize_job(db, jobs_col, job_id, group_id)
        return

    # ── PHASE 1: Presets ─────────────────────────────────────────────────
    auth_failed = False
    for preset_key, preset_state in steps["presets"].items():
        if preset_state["status"] == "success":
            continue  # already done

        # Budget check before starting next preset
        if out_of_budget():
            await yield_partial(f"budget exhausted before '{preset_key}'")
            return

        logger.info(f"[{job_id}] Fetching preset '{preset_key}' (attempt {preset_state['attempt'] + 1})")

        try:
            await fetch_meta_all_presets_for_group(
                group_id=group_id,
                meta_ad_account_id=ad_account_id,
                user_id=user_id,
                mongo_client=mongo_client,
                ad_account_currency=currency,
                presets=[preset_key],
            )

            # Success
            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    f"steps.presets.{preset_key}.status": "success",
                    f"steps.presets.{preset_key}.attempt": preset_state["attempt"] + 1,
                    f"steps.presets.{preset_key}.error": None,
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.info(f"[{job_id}] Preset '{preset_key}' ✅")

        except MetaAuthError as e:
            # Typed auth error path — the swallow-friendly string-matching
            # fallback below is still there for pre-existing raise-Exception
            # code, but MetaAuthError is the definitive signal.
            error_msg = str(e)[:200]
            logger.error(f"[{job_id}] Auth/permission error on '{preset_key}': {error_msg}")
            await _mark_all_auth_error(jobs_col, job_id, error_msg, db=db, group_id=group_id)
            auth_failed = True
            break

        except Exception as e:
            error_msg = str(e)[:200]
            is_auth = "auth" in error_msg.lower() or "permission" in error_msg.lower() or "token" in error_msg.lower()
            is_rate_limit = "rate" in error_msg.lower() or "429" in error_msg

            if is_auth:
                logger.error(f"[{job_id}] Auth/permission error on '{preset_key}': {error_msg}")
                await _mark_all_auth_error(jobs_col, job_id, error_msg, db=db, group_id=group_id)
                auth_failed = True
                break
            elif is_rate_limit:
                new_status = "rate_limited"
                logger.warning(f"[{job_id}] Rate limited on '{preset_key}', will retry later")
            else:
                new_status = "failed"
                logger.error(f"[{job_id}] Error on '{preset_key}': {error_msg}")

            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    f"steps.presets.{preset_key}.status": new_status,
                    f"steps.presets.{preset_key}.attempt": preset_state["attempt"] + 1,
                    f"steps.presets.{preset_key}.error": error_msg,
                    "updated_at": datetime.utcnow(),
                }},
            )

        # 5s cooldown between presets (Meta rate limit safety)
        await asyncio.sleep(5)

    if auth_failed:
        await _finalize_job(db, jobs_col, job_id, group_id)
        return

    # ── PHASE 2: Leads ───────────────────────────────────────────────────
    # Reload job to get updated state
    job = await jobs_col.find_one({"job_id": job_id})
    steps = job["steps"]

    if steps["leads"]["status"] != "success":
        # Leads can be slow — skip if we don't have enough budget left
        if remaining_budget() < LEADS_MIN_BUDGET_SECONDS:
            await yield_partial(
                f"insufficient budget for leads "
                f"({remaining_budget():.0f}s < {LEADS_MIN_BUDGET_SECONDS}s)"
            )
            return

        logger.info(f"[{job_id}] Fetching leads...")
        await jobs_col.update_one(
            {"job_id": job_id},
            {"$set": {
                "steps.leads.status": "in_progress",
                "updated_at": datetime.utcnow(),
            }},
        )

        # Fresh sync only on the very first refresh for this client group
        # (or after a manual full-resync reset). Every subsequent refresh
        # runs incrementally against the newest-lead watermark — see
        # integrations/facebook_utils/facebook_leads.py::fetch_and_cache_facebook_leads_FIXED
        # for the two code paths and integrations/facebook_utils/
        # meta_incremental_refresh.py for the watermark logic. Without this
        # flag lookup, execute_refresh used to hard-code is_initial_load=True
        # and re-fetched every ad's full lead history every 5 hours.
        group_doc = await db["client_groups"].find_one(
            {"id": group_id},
            {"leads_initial_sync_done": 1},
        )
        is_initial_load = not (group_doc or {}).get("leads_initial_sync_done")
        logger.info(
            f"[{job_id}] Leads mode: "
            f"{'INITIAL (full history)' if is_initial_load else 'INCREMENTAL (since watermark)'}"
        )

        try:
            await fetch_and_cache_facebook_leads_FIXED(
                ad_account_currency=currency,
                group_id=group_id,
                meta_ad_account_id=ad_account_id,
                user_id=user_id,
                mongo_client=mongo_client,
                is_initial_load=is_initial_load,
            )

            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "steps.leads.status": "success",
                    "steps.leads.attempt": steps["leads"]["attempt"] + 1,
                    "steps.leads.error": None,
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.info(f"[{job_id}] Leads ✅")

        except Exception as e:
            error_msg = str(e)[:200]
            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "steps.leads.status": "failed",
                    "steps.leads.attempt": steps["leads"]["attempt"] + 1,
                    "steps.leads.error": error_msg,
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.error(f"[{job_id}] Leads failed: {error_msg}")

    # ── PHASE 3: Lead counts ─────────────────────────────────────────────
    job = await jobs_col.find_one({"job_id": job_id})
    steps = job["steps"]

    if steps["lead_counts"]["status"] != "success":
        if out_of_budget():
            await yield_partial("budget exhausted before lead_counts")
            return

        try:
            await update_preset_lead_counts(
                group_id=group_id,
                user_id=user_id,
                mongo_client=mongo_client,
            )

            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "steps.lead_counts.status": "success",
                    "steps.lead_counts.attempt": steps["lead_counts"]["attempt"] + 1,
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.info(f"[{job_id}] Lead counts ✅")

        except Exception as e:
            error_msg = str(e)[:200]
            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "steps.lead_counts.status": "failed",
                    "steps.lead_counts.attempt": steps["lead_counts"]["attempt"] + 1,
                    "steps.lead_counts.error": error_msg,
                    "updated_at": datetime.utcnow(),
                }},
            )
            logger.error(f"[{job_id}] Lead counts failed: {error_msg}")

    # ── FINALIZE ─────────────────────────────────────────────────────────
    await _finalize_job(db, jobs_col, job_id, group_id)


# NOTE: `retry_pending_jobs` used to live here as the APScheduler-driven
# retry loop. It's been deleted:
#   - It was the ONLY caller that incremented job["attempt"], so with
#     APScheduler disabled on Vercel (see main.py) the counter never
#     advanced, and MAX_ATTEMPTS was never actually enforced.
#   - The Vercel path is claim_next_jobs → execute_refresh, which now
#     handles attempt-bumping inline for failure-partial claims.
# If APScheduler is ever re-enabled for local/on-prem, the new claim path
# still works — no scheduler-side loop needed.


async def get_refresh_progress(group_id: str, mongo_client) -> dict | None:
    """
    Get the latest refresh job progress for a group.
    Returns a summary dict for the /refresh-status endpoint.
    """
    db = mongo_client[DB_NAME]
    job = await db["meta_refresh_jobs"].find_one(
        {"group_id": group_id},
        sort=[("created_at", -1)],
    )

    if not job:
        return None

    steps = job.get("steps", {})
    presets = steps.get("presets", {})

    presets_done = sum(1 for p in presets.values() if p.get("status") == "success")
    presets_failed = sum(1 for p in presets.values() if p.get("status") in ("failed", "rate_limited", "auth_error"))
    presets_total = len(presets)

    # Determine current step
    if any(p.get("status") in ("pending", "in_progress") for p in presets.values()):
        current_step = "presets"
    elif steps.get("leads", {}).get("status") != "success":
        current_step = "leads"
    elif steps.get("lead_counts", {}).get("status") != "success":
        current_step = "lead_counts"
    else:
        current_step = "done"

    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "attempt": job["attempt"],
        "max_attempts": job["max_attempts"],
        "current_step": current_step,
        "presets_done": presets_done,
        "presets_total": presets_total,
        "presets_failed": presets_failed,
        "leads_status": steps.get("leads", {}).get("status", "pending"),
        "lead_counts_status": steps.get("lead_counts", {}).get("status", "pending"),
        "next_retry_at": job.get("next_retry_at"),
        "created_at": job.get("created_at"),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _mark_all_auth_error(
    jobs_col,
    job_id: str,
    error_msg: str,
    *,
    db=None,
    group_id: str | None = None,
):
    """Mark all non-success presets as auth_error and set the job to failed.

    Also flips `client_groups.meta_token_error=True` when db + group_id
    are supplied — the circuit-breaker signal. Two consumers read that flag:
      1. schedule_stale_groups: excludes the group from auto-reschedule so
         we don't hammer Graph with a token we already know is dead.
      2. Frontend (MarketingContent.jsx): shows the "Reconnect Meta" banner.

    Cleared on successful OAuth reconnect (routers/meta.py).
    """
    job = await jobs_col.find_one({"job_id": job_id})
    if not job:
        return

    update = {"updated_at": datetime.utcnow(), "status": "failed"}
    for preset_key, preset_state in job["steps"]["presets"].items():
        if preset_state["status"] != "success":
            update[f"steps.presets.{preset_key}.status"] = "auth_error"
            update[f"steps.presets.{preset_key}.error"] = error_msg

    update["steps.leads.status"] = "auth_error"
    update["steps.leads.error"] = error_msg
    update["steps.lead_counts.status"] = "auth_error"
    update["steps.lead_counts.error"] = error_msg

    await jobs_col.update_one({"job_id": job_id}, {"$set": update})

    if db is not None and group_id:
        await db["client_groups"].update_one(
            {"id": group_id},
            {"$set": {
                "meta_token_error": True,
                "meta_token_error_at": datetime.utcnow(),
            }},
        )
        logger.warning(
            f"[{job_id}] Flagged group {group_id} with meta_token_error=True "
            f"— excluded from auto-reschedule until user reconnects."
        )


async def _finalize_job(db, jobs_col, job_id: str, group_id: str):
    """Check all steps and set final job status + update client_groups."""
    job = await jobs_col.find_one({"job_id": job_id})
    if not job:
        return

    steps = job["steps"]
    presets = steps.get("presets", {})
    leads = steps.get("leads", {})
    lead_counts = steps.get("lead_counts", {})

    all_statuses = [p["status"] for p in presets.values()] + [leads["status"], lead_counts["status"]]

    partial_kind: str | None = None

    if all(s == "success" for s in all_statuses):
        final_status = "complete"
        group_status = "complete"
        next_retry = None
        logger.info(f"[{job_id}] All steps complete ✅")
    elif any(s == "auth_error" for s in all_statuses):
        final_status = "failed"
        group_status = "error"
        next_retry = None
        logger.error(f"[{job_id}] Failed due to auth/permission error")
    elif job["attempt"] >= job["max_attempts"]:
        final_status = "failed"
        group_status = "error"
        next_retry = None
        logger.error(f"[{job_id}] Max attempts reached, marking as failed")
    else:
        final_status = "partial"
        # This is a REAL failure partial (some step failed and we haven't
        # hit MAX_ATTEMPTS yet), not a budget yield. Tag it so
        # claim_next_jobs enforces the 10-min backoff and the attempt cap.
        # Budget yields go through yield_partial() with kind="budget".
        partial_kind = "failure"
        next_retry = datetime.utcnow() + timedelta(minutes=RETRY_DELAY_MINUTES)
        group_status = "running"  # still in progress, will retry
        failed_presets = [k for k, v in presets.items() if v["status"] != "success"]
        logger.warning(
            f"[{job_id}] Partial completion — "
            f"failed presets: {failed_presets}, "
            f"leads: {leads['status']}, "
            f"retry at: {next_retry.isoformat()}"
        )

    job_update: dict = {
        "$set": {
            "status": final_status,
            "next_retry_at": next_retry,
            "_claimed_at": None,  # release the claim so a tick can retry partial jobs
            "updated_at": datetime.utcnow(),
        }
    }
    if partial_kind is not None:
        job_update["$set"]["_partial_kind"] = partial_kind
    else:
        # Not partial anymore — drop the kind flag so it doesn't linger
        # on a completed / failed job (would only confuse observability).
        job_update["$unset"] = {"_partial_kind": ""}

    await jobs_col.update_one({"job_id": job_id}, job_update)

    # Update client group status
    update_fields = {
        "meta_refresh_status": group_status,
        "last_meta_refresh": datetime.utcnow(),
    }
    if final_status == "failed":
        error_parts = []
        for k, v in presets.items():
            if v.get("error"):
                error_parts.append(f"{k}: {v['error']}")
                break  # one example is enough
        if leads.get("error"):
            error_parts.append(f"leads: {leads['error']}")
        update_fields["meta_refresh_error"] = "; ".join(error_parts)[:300]

    # Build the update operation. On terminal states (complete/failed),
    # also clear the _refreshing flag so the UI stops showing "refreshing".
    # On "partial", leave the flag set — work is still in flight.
    update_op = {"$set": update_fields}
    if final_status != "partial":
        update_op["$unset"] = {"facebook_cache._refreshing": ""}

    # A completed refresh proves the token works — clear any lingering
    # meta_token_error flag (e.g. if the user reconnected but the clear
    # in routers/meta.py failed for some reason, or if this refresh was
    # kicked off on-demand from the UI while auto-cron was still skipping
    # the group).
    if final_status == "complete":
        update_op.setdefault("$unset", {})
        update_op["$unset"]["meta_token_error"] = ""
        update_op["$unset"]["meta_token_error_at"] = ""

    await db["client_groups"].update_one({"id": group_id}, update_op)


# ---------------------------------------------------------------------------
# Tick-driven orchestration (used by routers/cron.py)
# ---------------------------------------------------------------------------

async def schedule_stale_groups(
    mongo_client,
    cutoff_hours: float,
    presets: list[str],
    limit: int = 100,
) -> list[str]:
    """
    Find client groups whose Meta cache is older than `cutoff_hours` and
    enqueue a refresh job for each (status="pending"). Idempotent — skips
    groups that already have an open job.

    Args:
        cutoff_hours: how stale a group must be to schedule (e.g. 5 for the
                      5-hour Meta cron).
        presets:      preset keys to refresh. Pass META_ONGOING_PRESETS for
                      the 5h cron, META_STATIC_PRESETS for the monthly one.
        limit:        max number of groups to schedule per tick (caps the
                      growth of meta_refresh_jobs per minute).

    Returns the list of group_ids that were scheduled.
    """
    db = mongo_client[DB_NAME]
    cutoff = datetime.utcnow() - timedelta(hours=cutoff_hours)

    # Stage 1: candidate groups (stale or never refreshed).
    # meta_token_error is the circuit-breaker signal — a group flagged
    # here has a token we already know is dead (see _mark_all_auth_error).
    # Skipping it prevents unbounded re-enqueueing every 5h forever until
    # the user reconnects (cleared in routers/meta.py OAuth callback).
    candidates = await db["client_groups"].find(
        {
            "meta_ad_account_id": {"$exists": True, "$ne": None},
            "meta_token_error": {"$ne": True},
            # Two branches, not three: in MongoDB {field: None} already matches
            # documents where the field is absent, so the old {"$exists": False}
            # branch was pure duplication (verified against the live collection —
            # both forms match the same 9 never-refreshed groups, and the whole
            # filter returns identical result sets either way). What is left is
            # two plain ranges over last_meta_refresh, which is exactly what
            # idx_cg_meta_stale (partial, see utils/cache_helpers.py) indexes —
            # that index also serves the .sort() below, so this no longer
            # COLLSCANs the collection and sorts it in memory every minute.
            "$or": [
                {"last_meta_refresh": {"$lt": cutoff}},
                {"last_meta_refresh": None},
            ],
        },
        {
            "id": 1, "user_id": 1, "name": 1,
            "meta_ad_account_id": 1, "ad_account_currency": 1,
        },
    ).sort("last_meta_refresh", 1).limit(limit * 2).to_list(None)  # over-fetch in case some are filtered

    if not candidates:
        return []

    # Stage 2: filter out groups that already have an open job
    candidate_ids = [g["id"] for g in candidates]
    open_jobs = await db["meta_refresh_jobs"].find(
        {
            "group_id": {"$in": candidate_ids},
            "status": {"$in": ["pending", "in_progress", "partial"]},
        },
        {"group_id": 1},
    ).to_list(None)
    busy_ids = {j["group_id"] for j in open_jobs}

    scheduled: list[str] = []
    for group in candidates:
        if len(scheduled) >= limit:
            break
        if group["id"] in busy_ids:
            continue
        try:
            await create_refresh_job(
                group_id=group["id"],
                user_id=group["user_id"],
                group=group,
                mongo_client=mongo_client,
                presets=presets,
                status="pending",
            )
            scheduled.append(group["id"])
        except Exception as e:
            logger.error(f"Failed to schedule refresh for group {group['id']}: {e}")

    if scheduled:
        logger.info(
            f"[scheduler] Enqueued {len(scheduled)} groups for refresh "
            f"({len(presets)} presets, cutoff={cutoff_hours}h)"
        )
    return scheduled


async def claim_next_jobs(
    mongo_client,
    n: int,
    stale_claim_minutes: int = STALE_CLAIM_MINUTES,
) -> list[dict]:
    """
    Atomically claim up to `n` refresh jobs for this tick to work on.

    Four claim reasons — with DIFFERENT gating semantics. This is the whole
    point of the recent rewrite: the previous version reclaimed every
    partial job on the very next tick with no backoff and no attempt cap,
    creating an infinite retry loop against Meta's Graph API on any group
    with a persistent failure (dead token, wrong scope, etc.). The old
    job-level `attempt` counter was only ever incremented by the (now
    deleted) APScheduler-driven `retry_pending_jobs`, so on Vercel it stayed
    at 1 forever and MAX_ATTEMPTS was never enforced.

    Priority (jobs are picked in `next_retry_at, created_at` order, so
    older / earlier-scheduled jobs go first):

      1. status="pending"
         Never started. No backoff, no attempt cap. Claim immediately.

      2. status="partial", _partial_kind="budget"
         Yielded from execute_refresh because the tick's wall-clock budget
         was exhausted mid-work (see yield_partial). This is a normal event
         on large accounts — the job is fine, we just ran out of time.
         Claim immediately with no attempt-cap.

      3. status="partial", _partial_kind="failure" (or missing on legacy rows)
         Real failure — at least one step actually errored and we haven't
         hit MAX_ATTEMPTS yet. Enforce `next_retry_at <= now` (the 10-min
         backoff from _finalize_job) AND `attempt < max_attempts`. On claim
         we `$inc attempt`, so the third failure-partial claim brings
         attempt to MAX_ATTEMPTS and the row falls out of this filter —
         terminal-failure at the next _finalize_job.

      4. status="in_progress" AND _claimed_at < now - stale_claim_minutes
         Crash recovery — previous tick died mid-execution. Reclaim without
         touching attempt (we don't know if it was a real failure or an OOM).

    Groups with `meta_token_error=True` don't reach here in the first place
    because schedule_stale_groups filters them out. Any pre-existing jobs
    on such groups run out via attempt-cap and stop.
    """
    db = mongo_client[DB_NAME]
    jobs_col = db["meta_refresh_jobs"]
    now = datetime.utcnow()
    stale_before = now - timedelta(minutes=stale_claim_minutes)

    filter_query = {
        "$or": [
            # 1. pending
            {"status": "pending"},
            # 2. partial + budget
            {"status": "partial", "_partial_kind": "budget"},
            # 3. partial + failure (or legacy partial with no kind flag —
            #    treat as failure for safety so it can't loop forever)
            {
                "status": "partial",
                "$or": [
                    {"_partial_kind": "failure"},
                    {"_partial_kind": {"$exists": False}},
                ],
                "next_retry_at": {"$lte": now},
                "$expr": {"$lt": ["$attempt", "$max_attempts"]},
            },
            # 4. stale in_progress (crash recovery)
            {
                "status": "in_progress",
                "$or": [
                    {"_claimed_at": {"$lt": stale_before}},
                    {"_claimed_at": None},
                    {"_claimed_at": {"$exists": False}},
                ],
            },
        ],
    }

    # Aggregation pipeline update: claim atomically AND bump `attempt`
    # only when the doc was a failure-partial (or a legacy partial with
    # no kind). Pending / budget-partial / stale-in-progress claims leave
    # `attempt` untouched, so budget yields never eat into the retry budget.
    claim_update = [
        {
            "$set": {
                "status": "in_progress",
                "_claimed_at": now,
                "updated_at": now,
                "attempt": {
                    "$cond": [
                        {"$and": [
                            {"$eq": ["$status", "partial"]},
                            {"$ne": [
                                {"$ifNull": ["$_partial_kind", "failure"]},
                                "budget",
                            ]},
                        ]},
                        {"$add": [{"$ifNull": ["$attempt", 1]}, 1]},
                        {"$ifNull": ["$attempt", 1]},
                    ],
                },
            },
        },
    ]

    claimed: list[dict] = []
    for _ in range(n):
        # find_one_and_update is atomic — guarantees no two ticks claim the same row.
        job = await jobs_col.find_one_and_update(
            filter_query,
            claim_update,
            sort=[("next_retry_at", 1), ("created_at", 1)],
            # default return_document=BEFORE is fine — execute_refresh re-fetches
            # the job by job_id, so we only need the identifying fields here.
        )
        if not job:
            break
        claimed.append(job)

    if claimed:
        logger.info(f"[claim] Took {len(claimed)} job(s) for this tick")
    return claimed
