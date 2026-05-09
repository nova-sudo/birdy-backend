"""
services/meta_refresh_manager.py
---------------------------------
Resilient Meta refresh orchestrator with granular per-preset tracking
and automatic retry of failed steps.
"""

import logging
from datetime import datetime, timedelta

from core.constants import META_CACHE_PRESETS
from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_service import (
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)
from integrations.facebook_utils.facebook_leads import (
    fetch_and_cache_facebook_leads_FIXED,
)

logger = logging.getLogger(__name__)

RETRY_DELAY_MINUTES = 10
MAX_ATTEMPTS = 3


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def start_refresh(group_id: str, user_id: str, group: dict, mongo_client) -> str:
    """
    Create a refresh job and start executing it.
    Returns the job_id.
    """
    db = mongo_client[DB_NAME]
    now = datetime.utcnow()
    job_id = f"refresh_{group_id}_{int(now.timestamp())}"

    ad_account_id = group.get("meta_ad_account_id")
    currency = group.get("ad_account_currency")

    # Build initial job document with all presets pending
    presets_status = {}
    for preset in META_CACHE_PRESETS:
        presets_status[preset] = {"status": "pending", "attempt": 0, "error": None}

    job_doc = {
        "job_id": job_id,
        "group_id": group_id,
        "user_id": user_id,
        "ad_account_id": ad_account_id,
        "currency": currency,
        "group_name": group.get("name", ""),
        "status": "in_progress",
        "created_at": now,
        "updated_at": now,
        "attempt": 1,
        "max_attempts": MAX_ATTEMPTS,
        "next_retry_at": None,
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

    logger.info(f"[{job_id}] Started Meta refresh for '{group.get('name', group_id)}'")

    # Execute (this runs inline in the background task)
    await execute_refresh(job_id, mongo_client)
    return job_id


async def execute_refresh(job_id: str, mongo_client):
    """
    Execute or resume a refresh job. Only attempts steps that aren't already "success".
    """
    db = mongo_client[DB_NAME]
    jobs_col = db["meta_refresh_jobs"]

    job = await jobs_col.find_one({"job_id": job_id})
    if not job:
        logger.error(f"[{job_id}] Job not found")
        return

    group_id = job["group_id"]
    user_id = job["user_id"]
    ad_account_id = job["ad_account_id"]
    currency = job["currency"]
    steps = job["steps"]

    # Validate token first
    token = await get_facebook_token(user_id, mongo_client)
    if not token or not token.get("access_token"):
        logger.error(f"[{job_id}] No valid Facebook token")
        await _mark_all_auth_error(jobs_col, job_id, "No Facebook token")
        await _finalize_job(db, jobs_col, job_id, group_id)
        return

    # ── PHASE 1: Presets ─────────────────────────────────────────────────
    auth_failed = False
    for preset_key, preset_state in steps["presets"].items():
        if preset_state["status"] == "success":
            continue  # already done

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

        except Exception as e:
            error_msg = str(e)[:200]
            is_auth = "auth" in error_msg.lower() or "permission" in error_msg.lower() or "token" in error_msg.lower()
            is_rate_limit = "rate" in error_msg.lower() or "429" in error_msg

            if is_auth:
                logger.error(f"[{job_id}] Auth/permission error on '{preset_key}': {error_msg}")
                await _mark_all_auth_error(jobs_col, job_id, error_msg)
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
        import asyncio
        await asyncio.sleep(5)

    if auth_failed:
        await _finalize_job(db, jobs_col, job_id, group_id)
        return

    # ── PHASE 2: Leads ───────────────────────────────────────────────────
    # Reload job to get updated state
    job = await jobs_col.find_one({"job_id": job_id})
    steps = job["steps"]

    if steps["leads"]["status"] != "success":
        logger.info(f"[{job_id}] Fetching leads...")
        await jobs_col.update_one(
            {"job_id": job_id},
            {"$set": {
                "steps.leads.status": "in_progress",
                "updated_at": datetime.utcnow(),
            }},
        )

        try:
            await fetch_and_cache_facebook_leads_FIXED(
                ad_account_currency=currency,
                group_id=group_id,
                meta_ad_account_id=ad_account_id,
                user_id=user_id,
                mongo_client=mongo_client,
                is_initial_load=True,
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


async def retry_pending_jobs():
    """
    Called by scheduler every 10 minutes.
    Finds partial jobs that are due for retry and re-executes them.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        jobs_col = db["meta_refresh_jobs"]

        now = datetime.utcnow()
        pending_jobs = await jobs_col.find({
            "status": "partial",
            "next_retry_at": {"$lte": now},
            "attempt": {"$lt": MAX_ATTEMPTS},
        }).to_list(None)

        if not pending_jobs:
            return

        logger.info(f"Found {len(pending_jobs)} partial Meta refresh jobs to retry")

        for job in pending_jobs:
            job_id = job["job_id"]
            attempt = job["attempt"] + 1

            logger.info(f"[{job_id}] Retrying (attempt {attempt}/{MAX_ATTEMPTS})")

            await jobs_col.update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "in_progress",
                    "attempt": attempt,
                    "next_retry_at": None,
                    "updated_at": now,
                }},
            )

            # Update group status back to running
            await db["client_groups"].update_one(
                {"id": job["group_id"]},
                {"$set": {"meta_refresh_status": "running"}},
            )

            try:
                await execute_refresh(job_id, mongo_client)
            except Exception as e:
                logger.error(f"[{job_id}] Retry failed: {e}", exc_info=True)
                await jobs_col.update_one(
                    {"job_id": job_id},
                    {"$set": {"status": "failed", "updated_at": datetime.utcnow()}},
                )


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

async def _mark_all_auth_error(jobs_col, job_id: str, error_msg: str):
    """Mark all non-success presets as auth_error and set job to failed."""
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
        next_retry = datetime.utcnow() + timedelta(minutes=RETRY_DELAY_MINUTES)
        group_status = "running"  # still in progress, will retry
        failed_presets = [k for k, v in presets.items() if v["status"] != "success"]
        logger.warning(
            f"[{job_id}] Partial completion — "
            f"failed presets: {failed_presets}, "
            f"leads: {leads['status']}, "
            f"retry at: {next_retry.isoformat()}"
        )

    await jobs_col.update_one(
        {"job_id": job_id},
        {"$set": {
            "status": final_status,
            "next_retry_at": next_retry,
            "updated_at": datetime.utcnow(),
        }},
    )

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

    await db["client_groups"].update_one({"id": group_id}, {"$set": update_fields})
