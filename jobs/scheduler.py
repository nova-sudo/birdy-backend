from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import logging

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def start_background_jobs():
    """Start all background refresh jobs with proper scheduling for serverless"""

    # Only start if not already running (prevents duplicates in serverless)
    if scheduler.running:
        logger.info("Scheduler already running, skipping initialization")
        return

    from jobs.ghl_jobs import refresh_tokens_for_all_users, refresh_ghl_data_for_all_users
    from jobs.meta_jobs import refresh_meta_data_for_all_users
    from jobs.alert_jobs import evaluate_all_alerts

    # GHL token refresh: every 45 minutes (tokens expire in 1 hour)
    scheduler.add_job(
        refresh_tokens_for_all_users,
        CronTrigger(minute='*/45'),
        id='token_refresh',
        replace_existing=True,
        max_instances=1,
    )

    # Meta data refresh: hourly at :10 (frequent presets every run, slow presets once daily)
    scheduler.add_job(
        refresh_meta_data_for_all_users,
        CronTrigger(minute='10'),
        id='meta_refresh',
        replace_existing=True,
        max_instances=1,
    )

    # GHL contacts refresh: hourly at :30
    scheduler.add_job(
        refresh_ghl_data_for_all_users,
        CronTrigger(minute='30'),
        id='ghl_refresh',
        replace_existing=True,
        max_instances=1,
    )

    # Alert evaluation: hourly at :45
    scheduler.add_job(
        evaluate_all_alerts,
        CronTrigger(minute='45'),
        id='alert_evaluation',
        replace_existing=True,
        max_instances=1,
    )

    # Birdy suggestion passes: weekly (Mon 06:00) + monthly (1st 06:00).
    from jobs.ai_suggestion_jobs import run_weekly_suggestions, run_monthly_suggestions
    scheduler.add_job(
        run_weekly_suggestions,
        CronTrigger(day_of_week='mon', hour=6, minute=0),
        id='ai_suggestions_weekly',
        replace_existing=True,
        max_instances=1,
    )
    scheduler.add_job(
        run_monthly_suggestions,
        CronTrigger(day=1, hour=6, minute=0),
        id='ai_suggestions_monthly',
        replace_existing=True,
        max_instances=1,
    )

    # Client health: same Monday 06:00 slot, measured through the previous
    # Sunday. Registered separately from the suggestion passes so a failure in
    # one cannot stop the other.
    from jobs.health_jobs import run_weekly_health
    scheduler.add_job(
        run_weekly_health,
        CronTrigger(day_of_week='mon', hour=6, minute=0),
        id='client_health_weekly',
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("Background jobs scheduler started — token_refresh (*/45), meta_refresh (:10), ghl_refresh (:30), alert_eval (:45), ai_suggestions (weekly Mon 06:00 / monthly 1st 06:00)")

 
def stop_background_jobs():
    """Gracefully stop all background jobs"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background jobs stopped")
