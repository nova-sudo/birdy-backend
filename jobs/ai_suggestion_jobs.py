"""
jobs/ai_suggestion_jobs.py
--------------------------
Scheduled Birdy suggestion passes.

On Vercel these are triggered by the cron endpoints in routers/cron.py; on a
long-lived host (Azure/VM/Docker) by APScheduler in jobs/scheduler.py. Both call
the same functions here so behaviour is identical either way.

Each call runs one pass over every user within a wall-clock budget (see
run_pass_all_users). For a large user base a single weekly/monthly invocation may
not reach everyone; the pass is idempotent, so the scaling path is to shard it
(claim-based, like the meta/ghl ticks) rather than raise the budget.
"""

import logging

from dependencies import get_mongo_client
from ai.suggestions.contracts import WINDOW_MONTHLY, WINDOW_WEEKLY
from ai.suggestions.orchestrator import run_pass_all_users

logger = logging.getLogger(__name__)


async def run_weekly_suggestions(budget_seconds: float = 240.0) -> dict:
    async with get_mongo_client() as mongo_client:
        result = await run_pass_all_users(mongo_client, WINDOW_WEEKLY, budget_seconds=budget_seconds)
    logger.info("[ai-suggestions] weekly pass: %s", result)
    return result


async def run_monthly_suggestions(budget_seconds: float = 240.0) -> dict:
    async with get_mongo_client() as mongo_client:
        result = await run_pass_all_users(mongo_client, WINDOW_MONTHLY, budget_seconds=budget_seconds)
    logger.info("[ai-suggestions] monthly pass: %s", result)
    return result
