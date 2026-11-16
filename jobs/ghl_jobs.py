import asyncio
import logging
import time
from datetime import datetime, timedelta

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.gohighlevel import (
    AGENCY_JOB_REFRESH_MARGIN,
    agency_token_expires_within,
    ensure_fresh_agency_token,
    ghl_integration,
    get_subaccount_tokens,
    save_subaccount_token,
    fetch_location_details,
    get_contact_count_from_ghl,
)
from services.ghl_service import fetch_and_cache_multiple_ghl_locations

logger = logging.getLogger(__name__)


async def refresh_ghl_data_for_all_users():
    """
    SEQUENTIAL: Refresh GHL contact data ONE USER AT A TIME

    This prevents overwhelming the system and rate limits.
    Each user's locations are still processed in parallel for efficiency.
    """
    async with get_mongo_client() as mongo_client:
        start_time = datetime.utcnow()
        total_success = 0
        total_failure = 0

        try:
            logger.info("🔄 Starting SEQUENTIAL GHL data refresh job")
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Get all unique users with GHL locations
            pipeline = [
                {"$match": {"ghl_location_id": {"$exists": True, "$ne": None}}},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}}
            ]

            users_with_groups = await client_groups_collection.aggregate(pipeline).to_list(None)

            if not users_with_groups:
                logger.info("✅ No users with GHL locations found")
                return

            logger.info(f"📊 Found {len(users_with_groups)} users with GHL locations")

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

                    # Process THIS user's locations (can still be parallel within user)
                    result = await fetch_and_cache_multiple_ghl_locations(
                        user_id,
                        mongo_client,
                        is_initial_load=False  # Incremental mode
                    )

                    total_success += result.get("successful", 0)
                    total_failure += result.get("failed", 0)

                    logger.info(
                        f"✅ User {user_id}: {result['successful']} succeeded, "
                        f"{result['failed']} failed"
                    )

                    # ============================================
                    # 🔥 DELAY BETWEEN USERS (prevent rate limiting)
                    # ============================================
                    if user_index < len(users_with_groups):
                        await asyncio.sleep(2)  # 2 second delay between users
                        logger.info(f"⏳ Waiting 2s before next user...")

                except Exception as e:
                    total_failure += len(groups)
                    logger.error(f"❌ Error processing user {user_id}: {e}", exc_info=True)

            elapsed = (datetime.utcnow() - start_time).total_seconds()

            logger.info(
                f"✅ SEQUENTIAL GHL refresh completed in {elapsed:.2f}s - "
                f"Success: {total_success}, Failed: {total_failure}"
            )

        except Exception as e:
            logger.error(f"❌ Critical error in GHL refresh job: {e}", exc_info=True)


async def refresh_tokens_for_all_users():
    """
    FIXED: Regenerate location tokens instead of refreshing with invalid tokens
    """
    async with get_mongo_client() as mongo_client:
        try:
            logger.info("🔄 Starting token refresh job for all users")
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            users_cursor = users_collection.find({
                "integrations.gohighlevel": {"$exists": True}
            })

            users = await users_cursor.to_list(None)
            total_users = len(users)

            if total_users == 0:
                logger.info("✅ No users with GHL integrations found")
                return

            logger.info(f"📊 Found {total_users} users with GHL integrations")

            refreshed_count = 0
            failed_count = 0
            skipped_count = 0

            for user_doc in users:
                user_id = user_doc.get("user_id")

                try:
                    # ============================================
                    # STEP 1: Agency token, renewed well before it expires
                    # ============================================
                    agency_token = await ensure_fresh_agency_token(
                        user_id, mongo_client, margin=AGENCY_JOB_REFRESH_MARGIN, retry_dead=True,
                    )
                    if not agency_token or not agency_token.get("refresh_token"):
                        logger.error(f"❌ No agency token found for user {user_id}")
                        failed_count += 1
                        continue
                    if agency_token_expires_within(agency_token, timedelta(0)):
                        # Expired and the refresh was refused: GHL needs a reconnect.
                        logger.error(
                            f"❌ GHL agency token for {user_id} is expired and could not be "
                            f"refreshed ({agency_token.get('refresh_error')}); needs a reconnect"
                        )
                        failed_count += 1
                        continue
                    refreshed_count += 1

                    company_id = agency_token.get("company_id")
                    agency_access_token = agency_token.get("access_token")
                    if not company_id or not agency_access_token:
                        logger.error(f"❌ Missing company_id or access_token for user {user_id}")
                        failed_count += len(await get_subaccount_tokens(user_id, mongo_client))
                        continue

                    # ============================================
                    # STEP 2: Regenerate location tokens (not refresh!)
                    # ============================================
                    subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
                    # Plus every location a client uses: a location with no
                    # stored token (lost on a reconnect) gets its first one
                    # here, rather than never.
                    for location_id in await db["client_groups"].distinct(
                        "ghl_location_id", {"user_id": user_id, "ghl_location_id": {"$nin": [None, ""]}}
                    ):
                        subaccount_tokens.setdefault(location_id, {})

                    if subaccount_tokens:
                        logger.info(f"🔄 Regenerating {len(subaccount_tokens)} location tokens for user {user_id}")

                        if not company_id or not agency_access_token:
                            logger.error(f"❌ Missing company_id or access_token for user {user_id}")
                            failed_count += len(subaccount_tokens)
                            continue

                        for location_id, token_data in subaccount_tokens.items():
                            try:
                                logger.info(f"🔄 Regenerating token for location {location_id}")

                                # ✅ FIX: Generate new location token instead of refreshing
                                success, new_tokens = await ghl_integration.generate_location_token(
                                    company_id,
                                    location_id,
                                    agency_access_token
                                )

                                if success:
                                    # Fetch location details
                                    location_details = await fetch_location_details(
                                        location_id,
                                        new_tokens.get("access_token")
                                    )

                                    # Fetch ONLY contact count (lightweight)
                                    contact_count = await get_contact_count_from_ghl(
                                        location_id,
                                        new_tokens.get("access_token")
                                    )

                                    # Save new tokens
                                    await save_subaccount_token(
                                        user_id,
                                        location_id,
                                        new_tokens,
                                        mongo_client,
                                        location_details,
                                        contact_count=contact_count
                                    )

                                    logger.info(
                                        f"✅ Regenerated token for location {location_id} "
                                        f"with {contact_count} contacts"
                                    )
                                    refreshed_count += 1
                                else:
                                    logger.error(
                                        f"❌ Failed to regenerate token for location {location_id}: "
                                        f"{new_tokens.get('error')}"
                                    )
                                    failed_count += 1

                            except Exception as e:
                                logger.error(
                                    f"❌ Error processing location {location_id}: {str(e)}",
                                    exc_info=True
                                )
                                failed_count += 1

                except Exception as e:
                    logger.error(f"❌ Error processing user {user_id}: {str(e)}", exc_info=True)
                    failed_count += 1

            logger.info(
                f"✅ Token refresh job completed: "
                f"{refreshed_count} refreshed, {skipped_count} skipped, {failed_count} failed"
            )

        except Exception as e:
            logger.error(f"❌ Critical error in token refresh job: {str(e)}", exc_info=True)
