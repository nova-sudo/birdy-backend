"""
Turn every account's Slack brief into its default Slack report, then exit.

Optional: the report tick (GET /api/cron/slack-reports) does the same for any
account it finds without one, so this only makes the switch happen up front.
Idempotent — run it as often as you like.

Run with:  python -m scripts.migrate_slack_reports
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

from core.database import DB_NAME
from services.slack_reports import create_slack_report_indexes, migrate_briefs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def main():
    client = AsyncIOMotorClient(MONGO_URI)
    try:
        await create_slack_report_indexes(client)
        migrated = await migrate_briefs(client[DB_NAME])
    finally:
        client.close()
    logger.info("Migrated %d brief(s) into Slack reports", migrated)


if __name__ == "__main__":
    asyncio.run(main())
