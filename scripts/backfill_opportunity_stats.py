"""
Backfill script: fetches opportunity stats from the GHL Opportunities Search API
for every client group × every date preset, and writes them into ghl_opp_cache.

This replaces the old approach of aggregating from embedded
contact_data.opportunities (which was often incomplete).

Run with:  python -m scripts.backfill_opportunity_stats
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
from core.constants import META_CACHE_PRESETS, ghl_date_bounds_mmddyyyy
from integrations.gohighlevel import ghl_integration, get_subaccount_tokens

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill():
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    users_col = db["users"]
    groups_col = db["client_groups"]

    # Get all users that have GHL integration
    users = await users_col.find(
        {"integrations.gohighlevel": {"$exists": True}},
        {"user_id": 1},
    ).to_list(None)

    logger.info(f"Found {len(users)} users with GHL integration")
    logger.info(f"Will fetch {len(META_CACHE_PRESETS)} presets per group")

    total_updated = 0
    total_failed = 0

    for user_doc in users:
        user_id = user_doc["user_id"]
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing user: {user_id}")

        try:
            subaccount_tokens = await get_subaccount_tokens(user_id, client)
        except Exception as e:
            logger.error(f"  Failed to get subaccount tokens: {e}")
            continue

        if not subaccount_tokens:
            logger.warning(f"  No subaccount tokens found, skipping")
            continue

        groups = await groups_col.find(
            {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}},
            {"id": 1, "name": 1, "ghl_location_id": 1},
        ).to_list(None)

        logger.info(f"  Found {len(groups)} client groups with GHL locations")

        for group in groups:
            group_id = group["id"]
            group_name = group.get("name", "Unknown")
            location_id = group["ghl_location_id"]

            location_data = subaccount_tokens.get(location_id, {})
            access_token = location_data.get("access_token")

            if not access_token:
                logger.warning(f"  [{group_name}] No access token for location {location_id}, skipping")
                total_failed += 1
                continue

            logger.info(f"  [{group_name}] Fetching opp stats for {len(META_CACHE_PRESETS)} presets...")
            opp_cache = {}
            preset_ok = 0

            for preset in META_CACHE_PRESETS:
                start, end = ghl_date_bounds_mmddyyyy(preset)
                try:
                    ok, stats = await ghl_integration.fetch_opportunity_stats(
                        location_id, access_token, date_start=start, date_end=end
                    )
                    if ok:
                        opp_cache[preset] = stats
                        preset_ok += 1
                    else:
                        logger.warning(f"    preset={preset} failed")
                except Exception as e:
                    logger.warning(f"    preset={preset} error: {e}")
                await asyncio.sleep(1)  # Rate limit

            from datetime import datetime
            opp_cache["updated_at"] = datetime.utcnow().isoformat()

            # Write the full per-preset cache
            await groups_col.update_one(
                {"id": group_id},
                {"$set": {"ghl_opp_cache": opp_cache}},
            )

            # Also write "maximum" into legacy location
            max_stats = opp_cache.get("maximum", {})
            if max_stats:
                await groups_col.update_one(
                    {"id": group_id},
                    {"$set": {"gohighlevel_cache.metrics.opportunity_stats": max_stats}},
                )

            logger.info(f"  [{group_name}] ✅ {preset_ok}/{len(META_CACHE_PRESETS)} presets cached")
            if max_stats:
                logger.info(f"    maximum: won={max_stats.get('won',0)} lost={max_stats.get('lost',0)} "
                           f"open={max_stats.get('open',0)} abandoned={max_stats.get('abandoned',0)} "
                           f"revenue=${max_stats.get('won_revenue',0):,.2f}")
            total_updated += 1

            # Delay between groups
            await asyncio.sleep(2)

    logger.info(f"\n{'='*60}")
    logger.info(f"Backfill complete: {total_updated} groups updated, {total_failed} failed")

    client.close()


if __name__ == "__main__":
    asyncio.run(backfill())
