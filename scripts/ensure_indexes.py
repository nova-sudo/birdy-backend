"""
Create every index the app declares, then exit.

Run after a deploy that adds one — the daily cron
(GET /api/cron/ensure-indexes) will otherwise pick it up within a day, and a
local app start still does it automatically.

Run with:  python -m scripts.ensure_indexes

Exits 1 if any creator failed, and logs which one. This includes the agent
migration, which runs inside create_agent_indexes, so a deploy can rely on the
exit code. The database is MONGODB_URI + MONGODB_DB, from the environment first
and then .env, and MONGODB_DB falls back to the dev database: set both.
"""

import asyncio
import logging
import os
import sys

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient

from core.indexes import ensure_indexes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def main():
    client = AsyncIOMotorClient(MONGO_URI)
    try:
        result = await ensure_indexes(client)
    finally:
        client.close()

    if result["failed"]:
        for name, error in result["failed"].items():
            logger.error(f"{name}: {error}")
        sys.exit(1)

    logger.info(f"All {result['ok']} index sets are in place")


if __name__ == "__main__":
    asyncio.run(main())
