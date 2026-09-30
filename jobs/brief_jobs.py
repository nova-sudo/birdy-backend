"""
jobs/brief_jobs.py
------------------
Hourly check for scheduled Slack briefs that are due (see services/slack_brief.py).

On Vercel this runs from routers/cron.py's /api/cron/slack-briefs; on a
long-lived host from APScheduler in jobs/scheduler.py. The brief times the app
offers are whole hours, so an hourly tick at :00 delivers each one on the hour.
"""

import logging

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.slack_brief import send_due_briefs

logger = logging.getLogger(__name__)


async def run_slack_briefs() -> dict:
    """Send every brief that is due. Never raises into the job runner."""
    try:
        async with get_mongo_client() as mongo_client:
            return await send_due_briefs(mongo_client[DB_NAME])
    except Exception as e:
        logger.error("Slack brief pass failed: %s", e, exc_info=True)
        return {"error": str(e)}
