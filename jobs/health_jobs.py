"""
jobs/health_jobs.py
-------------------
Weekly client-health recompute — Monday 06:00, against data through the
previous Sunday.

The rule itself lives in services/client_health.py; this is only the wiring
that runs it on a schedule. Kept separate from the suggestion jobs that share
the same slot so a failure in one cannot take the other down.
"""

import logging

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.client_health import recompute_all

logger = logging.getLogger(__name__)


async def run_weekly_health():
    """Recompute every client's health band. Safe to run more than once."""
    try:
        async with get_mongo_client() as mongo_client:
            db = mongo_client[DB_NAME]
            result = await recompute_all(db)
            logger.info("Weekly client-health pass complete: %s", result)
            return result
    except Exception as e:
        # A health pass failing must not take the Monday job runner with it —
        # the bands simply stay as they were until the next run.
        logger.error("Weekly client-health pass failed: %s", e, exc_info=True)
        return {"error": str(e)}
