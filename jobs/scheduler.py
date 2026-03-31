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

    from jobs.meta_jobs import refresh_meta_data_for_all_users
    from jobs.ghl_jobs import refresh_ghl_data_for_all_users, refresh_tokens_for_all_users
    from jobs.hp_jobs import refresh_hp_data_for_all_users
    from jobs.alert_jobs import evaluate_all_alerts

    # Token refresh: runs every 12 hours at :00 minutes
    scheduler.add_job(
        refresh_tokens_for_all_users,
        CronTrigger( minute='55'),
        id='token_refresh',
        replace_existing=True,
        max_instances=1
    )

    scheduler.add_job(
        evaluate_all_alerts,
        CronTrigger(minute="30"),
        id="alert_evaluation",
        replace_existing=True,
        max_instances=1
    )

    # GHL data refresh: runs hourly at :05 (waits for token refresh to complete)
    scheduler.add_job(
        refresh_ghl_data_for_all_users,
        CronTrigger(minute='00'),
        id='ghl_refresh',
        replace_existing=True,
        max_instances=2

    )
    #
    # # Meta data refresh: runs hourly at :25 (staggered)
    scheduler.add_job(
        refresh_meta_data_for_all_users,
        CronTrigger(minute='10'),
        id='meta_refresh',
        replace_existing=True,
        max_instances=1
    )

    # # HP data refresh: runs hourly at :45 (staggered)
    # scheduler.add_job(
    #     refresh_hp_data_for_all_users,
    #     CronTrigger(minute='10'),
    #     id='hp_refresh',
    #     replace_existing=True,
    #     max_instances=1
    # )

    scheduler.start()
    logger.info("🚀 Background refresh jobs started with cron scheduling")


def stop_background_jobs():
    """Gracefully stop all background jobs"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background jobs stopped")
