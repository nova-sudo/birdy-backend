"""
Backfill script — recomputes ghl_opp_cache for every client group.

For each group:
  1. Pull every opportunity from the GHL API in one paginated sweep
  2. Derive stats for all 13 date presets in-memory using
     integrations.gohighlevel.compute_opp_stats (by lastStatusChangeAt /
     createdAt rather than the GHL API's creation-date filter, which was
     returning zeros for long-cycle pipelines).
  3. Write every preset to ghl_opp_cache.<preset> and mirror "maximum" into
     the legacy gohighlevel_cache.metrics.opportunity_stats for backward compat.

Run with:  python -m scripts.backfill_opportunity_stats
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

# Add project root to path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from motor.motor_asyncio import AsyncIOMotorClient
from core.database import DB_NAME
from core.constants import META_CACHE_PRESETS, ghl_date_bounds
from integrations.gohighlevel import (
    ghl_integration,
    get_subaccount_tokens,
    compute_opp_stats,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MONGO_URI = os.environ.get("MONGODB_URI", os.environ.get("MONGO_URI", "mongodb://localhost:27017"))


async def backfill():
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    users_col = db["users"]
    groups_col = db["client_groups"]

    users = await users_col.find(
        {"integrations.gohighlevel": {"$exists": True}},
        {"user_id": 1},
    ).to_list(None)

    logger.info("Found %d users with GHL integration", len(users))
    logger.info("Will derive %d presets per group from one opp fetch each", len(META_CACHE_PRESETS))

    total_updated = 0
    total_failed = 0
    total_skipped = 0

    for user_doc in users:
        user_id = user_doc["user_id"]
        logger.info("\n" + "=" * 60)
        logger.info("Processing user: %s", user_id)

        try:
            subaccount_tokens = await get_subaccount_tokens(user_id, client)
        except Exception as e:
            logger.error("  Failed to get subaccount tokens: %s", e)
            continue

        if not subaccount_tokens:
            logger.warning("  No subaccount tokens found, skipping")
            continue

        groups = await groups_col.find(
            {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}},
            {"id": 1, "name": 1, "ghl_location_id": 1},
        ).to_list(None)

        logger.info("  Found %d client groups with GHL locations", len(groups))

        for group in groups:
            group_id = group["id"]
            group_name = group.get("name", "Unknown")
            location_id = group["ghl_location_id"]

            access_token = subaccount_tokens.get(location_id, {}).get("access_token")
            if not access_token:
                logger.warning("  [%s] No access token, skipping", group_name)
                total_failed += 1
                continue

            logger.info("  [%s] Fetching all opportunities…", group_name)
            ok, opps = await ghl_integration.fetch_all_opportunities(location_id, access_token)

            if not ok:
                logger.warning("  [%s] API fetch failed — preserving existing cache", group_name)
                total_failed += 1
                await asyncio.sleep(1)
                continue

            # Safety: if fetch returned empty but cache has data, skip
            if not opps:
                existing = await groups_col.find_one(
                    {"id": group_id}, {"ghl_opp_cache.maximum": 1}
                )
                existing_max = (existing or {}).get("ghl_opp_cache", {}).get("maximum", {}) or {}
                if (existing_max.get("total_opportunities") or 0) > 0:
                    logger.warning(
                        "  [%s] API returned 0 opps but cache has %s — skipping to preserve",
                        group_name, existing_max.get("total_opportunities"),
                    )
                    total_skipped += 1
                    continue
                logger.info("  [%s] No opportunities (legitimately empty location)", group_name)

            # Derive all preset stats in-memory
            opp_cache = {
                preset: compute_opp_stats(opps, *ghl_date_bounds(preset))
                for preset in META_CACHE_PRESETS
            }

            # Write via dot-notation
            update_fields = {f"ghl_opp_cache.{preset}": stats for preset, stats in opp_cache.items()}
            update_fields["ghl_opp_cache.updated_at"] = datetime.utcnow().isoformat()

            await groups_col.update_one(
                {"id": group_id},
                {"$set": update_fields},
            )

            # Mirror "maximum" into legacy location
            max_stats = opp_cache.get("maximum") or {}
            if (max_stats.get("total_opportunities") or 0) > 0:
                await groups_col.update_one(
                    {"id": group_id},
                    {"$set": {"gohighlevel_cache.metrics.opportunity_stats": max_stats}},
                )

            l7 = opp_cache.get("last_7d") or {}
            l30 = opp_cache.get("last_30d") or {}
            logger.info(
                "  [%s] %d opps | max won=%d total=%d rev=£%.0f | last_7d won=%d total=%d | last_30d won=%d total=%d",
                group_name, len(opps),
                max_stats.get("won", 0), max_stats.get("total_opportunities", 0), max_stats.get("won_revenue", 0),
                l7.get("won", 0), l7.get("total_opportunities", 0),
                l30.get("won", 0), l30.get("total_opportunities", 0),
            )
            total_updated += 1
            await asyncio.sleep(1)

    logger.info("\n" + "=" * 60)
    logger.info(
        "Backfill complete: %d updated, %d failed, %d skipped (data preserved)",
        total_updated, total_failed, total_skipped,
    )

    client.close()


if __name__ == "__main__":
    asyncio.run(backfill())
