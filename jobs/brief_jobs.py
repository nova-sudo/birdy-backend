"""
jobs/brief_jobs.py
------------------
The Slack report tick: sends every scheduled report that is due (see
services/slack_reports.py). The brief is the account's default report.

On Vercel this runs from routers/cron.py's /api/cron/slack-reports; on a
long-lived host from APScheduler in jobs/scheduler.py. Report times are any
minute, so the tick runs every 5 minutes and a report lands within 5 of its time.
"""

import logging

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.slack_reports import send_due_reports

logger = logging.getLogger(__name__)


async def run_slack_briefs() -> dict:
    """Send every report that is due. Never raises into the job runner."""
    try:
        async with get_mongo_client() as mongo_client:
            return await send_due_reports(mongo_client[DB_NAME])
    except Exception as e:
        logger.error("Slack report pass failed: %s", e, exc_info=True)
        return {"error": str(e)}
