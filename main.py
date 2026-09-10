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
from dependencies import get_mongo_client
from core.mongo_client import get_shared_mongo_client, close_shared_mongo_client

from routers import auth, ghl, meta, hotprospector, client_groups, settings, alerts, admin, admin_console, chat, metrics, cron, webhooks, call_logs, call_analysis, mcp_tokens, ai_credentials, slack, slack_events, slack_interactions, waitlist, dashboard, onboarding, client_notes
from billing import router as billing_router
from credits import router as credits_router

from ai.mcp import mcp_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Lifespan ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup.

    On Vercel this runs on every cold container, in front of the request that
    woke it — so it does as close to nothing as possible. Index creation and
    the cache backfill both moved out to crons; what's left is warming the
    Mongo client, which is the one thing the first request genuinely needs.
    """
    on_vercel = bool(os.getenv("VERCEL"))

    get_shared_mongo_client()  # warm the process-wide client every request shares

    if not on_vercel:
        # One long-lived process serves everything locally, so the few seconds
        # this costs are paid once at boot rather than on every cold start,
        # and it saves remembering to run the script after adding an index.
        # On Vercel the daily /api/cron/ensure-indexes does it instead.
        from core.indexes import ensure_indexes

        async with get_mongo_client() as client:
            await ensure_indexes(client)

        # Fills in groups that have no cache yet. On Vercel the per-minute
        # meta/ghl/hp ticks already claim them — schedule_stale_groups treats
        # a missing last_*_refresh as infinitely stale — so running this here
        # would only duplicate that work inside a container that may be frozen
        # mid-flight anyway.
        asyncio.create_task(populate_cache_for_existing_groups())
    else:
        # APScheduler is only suitable for long-lived processes (Azure App
        # Service, bare VM, Docker, etc.). On Vercel's serverless runtime it's
        # unreliable because containers are recycled — Vercel cron endpoints in
        # routers/cron.py take over scheduling there.
        logger.info("Detected VERCEL runtime — skipping APScheduler (Vercel crons drive refreshes)")

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
    # GETs no longer preflight at all (the dashboard stopped sending headers
    # that made them non-simple), but mutations still must: PUT/PATCH/DELETE
    # are never simple, and a JSON body is never a safelisted content type.
    # Starlette caches those for 600s by default. 7200 is the ceiling Chromium
    # honours, and the answer doesn't change between deploys.
    max_age=7200,
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
app.include_router(credits_router)
app.include_router(chat.router)
app.include_router(metrics.router)
app.include_router(cron.router)
app.include_router(webhooks.router)
app.include_router(call_logs.router)
app.include_router(call_analysis.router)
app.include_router(mcp_tokens.router)
app.include_router(ai_credentials.router)
app.include_router(slack.router)
app.include_router(slack_events.router)
app.include_router(slack_interactions.router)
app.include_router(waitlist.router)
app.include_router(dashboard.router)
app.include_router(onboarding.router)
app.include_router(client_notes.router)

# MCP server — all migrated tools live in ai/mcp/*.py, registered onto the
# shared FastMCP instance in ai/mcp/server.py
app.mount("/mcp", mcp_app)
