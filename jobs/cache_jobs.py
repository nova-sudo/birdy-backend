import asyncio
import logging
import time

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.gohighlevel import get_subaccount_tokens
from integrations.hotprospector import get_hotprospector_credentials
from services.ghl_service import fetch_and_cache_ghl_data_optimized
from services.meta_service import fetch_and_cache_meta_data, get_facebook_data
from services.hp_service import fetch_and_cache_hp_data

logger = logging.getLogger(__name__)


async def populate_cache_for_existing_groups():
    """
    Run once on startup to populate cache for existing groups that don't have cache data.
    This ensures all groups have data immediately.
    """
    async with get_mongo_client() as mongo_client:
        try:
            logger.info("🔄 Starting cache population for existing client groups...")
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Find all groups that don't have cache OR have empty cache
            groups_needing_cache = await client_groups_collection.find({
                "$or": [
                    {"gohighlevel_cache": {"$exists": False}},
                    {"facebook_cache": {"$exists": False}},
                    {"hotprospector_cache": {"$exists": False}},
                    {"gohighlevel_cache": {}},
                    {"facebook_cache": {}},
                    {"hotprospector_cache": {}}
                ]
            }).to_list(None)

            if not groups_needing_cache:
                logger.info("✅ All client groups already have cache data")
                return

            logger.info(f"📊 Found {len(groups_needing_cache)} groups needing cache population")

            # Group by user for efficient processing
            users_groups = {}
            for group in groups_needing_cache:
                user_id = group["user_id"]
                if user_id not in users_groups:
                    users_groups[user_id] = []
                users_groups[user_id].append(group)

            # Process each user's groups
            for user_id, groups in users_groups.items():
                logger.info(f"Processing {len(groups)} groups for user {user_id}")

                try:
                    # Get user's integration tokens
                    subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
                    facebook_ad_accounts_data = await get_facebook_data(
                        user_id, "global", "adaccounts", mongo_client
                    )
                    facebook_ad_accounts = facebook_ad_accounts_data.get("data",
                                                                         []) if facebook_ad_accounts_data else []
                    hp_credentials = await get_hotprospector_credentials(user_id, mongo_client)

                    # Process groups in batches of 5 to avoid overwhelming the system
                    batch_size = 5
                    for i in range(0, len(groups), batch_size):
                        batch = groups[i:i + batch_size]
                        batch_num = (i // batch_size) + 1
                        total_batches = (len(groups) + batch_size - 1) // batch_size

                        logger.info(f"Processing batch {batch_num}/{total_batches} for user {user_id}")

                        # Create tasks for this batch
                        tasks = []
                        for group in batch:
                            group_id = group["id"]

                            # GHL task
                            if group.get("ghl_location_id") and not group.get("gohighlevel_cache"):
                                tasks.append(
                                    fetch_and_cache_ghl_data_optimized(
                                        group_id,
                                        group["ghl_location_id"],
                                        user_id,
                                        mongo_client
                                    )
                                )

                            # Meta task
                            if group.get("meta_ad_account_id") and not group.get("facebook_cache"):
                                tasks.append(
                                    fetch_and_cache_meta_data(
                                        group_id,
                                        group["meta_ad_account_id"],
                                        user_id,
                                        mongo_client
                                    )
                                )

                            # HP task
                            if group.get("ghl_location_id") and hp_credentials and not group.get("hotprospector_cache"):
                                tasks.append(
                                    fetch_and_cache_hp_data(
                                        group_id,
                                        group["ghl_location_id"],
                                        user_id,
                                        mongo_client
                                    )
                                )

                        # Execute batch in parallel
                        if tasks:
                            results = await asyncio.gather(*tasks, return_exceptions=True)
                            for result in results:
                                if isinstance(result, Exception):
                                    logger.error(f"Error in batch: {result}")

                        # Small delay between batches
                        if i + batch_size < len(groups):
                            await asyncio.sleep(1)

                except Exception as e:
                    logger.error(f"Error processing groups for user {user_id}: {e}")

            logger.info("✅ Finished cache population for existing client groups")

        except Exception as e:
            logger.error(f"Error in startup cache population: {e}", exc_info=True)
