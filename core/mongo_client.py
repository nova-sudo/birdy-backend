"""
core/mongo_client.py
---------------------
The application's shared, long-lived Motor client — one per process, created
on first use and closed once at shutdown.

This replaced a per-request client (dependencies.py used to construct an
AsyncIOMotorClient, ping it, and close it inside every request). Motor keeps
its own connection pool and is designed to be shared across concurrent tasks;
building a client per request threw that pool away every time, so all ~360
call sites paid SRV resolution, a TCP + TLS handshake, SCRAM auth and a ping
round-trip to Atlas before their first query — a few hundred milliseconds per
endpoint, on every endpoint.

Two front doors, one client:
  * dependencies.get_mongo_client() wraps it in the async-context-manager
    shape the existing call sites are written against.
  * ai/mcp/*.py calls get_shared_mongo_client() directly, because FastMCP
    session state has to stay JSON-serializable and can't carry a live client.
"""

import logging
import os

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

# Each serverless container gets its own process, and therefore its own pool.
# Motor's default cap is 100 per client, which across a fan-out of Vercel
# containers can reach the cluster's connection limit — harmless while clients
# were closed at the end of every request, a real risk now that they persist.
# 20 is well above what one container's concurrency needs and bounds the total.
MAX_POOL_SIZE = int(os.getenv("MONGO_MAX_POOL_SIZE", "20"))

# Motor waits 30s for a reachable server by default, which is tuned for a
# long-running process riding out a replica-set election. A Vercel function is
# killed by its own timeout well before that, so the wait buys nothing and only
# delays the error; 10s still covers a quick election.
SERVER_SELECTION_TIMEOUT_MS = int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "10000"))

_client: AsyncIOMotorClient | None = None


def get_shared_mongo_client() -> AsyncIOMotorClient:
    """
    Return the process-wide Motor client, creating it on first use.

    No `io_loop=` is passed: Motor resolves the running loop lazily on first
    use, which is what a lazily-created singleton wants. Pinning it at
    construction would bind the client to whichever loop happened to be
    current at import/warm-up time.
    """
    global _client
    if _client is None:
        mongo_uri = os.getenv("MONGODB_URI")
        if not mongo_uri:
            raise RuntimeError("MongoDB configuration error: MONGODB_URI is not set")
        _client = AsyncIOMotorClient(
            mongo_uri,
            maxPoolSize=MAX_POOL_SIZE,
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
        )
        logger.info("Shared Mongo client initialized (maxPoolSize=%s)", MAX_POOL_SIZE)
    return _client


async def close_shared_mongo_client() -> None:
    """Close and clear the shared Motor client, if one was created."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("Shared Mongo client closed")
