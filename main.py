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

from routers import auth, ghl, meta, hotprospector, client_groups, settings, alerts, admin, chat, metrics, cron, webhooks, call_logs
from services.call_logs_service import create_call_logs_indexes
from billing import router as billing_router

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

    # APScheduler is only suitable for long-lived processes (Azure App Service,
    # bare VM, Docker, etc.). On Vercel's serverless runtime it's unreliable
    # because containers are recycled — Vercel cron endpoints in routers/cron.py
    # take over scheduling there.
    if os.getenv("VERCEL"):
        logger.info("Detected VERCEL runtime — skipping APScheduler (Vercel crons drive refreshes)")


    asyncio.create_task(populate_cache_for_existing_groups())
    logger.info("Server started")

    yield

    if not os.getenv("VERCEL"):
        stop_background_jobs()
    logger.info("Server stopped")


# ── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

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
app.include_router(billing_router)
app.include_router(chat.router)
app.include_router(metrics.router)
app.include_router(cron.router)
app.include_router(webhooks.router)
app.include_router(call_logs.router)
