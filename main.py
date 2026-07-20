"""
main.py — FastAPI application entrypoint (Vercel deployment target).

All business logic lives in routers/, services/, and jobs/.
This file only wires them together.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

from core.config import CORS_ORIGINS
from middleware.token_refresh import TokenRefreshMiddleware
from jobs.scheduler import start_background_jobs, stop_background_jobs
from jobs.cache_jobs import populate_cache_for_existing_groups
from utils.cache_helpers import create_performance_indexes
from integrations.facebook_utils.facebook_leads import create_facebook_leads_indexes
from integrations.facebook_utils.facebook_campaigns import create_campaign_insights_indexes
from integrations.facebook_utils.facebook_adsets import create_adset_insights_indexes
from integrations.facebook_utils.facebook_ads import create_ad_insights_indexes
from dependencies import get_mongo_client
from core.mongo_client import get_shared_mongo_client, close_shared_mongo_client

from routers import auth, ghl, meta, hotprospector, client_groups, settings, alerts, admin, admin_console, chat, metrics, cron, webhooks, call_logs, mcp_tokens, ai_credentials, slack, slack_events, slack_interactions, waitlist, dashboard
from services.call_logs_service import create_call_logs_indexes
from services.mcp_token_service import create_mcp_tokens_indexes
from services.slack_bot_service import create_slack_bot_indexes
from services.slack_interaction_store import create_slack_ui_interaction_indexes
from ai.session_store import create_ai_session_indexes
from ai.suggestions.store import create_suggestion_indexes
from ai.conversation_log import create_conversation_log_indexes
from billing import router as billing_router

from ai.mcp import mcp_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create indexes, start background jobs, warm caches."""
    async with get_mongo_client() as client:
        await create_performance_indexes(client)
        await create_facebook_leads_indexes(client)
        await create_campaign_insights_indexes(client)
        await create_adset_insights_indexes(client)
        await create_ad_insights_indexes(client)
        await create_call_logs_indexes(client)
        await create_mcp_tokens_indexes(client)
        await create_slack_bot_indexes(client)
        await create_slack_ui_interaction_indexes(client)
        await create_ai_session_indexes(client)
        await create_suggestion_indexes(client)
        await create_conversation_log_indexes(client)

    get_shared_mongo_client()  # warm the singleton backing the MCP-hosted tools

    # APScheduler is only suitable for long-lived processes (Azure App Service,
    # bare VM, Docker, etc.). On Vercel's serverless runtime it's unreliable
    # because containers are recycled — Vercel cron endpoints in routers/cron.py
    # take over scheduling there.
    if os.getenv("VERCEL"):
        logger.info("Detected VERCEL runtime — skipping APScheduler (Vercel crons drive refreshes)")


    asyncio.create_task(populate_cache_for_existing_groups())
    logger.info("Server started")

    yield

    await close_shared_mongo_client()
    if not os.getenv("VERCEL"):
        stop_background_jobs()
    logger.info("Server stopped")


@asynccontextmanager
async def combined_lifespan(app: FastAPI):
    """Runs both the app's own lifespan and the mounted FastMCP app's lifespan.

    FastMCP's Streamable HTTP session manager only starts inside its own
    lifespan context — mounting mcp_app without wiring this in raises
    "Task group is not initialized" on the first request to /mcp.
    """
    async with lifespan(app):
        async with mcp_app.lifespan(app):
            yield


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=combined_lifespan)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TokenRefreshMiddleware)

# Routers
app.include_router(auth.router)
app.include_router(ghl.router)
app.include_router(meta.router)
app.include_router(hotprospector.router)
app.include_router(client_groups.router)
app.include_router(settings.router)
app.include_router(alerts.router)
app.include_router(admin.router)
app.include_router(admin_console.router)
app.include_router(billing_router)
app.include_router(chat.router)
app.include_router(metrics.router)
app.include_router(cron.router)
app.include_router(webhooks.router)
app.include_router(call_logs.router)
app.include_router(mcp_tokens.router)
app.include_router(ai_credentials.router)
app.include_router(slack.router)
app.include_router(slack_events.router)
app.include_router(slack_interactions.router)
app.include_router(waitlist.router)
app.include_router(dashboard.router)

# MCP server — all migrated tools live in ai/mcp/*.py, registered onto the
# shared FastMCP instance in ai/mcp/server.py
app.mount("/mcp", mcp_app)
