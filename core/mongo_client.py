"""
core/mongo_client.py
---------------------
Shared, long-lived Motor client for the MCP-hosted tool surface (ai/mcp/).

This is a deliberate exception to the rest of the app's per-request Mongo
client convention (see dependencies.py::get_mongo_client()). FastMCP session
state must be JSON-serializable, so a live AsyncIOMotorClient can't be handed
to tools via ctx.set_state() — a module-level singleton, warmed once at app
startup, is the standard pattern for a long-running MCP server instead.

Only ai/mcp/*.py tools use this. Everything else keeps using
dependencies.py::get_mongo_client() per request.
"""

import asyncio
import logging
import os

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None


def get_shared_mongo_client() -> AsyncIOMotorClient:
    """Return the shared Motor client, creating it on first use."""
    global _client
    if _client is None:
        mongo_uri = os.getenv("MONGODB_URI")
        if not mongo_uri:
            raise RuntimeError("MongoDB configuration error: MONGODB_URI is not set")
        _client = AsyncIOMotorClient(mongo_uri, io_loop=asyncio.get_event_loop())
        logger.info("Shared Mongo client initialized for MCP tools")
    return _client


async def close_shared_mongo_client() -> None:
    """Close and clear the shared Motor client, if one was created."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("Shared Mongo client closed")
