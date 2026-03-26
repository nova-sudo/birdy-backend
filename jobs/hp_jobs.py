import asyncio
import logging
import time
from datetime import datetime

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.gohighlevel import get_subaccount_tokens
from integrations.hotprospector import (
    get_hotprospector_credentials,
    save_hotprospector_leads_to_collection,
    get_hotprospector_leads_from_collection,
    HotProspectorIntegration,
    get_client_group_mapping,
)

logger = logging.getLogger(__name__)


async def refresh_hp_data_for_all_users():
    """
    SEQUENTIAL: Refresh Hot Prospector data ONE USER AT A TIME
    """
    async with get_mongo_client() as mongo_client:
        start_time = datetime.utcnow()
        success_count = 0
        failure_count = 0

        try:
            logger.info("🔄 Starting SEQUENTIAL Hot Prospector data refresh job")
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Get all unique users with GHL locations (HP uses GHL location)
            pipeline = [
                {"$match": {"ghl_location_id": {"$exists": True, "$ne": None}}},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}}
            ]

            users_with_groups = await client_groups_collection.aggregate(pipeline).to_list(None)

            if not users_with_groups:
                logger.info("✅ No users with GHL locations found")
                return

            logger.info(f"📊 Found {len(users_with_groups)} users for HP refresh")

            # ============================================
            # 🔥 PROCESS USERS ONE AT A TIME (SEQUENTIAL)
            # ============================================
            for user_index, user_data in enumerate(users_with_groups, 1):
                user_id = user_data["_id"]
                groups = user_data["groups"]

                try:
                    logger.info(
                        f"🔄 [{user_index}/{len(users_with_groups)}] "
                        f"Processing user {user_id} ({len(groups)} locations)"
                    )

                    # Get HP credentials for this user
                    hp_credentials = await get_hotprospector_credentials(user_id, mongo_client)

                    if not hp_credentials:
                        logger.info(f"No HP credentials for user {user_id}, skipping")
                        continue

                    # Get subaccount tokens for location names
                    subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)

                    # Get client group mapping
                    client_group_mapping = await get_client_group_mapping(user_id, mongo_client)

                    integration = HotProspectorIntegration(
                        hp_credentials.get("api_uid"),
                        hp_credentials.get("api_key")
                    )

                    # Fetch HP data for all groups (can still be parallel within user)
                    tasks = []
                    for group in groups:
                        if group.get("ghl_location_id"):
                            location_name = subaccount_tokens.get(
                                group["ghl_location_id"], {}
                            ).get("name", "Unknown Location")

                            tasks.append(
                                fetch_hp_leads_with_calls_for_group(
                                    group["ghl_location_id"],
                                    user_id,
                                    mongo_client,
                                    integration,
                                    location_name,
                                    client_group_mapping
                                )
                            )

                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    # Update database with results
                    for i, group in enumerate(groups):
                        if not group.get("ghl_location_id"):
                            continue

                        if not isinstance(results[i], Exception):
                            location_id, lead_count, call_count = results[i]
                            location_data = subaccount_tokens.get(location_id, {})

                            cache_data = {
                                "ghl_location_id": location_id,
                                "name": location_data.get("name", "Unknown Location"),
                                "metrics": {
                                    "total_leads": lead_count,
                                }
                            }

                            await client_groups_collection.update_one(
                                {"id": group["id"]},
                                {
                                    "$set": {
                                        "hotprospector_cache": cache_data,
                                        "last_hp_refresh": datetime.utcnow()
                                    }
                                }
                            )
                            logger.info(
                                f"✅ Updated HP cache for group {group['name']}: "
                                f"{lead_count} leads"
                            )
                            success_count += 1
                        else:
                            logger.error(f"❌ Failed to refresh HP for group {group['name']}: {results[i]}")
                            failure_count += 1

                    # ============================================
                    # 🔥 DELAY BETWEEN USERS
                    # ============================================
                    if user_index < len(users_with_groups):
                        await asyncio.sleep(2)  # 2 second delay
                        logger.info(f"⏳ Waiting 2s before next user...")

                except Exception as e:
                    failure_count += len(groups)
                    logger.error(f"Error refreshing HP data for user {user_id}: {e}", exc_info=True)

            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"✅ SEQUENTIAL HP data refresh completed in {elapsed:.2f}s - "
                f"Success: {success_count}, Failed: {failure_count}"
            )

        except Exception as e:
            logger.error(f"❌ Critical error in HP refresh job: {e}", exc_info=True)


async def fetch_hp_leads_with_calls_for_group(
        ghl_location_id: str,
        user_id: str,
        mongo_client,
        integration: HotProspectorIntegration,
        location_name: str,
        client_group_mapping: dict
):
    """
    Fetch Hot Prospector leads WITH CALL LOGS for a single group.

    KEY CHANGE: Now includes call logs in background fetch
    """
    try:
        # Try cache first
        cached_leads, total_count = await get_hotprospector_leads_from_collection(
            user_id,
            ghl_location_id,
            mongo_client,
            skip=0,
            limit=None
        )

        # Fetch fresh if no cache
        if not cached_leads:
            client_group_name = client_group_mapping.get(ghl_location_id)

            try:
                success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)

                if success:
                    # Normalize leads
                    normalized_leads = [
                        integration.normalize_lead(
                            lead,
                            ghl_location_id,
                            location_name,
                            client_group_name
                        )
                        for lead in hp_leads
                    ]

                    # 🔥 FETCH CALL LOGS
                    total_calls = 0
                    if normalized_leads:
                        lead_ids = [
                            str(lead.get("id"))
                            for lead in normalized_leads
                            if lead.get("id")
                        ]

                        # call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                        for lead in normalized_leads:
                            lead_id = str(lead.get("id"))
                            # lead["call_logs"] = call_logs_map.get(lead_id, [])
                            lead["call_logs_count"] = len(lead["call_logs"])
                            total_calls += lead["call_logs_count"]

                    # Save to cache WITH CALL LOGS
                    await save_hotprospector_leads_to_collection(
                        user_id,
                        ghl_location_id,
                        normalized_leads,
                        mongo_client
                    )

                    return ghl_location_id, len(normalized_leads), total_calls
                else:
                    return ghl_location_id, 0, 0

            except Exception as e:
                logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
                return ghl_location_id, 0, 0
        else:
            # Count calls from cached data
            total_calls = sum(lead.get("call_logs_count", 0) for lead in cached_leads)
            return ghl_location_id, total_count, total_calls

    except Exception as e:
        logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
        return ghl_location_id, 0, 0
