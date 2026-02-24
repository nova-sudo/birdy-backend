from fastapi import FastAPI, Request, Response, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import json
import urllib.parse
import logging
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv
import bcrypt
import jwt as pyjwt
import time
from integrations.facebook_utils.facebook_ads import (
  create_ad_insights_indexes,
  fetch_and_cache_ad_insights
)
from integrations.gohighlevel import (ghl_integration, get_agency_token, get_subaccount_tokens, save_agency_token, save_subaccount_token, fetch_location_details,
    get_contact_count_from_ghl, fetch_ghl_contacts_on_demand, )
from integrations.facebook_utils.facebook import facebook_integration, save_facebook_token, get_facebook_token
from integrations.hotprospector import (
    save_hotprospector_credentials,
    get_hotprospector_credentials,
    save_hotprospector_leads_to_collection,
    get_hotprospector_leads_from_collection,
    HotProspectorIntegration,
    get_client_group_mapping , # NEW IMPORT

)
from integrations.facebook_utils.facebook_leads import (
    fetch_and_cache_facebook_leads_FIXED,
    create_facebook_leads_indexes
)
from integrations.facebook_utils.facebook_campaigns import (
    FacebookCampaignFetcher,
    save_campaign_insights_to_db,
    create_campaign_insights_indexes,
    fetch_and_cache_campaign_insights,
)
from integrations.facebook_utils.facebook_adsets import (
    create_adset_insights_indexes,
    fetch_and_cache_adset_insights
)
from utils.cache_helpers import create_performance_indexes
from integrations.facebook_utils.facebook_optimized import (
    MetaDataFetcher,
    fetch_all_accounts_parallel
)
from bson import ObjectId
from typing import Optional
import hashlib
import hmac
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import timedelta
import asyncio

from contextlib import asynccontextmanager


# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



app = FastAPI()
scheduler = AsyncIOScheduler()

_mongo_client = None
_mongo_client_lock = asyncio.Lock()
_connection_attempt = 0
_last_connection_error = None

async def fetch_meta_data_for_group_with_retry(
        meta_ad_account_id: str,
        current_user: str,
        mongo_client,
        facebook_ad_accounts: list,
        max_retries: int = 3
):
    """
    Enhanced Meta data fetching with retry logic
    """
    for retry in range(max_retries):
        try:
            result = await fetch_meta_data_for_group(
                meta_ad_account_id,
                current_user,
                mongo_client,
                facebook_ad_accounts
            )

            if result:
                return result

            # If no result and not last retry, wait and try again
            if retry < max_retries - 1:
                wait_time = 2 ** retry
                logger.warning(f"⏳ No data for {meta_ad_account_id}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited
                wait_time = 5 * (2 ** retry)
                logger.warning(f"⏳ Rate limited on {meta_ad_account_id}, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            elif e.response.status_code >= 500:
                # Server error, retry
                wait_time = 2 ** retry
                logger.warning(f"⏳ Server error for {meta_ad_account_id}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                # Client error, don't retry
                logger.error(f"❌ Client error for {meta_ad_account_id}: {e.response.status_code}")
                return None

        except Exception as e:
            if retry < max_retries - 1:
                wait_time = 2 ** retry
                logger.warning(f"⏳ Error for {meta_ad_account_id}: {e}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"❌ Failed to fetch Meta data for {meta_ad_account_id} after {max_retries} retries: {e}")
                return None

    return None


# REPLACE THIS IN main.py

# Find this function in main.py:
# async def refresh_meta_data_for_all_users():

# Replace it with this:

async def refresh_meta_data_for_all_users():
    """
    INCREMENTAL: Refresh ONLY TODAY's Meta data for all users

    Strategy:
    - Leads: Fetch date_preset=today, stop at last known lead
    - Insights: Update only today's records (date_preset=today)

    Expected speedup: 10-50x faster than full refresh
    """
    async with get_mongo_client() as mongo_client:
        start_time = datetime.utcnow()
        success_count = 0
        failure_count = 0

        try:
            logger.info("🔄 Starting INCREMENTAL Meta data refresh (today only)")
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            # Get all unique users with Meta ad accounts
            pipeline = [
                {"$match": {"meta_ad_account_id": {"$exists": True, "$ne": None}}},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}}
            ]

            users_with_groups = await client_groups_collection.aggregate(pipeline).to_list(None)

            if not users_with_groups:
                logger.info("✅ No users with Meta ad accounts found")
                return

            logger.info(f"📊 Found {len(users_with_groups)} users with Meta ad accounts")

            # ============================================
            # 🔥 PROCESS USERS SEQUENTIALLY
            # ============================================
            for user_index, user_data in enumerate(users_with_groups, 1):
                user_id = user_data["_id"]
                groups = user_data["groups"]

                try:
                    logger.info(
                        f"🔄 [{user_index}/{len(users_with_groups)}] "
                        f"Processing user {user_id} ({len(groups)} ad accounts)"
                    )

                    # Check token validity
                    facebook_token = await get_facebook_token(user_id, mongo_client)

                    if not facebook_token or not facebook_token.get("access_token"):
                        logger.warning(f"⚠️ User {user_id} has invalid Facebook token, skipping")
                        failure_count += len(groups)
                        continue

                    access_token = facebook_token["access_token"]

                    # Process each group's Meta account
                    for group in groups:
                        if not group.get("meta_ad_account_id"):
                            continue

                        group_id = group["id"]
                        meta_ad_account_id = group["meta_ad_account_id"]
                        group_name = group.get("name", "Unknown Group")

                        try:
                            logger.info(
                                f"  🔄 Updating TODAY's data for '{group_name}' "
                                f"(account: {meta_ad_account_id})"
                            )

                            # ============================================
                            # STEP 1: UPDATE TODAY'S INSIGHTS (parallel)
                            # ============================================
                            from integrations.facebook_utils.meta_incremental_refresh import (
                                update_todays_campaign_insights,
                                update_todays_adset_insights,
                                update_todays_ad_insights,
                                fetch_todays_facebook_leads_incremental
                            )

                            tasks = [
                                update_todays_campaign_insights(
                                    meta_ad_account_id,
                                    access_token,
                                    user_id,
                                    group_id,
                                    group_name,
                                    mongo_client
                                ),
                                update_todays_adset_insights(
                                    meta_ad_account_id,
                                    access_token,
                                    user_id,
                                    group_id,
                                    group_name,
                                    mongo_client
                                ),
                                update_todays_ad_insights(
                                    meta_ad_account_id,
                                    access_token,
                                    user_id,
                                    group_id,
                                    group_name,
                                    mongo_client
                                )
                            ]

                            insights_results = await asyncio.gather(*tasks, return_exceptions=True)

                            campaign_count = insights_results[0] if not isinstance(insights_results[0],
                                                                                   Exception) else 0
                            adset_count = insights_results[1] if not isinstance(insights_results[1], Exception) else 0
                            ad_count = insights_results[2] if not isinstance(insights_results[2], Exception) else 0

                            # ============================================
                            # STEP 2: UPDATE TODAY'S LEADS (incremental)
                            # ============================================
                            new_leads_count, new_leads = await fetch_todays_facebook_leads_incremental(
                                meta_ad_account_id,
                                access_token,
                                user_id,
                                group_id,
                                group_name,
                                mongo_client,
                                max_concurrent_ads=5
                            )

                            # Save new leads to database
                            if new_leads:
                                leads_collection = db["facebook_leads"]
                                lead_docs = []

                                for lead in new_leads:
                                    lead_docs.append({
                                        "user_id": user_id,
                                        "ad_account_id": meta_ad_account_id,
                                        "client_group_id": group_id,
                                        "client_group_name": group_name,
                                        "lead_id": lead.get("lead_id"),
                                        "lead_data": lead,
                                        "created_at": datetime.now(),
                                        "updated_at": datetime.now()
                                    })

                                if lead_docs:
                                    try:
                                        await leads_collection.insert_many(
                                            lead_docs,
                                            ordered=False
                                        )
                                        logger.info(f"  💾 Saved {len(lead_docs)} new leads")
                                    except Exception as e:
                                        logger.warning(f"  ⚠️ Some duplicate leads: {str(e)}")

                            # ============================================
                            # STEP 3: UPDATE CACHE
                            # ============================================
                            await client_groups_collection.update_one(
                                {"id": group_id},
                                {
                                    "$set": {
                                        "last_meta_refresh": datetime.utcnow(),
                                        "last_meta_refresh_mode": "incremental_today"
                                    },
                                    "$inc": {
                                        "facebook_cache.total_leads": new_leads_count
                                    }
                                }
                            )

                            logger.info(
                                f"  ✅ Updated '{group_name}': "
                                f"{campaign_count} campaigns, {adset_count} adsets, "
                                f"{ad_count} ads, {new_leads_count} new leads"
                            )

                            success_count += 1

                        except Exception as e:
                            logger.error(
                                f"  ❌ Error updating '{group_name}': {str(e)}",
                                exc_info=True
                            )
                            failure_count += 1

                    # ============================================
                    # 🔥 DELAY BETWEEN USERS
                    # ============================================
                    if user_index < len(users_with_groups):
                        await asyncio.sleep(1)
                        logger.info(f"⏳ Waiting 1s before next user...")

                except Exception as e:
                    failure_count += len(groups)
                    logger.error(f"❌ Error refreshing Meta data for user {user_id}: {e}", exc_info=True)

            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"✅ INCREMENTAL Meta refresh completed in {elapsed:.2f}s - "
                f"Success: {success_count}, Failed: {failure_count}"
            )

        except Exception as e:
            logger.error(f"❌ Critical error in Meta refresh job: {e}", exc_info=True)

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
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
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


def start_background_jobs():
    """Start all background refresh jobs with proper scheduling for serverless"""

    # Only start if not already running (prevents duplicates in serverless)
    if scheduler.running:
        logger.info("Scheduler already running, skipping initialization")
        return

    # Token refresh: runs every 12 hours at :00 minutes
    # scheduler.add_job(
    #     refresh_tokens_for_all_users,
    #     CronTrigger( minute='32'),
    #     id='token_refresh',
    #     replace_existing=True,
    #     max_instances=1
    # )

    # GHL data refresh: runs hourly at :05 (waits for token refresh to complete)
    # scheduler.add_job(
    #     refresh_ghl_data_for_all_users,
    #     CronTrigger(minute='34'),
    #     id='ghl_refresh',
    #     replace_existing=True,
    #     max_instances=2
    #
    # )
    #
    # # Meta data refresh: runs hourly at :25 (staggered)
    # scheduler.add_job(
    #     refresh_meta_data_for_all_users,
    #     CronTrigger(minute='19'),
    #     id='meta_refresh',
    #     replace_existing=True,
    #     max_instances=1
    # )

    # # HP data refresh: runs hourly at :45 (staggered)
    # scheduler.add_job(
    #     refresh_hp_data_for_all_users,
    #     CronTrigger(minute='10'),
    #     id='hp_refresh',
    #     replace_existing=True,
    #     max_instances=1
    # )

    scheduler.start()
    logger.info("🚀 Background refresh jobs started with cron scheduling")


def stop_background_jobs():
    """Gracefully stop all background jobs"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background jobs stopped")



async def populate_cache_for_existing_groups():
    """
    Run once on startup to populate cache for existing groups that don't have cache data.
    This ensures all groups have data immediately.
    """
    async with get_mongo_client() as mongo_client:
        try:
            logger.info("🔄 Starting cache population for existing client groups...")
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
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



# Utility function to convert MongoDB documents to JSON-serializable format
def mongo_to_dict(obj):
    if isinstance(obj, dict):
        return {k: mongo_to_dict(v) for k, v in obj.items() if k != "_id"}
    elif isinstance(obj, list):
        return [mongo_to_dict(item) for item in obj]
    elif isinstance(obj, ObjectId):
        return str(obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    return obj


@app.on_event("startup")
async def startup_event():
    """Run once when server starts"""
    async with get_mongo_client() as client:
        # Create indexes
        await create_performance_indexes(client)
        await create_facebook_leads_indexes(client)
        await create_campaign_insights_indexes(client)
        await create_adset_insights_indexes(client)
        await create_ad_insights_indexes(client)

        # Start background jobs ONCE
        start_background_jobs()

        # Populate cache for existing groups (run in background, non-blocking)
        asyncio.create_task(populate_cache_for_existing_groups())

    logger.info("🚀 Server started with performance optimizations and cache population")


@app.on_event("shutdown")
async def shutdown_event():
    scheduler.shutdown()
    logger.info("🛑 Background jobs stopped")

# Cookie configuration - set via environment variables
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "none")  # "none", "lax", or "strict"
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)  # Optional: set domain for cookies


# Helper function to set cookies
def set_cookie(response, key, value, max_age):
    """Set a cookie with flexible settings"""
    response.set_cookie(
        key=key,
        value=value,
        max_age=max_age,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        domain=COOKIE_DOMAIN
    )


@asynccontextmanager
async def get_mongo_client():
    client = None
    try:
        loop = asyncio.get_event_loop()
        mongo_uri = os.getenv("MONGODB_URI")
        if not mongo_uri:
            logger.error("MONGODB_URI environment variable is not set")
            raise HTTPException(status_code=500, detail="MongoDB configuration error: MONGODB_URI is not set")
        logger.debug(f"Creating MongoDB client with event loop: {loop}, URI: {mongo_uri[:10]}... (redacted)")
        client = AsyncIOMotorClient(mongo_uri, io_loop=loop)
        await client.admin.command("ping")
        logger.debug("MongoDB connection established successfully")
        yield client
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in MongoDB client setup: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error setting up MongoDB client: {str(e)}")
    finally:
        if client:
            logger.debug("Closing MongoDB client")
            client.close()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://birdy-beta.vercel.app", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# JWT configuration
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_MINUTES = 600
JWT_REFRESH_SECRET = os.getenv("JWT_REFRESH_SECRET", JWT_SECRET + "_refresh")
JWT_REFRESH_EXPIRY_DAYS = 30

# Verify JWT secrets
if not JWT_SECRET or not isinstance(JWT_SECRET, str) or JWT_SECRET.strip() == "":
    logger.error("JWT_SECRET is not set or invalid in .env file")
    raise ValueError("JWT_SECRET environment variable must be a non-empty string")
if not JWT_REFRESH_SECRET or not isinstance(JWT_REFRESH_SECRET, str) or JWT_REFRESH_SECRET.strip() == "":
    logger.error("JWT_REFRESH_SECRET is not set or invalid in .env file")
    raise ValueError("JWT_REFRESH_SECRET environment variable must be a non-empty string")

# Log JWT module info for debugging
logger.info(f"JWT module: {pyjwt.__file__}, version: {pyjwt.__version__}")

async def get_current_user(request: Request):
    async with get_mongo_client() as mongo_client:
        token = request.cookies.get("auth_token")
        if not token:
            raise HTTPException(status_code=401, detail="No authentication token provided")
        try:
            payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            email = payload.get("sub")
            if not email:
                raise HTTPException(status_code=401, detail="Invalid token")
            return email
        except pyjwt.ExpiredSignatureError:
            logger.info("Access token expired, checking refresh token")
            refresh_token = request.cookies.get("refresh_token")
            if not refresh_token:
                logger.info("No refresh token provided, requiring re-authentication")
                raise HTTPException(status_code=401, detail="Access token expired, please log in again")
            try:
                refresh_payload = pyjwt.decode(refresh_token, JWT_REFRESH_SECRET, algorithms=[JWT_ALGORITHM])
                email = refresh_payload.get("sub")
                if not email or refresh_payload.get("type") != "refresh":
                    raise HTTPException(status_code=401, detail="Invalid refresh token")
                access_token, new_refresh_token = await generate_tokens(email)
                request.state.new_tokens = {
                    "auth_token": access_token,
                    "refresh_token": new_refresh_token
                }
                return email
            except pyjwt.PyJWTError as e:
                logger.error(f"Refresh token decode error: {str(e)}")
                raise HTTPException(status_code=401, detail="Invalid refresh token")
        except pyjwt.PyJWTError as e:
            logger.error(f"JWT decode error: {str(e)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token")



# Pydantic models
class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str
    rememberMe: bool = False

class LinkClientRequest(BaseModel):
    client_name: str
    ghl_location_id: str
    meta_ad_account_id: str
    hotprospector_group_id: str

class ClientGroupRequest(BaseModel):
    name: str
    ghl_location_id: str | None
    meta_ad_account_id: str | None
    hotprospector_group_id: str | None
    notes: str | None = ""

async def generate_tokens(email: str):
    logger.debug(f"Generating tokens for {email} with event loop: {asyncio.get_event_loop()}")
    try:
        exp_timestamp = int((datetime.utcnow() + timedelta(minutes=JWT_EXPIRY_MINUTES)).timestamp())
        access_payload = {"sub": email, "exp": exp_timestamp, "type": "access"}
        access_token = pyjwt.encode(access_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        refresh_exp_timestamp = int((datetime.utcnow() + timedelta(days=JWT_REFRESH_EXPIRY_DAYS)).timestamp())
        refresh_payload = {"sub": email, "exp": refresh_exp_timestamp, "type": "refresh"}
        refresh_token = pyjwt.encode(refresh_payload, JWT_REFRESH_SECRET, algorithm=JWT_ALGORITHM)
        logger.debug(f"Generated tokens for {email}: access_token={access_token[:10]}..., refresh_token={refresh_token[:10]}...")
        return access_token, refresh_token
    except Exception as e:
        logger.error(f"Error generating JWT tokens for {email}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to generate tokens: {str(e)}")


async def refresh_tokens_for_all_users():
    """
    FIXED: Regenerate location tokens instead of refreshing with invalid tokens
    """
    async with get_mongo_client() as mongo_client:
        try:
            logger.info("🔄 Starting token refresh job for all users")
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
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
                    # STEP 1: Refresh agency token
                    # ============================================
                    agency_token = await get_agency_token(user_id, mongo_client)

                    # ============================================
                    # STEP 1: Refresh agency token and get fresh credentials
                    # ============================================
                    if not agency_token or not agency_token.get("refresh_token"):
                        logger.error(f"❌ No agency token found for user {user_id}")
                        failed_count += 1
                        continue

                    logger.info(f"🔄 Refreshing agency token for user {user_id}")
                    refresh_token = agency_token.get("refresh_token")
                    success, result = await ghl_integration.refresh_agency_token(refresh_token)

                    if not success:
                        logger.error(f"❌ Failed to refresh agency token for user {user_id}: {result.get('error')}")
                        failed_count += 1
                        continue  # Skip location tokens if agency token failed

                    # Save the refreshed agency token
                    await save_agency_token(user_id, result, mongo_client)
                    logger.info(f"✅ Refreshed agency token for user {user_id}")
                    refreshed_count += 1

                    # ✅ FIX: Use the FRESH tokens from the result
                    company_id = result.get("companyId")  # Note: API returns "companyId"
                    agency_access_token = result.get("access_token")

                    # Verify we have the required credentials
                    if not company_id or not agency_access_token:
                        logger.error(
                            f"❌ Missing credentials after refresh for user {user_id}: "
                            f"company_id={company_id}, has_token={bool(agency_access_token)}"
                        )
                        # Try to get from saved data
                        fresh_agency_token = await get_agency_token(user_id, mongo_client)
                        if fresh_agency_token:
                            company_id = fresh_agency_token.get("company_id")
                            agency_access_token = fresh_agency_token.get("access_token")

                        if not company_id or not agency_access_token:
                            failed_count += len(await get_subaccount_tokens(user_id, mongo_client))
                            continue

                    # ============================================
                    # STEP 2: Regenerate location tokens (not refresh!)
                    # ============================================
                    subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)

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

# ============================================
# AUTH CHECK ENDPOINT (for frontend to verify login)
# ============================================
@app.get("/api/auth/check")
async def check_auth(current_user: str = Depends(get_current_user)):
    """Simple endpoint to check if user is authenticated"""
    return {
        "authenticated": True,
        "user": current_user
    }

# Register endpoint
@app.post("/api/register")
async def register_user(request: RegisterRequest, response: Response):
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]
            existing_user = await users_collection.find_one({"user_id": request.email})
            if existing_user:
                raise HTTPException(status_code=400, detail="Email already registered")
            hashed_password = bcrypt.hashpw(request.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            user_doc = {
                "user_id": request.email,
                "name": request.name,
                "password": hashed_password,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "integrations": {}
            }
            await users_collection.insert_one(user_doc)
            logger.info(f"Registered user: {request.email}")

            access_token, refresh_token = await generate_tokens(request.email)

            set_cookie(response, "auth_token", access_token, JWT_EXPIRY_MINUTES * 60)
            set_cookie(response, "refresh_token", refresh_token, JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60)

            logger.info(f"Set auth_token and refresh_token cookies for user: {request.email}")
            return {"message": "Registration successful", "user": {"email": request.email, "name": request.name}}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error registering user: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to register user: {str(e)}")


@app.post("/api/login")
async def login_user(request: LoginRequest, response: Response):
    async with get_mongo_client() as mongo_client:
        try:
            logger.debug(f"Starting login for user {request.email}, rememberMe: {request.rememberMe}")
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]

            # Verify user credentials
            logger.debug(f"Querying user with email {request.email}")
            user_doc = await users_collection.find_one({"user_id": request.email})

            if not user_doc:
                logger.warning(f"No user found with email {request.email}")
                raise HTTPException(status_code=401, detail="Invalid email or password")

            logger.debug(f"User found: {user_doc['user_id']}, verifying password")
            if not bcrypt.checkpw(request.password.encode('utf-8'), user_doc["password"].encode('utf-8')):
                logger.warning(f"Password mismatch for user {request.email}")
                raise HTTPException(status_code=401, detail="Invalid email or password")

            # # ============================================
            # # CHECK AND REFRESH EXPIRED TOKENS (if needed)
            # # ============================================
            # expired_tokens = []
            #
            # # Check agency token expiration
            # logger.debug(f"Checking agency token for user {request.email}")
            # agency_token = await get_agency_token(request.email, mongo_client)
            #
            # if agency_token and agency_token.get("refresh_token"):
            #     expires_at = agency_token.get("expires_at")
            #
            #     # Check if expired or expires within 5 minutes
            #     if expires_at:
            #         time_until_expiry = (expires_at - datetime.now()).total_seconds()
            #         if time_until_expiry < 300:  # Less than 5 minutes
            #             expired_tokens.append(("agency", None))
            #             logger.info(f"⚠️ Agency token expired or expiring soon for {request.email}")
            #     else:
            #         # No expiry date, consider it expired
            #         expired_tokens.append(("agency", None))
            #
            # # Check location tokens
            # logger.debug(f"Checking subaccount tokens for user {request.email}")
            # subaccount_tokens = await get_subaccount_tokens(request.email, mongo_client)
            #
            # for location_id, token_data in subaccount_tokens.items():
            #     refresh_token = token_data.get("refresh_token")
            #     expires_at = token_data.get("expires_at")
            #
            #     if refresh_token and expires_at:
            #         time_until_expiry = (expires_at - datetime.now()).total_seconds()
            #         if time_until_expiry < 300:  # Less than 5 minutes
            #             expired_tokens.append(("location", location_id))
            #             logger.info(f"⚠️ Token expired or expiring soon for location {location_id}")
            #     elif refresh_token and not expires_at:
            #         # No expiry date, consider it expired
            #         expired_tokens.append(("location", location_id))
            #
            # # Refresh only expired tokens
            # if expired_tokens:
            #     logger.info(f"🔄 Refreshing {len(expired_tokens)} expired tokens for {request.email}")
            #
            #     for token_type, location_id in expired_tokens:
            #         try:
            #             if token_type == "agency":
            #                 refresh_token = agency_token.get("refresh_token")
            #                 logger.debug(f"Refreshing agency token for user {request.email}")
            #                 success, result = await ghl_integration.refresh_agency_token(refresh_token)
            #
            #                 if success:
            #                     await save_agency_token(request.email, result, mongo_client)
            #                     logger.info(f"✅ Refreshed agency token for user {request.email}")
            #                 else:
            #                     logger.error(f"❌ Failed to refresh agency token: {result.get('error')}")
            #
            #             elif token_type == "location":
            #                 token_data = subaccount_tokens.get(location_id, {})
            #                 refresh_token = token_data.get("refresh_token")
            #
            #                 if refresh_token:
            #                     logger.debug(f"Refreshing token for location {location_id}")
            #                     success, result = await ghl_integration.refresh_location_token(
            #                         location_id,
            #                         refresh_token
            #                     )
            #
            #                     if success:
            #                         location_details = await fetch_location_details(
            #                             location_id,
            #                             result.get("access_token")
            #                         )
            #
            #                         success_contacts, contacts = await ghl_integration.fetch_location_contacts(
            #                             location_id,
            #                             result.get("access_token")
            #                         )
            #
            #                         if not success_contacts:
            #                             logger.warning(f"Failed to fetch contacts for location {location_id}")
            #                             contacts = []
            #
            #                         await save_subaccount_token(
            #                             request.email,
            #                             location_id,
            #                             result,
            #                             mongo_client,
            #                             location_details,
            #                             contacts
            #                         )
            #                         logger.info(f"✅ Refreshed token for location {location_id}")
            #                     else:
            #                         logger.error(
            #                             f"❌ Failed to refresh token for location {location_id}: {result.get('error')}")
            #
            #         except Exception as e:
            #             logger.error(f"❌ Error refreshing token: {str(e)}", exc_info=True)
            # else:
            #     logger.info(f"✅ All tokens valid for user {request.email}, no refresh needed")

            # ============================================
            # GENERATE JWT TOKENS
            # ============================================
            logger.debug(f"Generating JWT tokens for {request.email}")
            access_token, refresh_token = await generate_tokens(request.email)

            # Calculate max_age based on remember_me
            access_token_max_age = (JWT_EXPIRY_MINUTES * 60) if not request.rememberMe else (30 * 24 * 60 * 60)
            refresh_token_max_age = (JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60) if not request.rememberMe else (
                        90 * 24 * 60 * 60)

            logger.debug(
                f"Setting cookies: access_token_max_age={access_token_max_age}, "
                f"refresh_token_max_age={refresh_token_max_age}"
            )

            set_cookie(response, "auth_token", access_token, access_token_max_age)
            set_cookie(response, "refresh_token", refresh_token, refresh_token_max_age)

            # logger.info(
            #     f"✅ User logged in successfully: {request.email}, "
            #     f"refreshed {len(expired_tokens)} expired tokens"
            # )

            return {
                "message": "Login successful",
                "user": {
                    "email": request.email,
                    "name": user_doc["name"]
                },
                # "tokens_refreshed": len(expired_tokens)
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error logging in user {request.email}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to login user: {str(e)}")



@app.post("/api/logout")
async def logout_user(response: Response):
    response.delete_cookie(
        key="auth_token",
        path="/",
        domain=COOKIE_DOMAIN,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE
    )
    response.delete_cookie(
        key="refresh_token",
        path="/",
        domain=COOKIE_DOMAIN,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE
    )
    logger.info("User logged out, auth_token and refresh_token cleared")
    return {"message": "Logout successful"}# Middleware to set new tokens if refreshed

@app.middleware("http")
async def set_new_tokens(request: Request, call_next):
    response = await call_next(request)
    if hasattr(request.state, "new_tokens"):
        for key, value in request.state.new_tokens.items():
            max_age = JWT_EXPIRY_MINUTES * 60 if key == "auth_token" else JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60
            response.set_cookie(
                key=key,
                value=value,
                httponly=True,
                secure=True,
                samesite="none",
                max_age=max_age
            )
    return response

# GoHighLevel Integration Endpoints
@app.get("/api/connect")
async def connect(current_user: str = Depends(get_current_user)):
    auth_url = ghl_integration.generate_auth_url()
    logger.info(f"Generated auth URL for GoHighLevel agency flow for user: {current_user}")
    return {"auth_url": auth_url}

@app.get("/oauth/callback")
async def oauth_callback(code: str, response: Response, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        logger.info(f"Received GoHighLevel callback with code: {code} for user: {current_user}")
        success, result = await ghl_integration.exchange_code_for_token(code)
        if not success:
            logger.error(f"GoHighLevel OAuth callback error: {result}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(result.get('error', 'OAuth callback failed'))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        try:
            await save_agency_token(current_user, result, mongo_client)
        except Exception as e:
            logger.error(f"Failed to save GoHighLevel agency tokens: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        result_for_cookie = result.copy()
        try:
            cookie_value = json.dumps(result_for_cookie)
            response.set_cookie(
                key="gohighlevel_tokens",
                value=cookie_value,
                httponly=True,
                secure=True,
                samesite="none",
                max_age=result.get("expires_in", 3600)
            )
        except Exception as e:
            logger.error(f"Failed to set GoHighLevel cookie: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=success&tokens={urllib.parse.quote(cookie_value)}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

@app.get("/api/subaccount/locations")
async def get_subaccount_locations(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            agency_token = await get_agency_token(current_user, mongo_client)
            if not agency_token:
                raise HTTPException(status_code=400, detail="No agency token available. Please connect GoHighLevel in Settings.")
            company_id = agency_token.get("company_id")
            access_token = agency_token.get("access_token")
            if not company_id or not access_token:
                raise HTTPException(status_code=400, detail="Invalid agency token")
            success, locations = await ghl_integration.fetch_locations(company_id, access_token)
            if not success:
                status_code = locations.get("status_code", 400)
                if status_code == 404:
                    raise HTTPException(status_code=404, detail="No locations available or incorrect GoHighLevel API configuration.")
                raise HTTPException(status_code=status_code, detail=f"Failed to fetch locations: {locations.get('error', 'Unknown error')}")
            return {
                "locations": locations,
                "message": f"Successfully fetched {len(locations)} locations"
            }
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error fetching locations for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Invalid response from GoHighLevel API: {str(e)}")
        except Exception as e:
            logger.error(f"Error fetching locations for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch locations: {str(e)}")


@app.post("/api/add-subaccount")
async def add_subaccount(request: Request, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            location_id = body.get("location_id")
            if not location_id:
                raise HTTPException(status_code=400, detail="location_id is required")

            agency_token = await get_agency_token(current_user, mongo_client)
            if not agency_token:
                raise HTTPException(status_code=400,
                                    detail="No agency token available. Please connect GoHighLevel in Settings.")

            company_id = agency_token.get("company_id")
            access_token = agency_token.get("access_token")
            if not company_id or not access_token:
                raise HTTPException(status_code=400, detail="Invalid agency token")

            success, loc_tokens = await ghl_integration.generate_location_token(company_id, location_id, access_token)
            if not success:
                raise HTTPException(status_code=400,
                                    detail=f"Failed to generate location token: {loc_tokens.get('error', 'Unknown error')}")

            location_details = await fetch_location_details(location_id, loc_tokens.get("access_token"))

            # ✅ FIX: Only fetch contact COUNT, not full contacts
            contact_count = await get_contact_count_from_ghl(location_id, loc_tokens.get("access_token"))

            # ✅ FIX: Save only count, not contacts array
            await save_subaccount_token(
                current_user,
                location_id,
                loc_tokens,
                mongo_client,
                location_details,
                contact_count=contact_count
            )

            return {
                "success": True,
                "location_id": location_id,
                "location_details": location_details,
                "contact_count": contact_count  # Return count, not contacts
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding subaccount for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to add subaccount: {str(e)}")


# Meta Integration Endpoints
@app.get("/api/connect/facebook")
async def connect_facebook(current_user: str = Depends(get_current_user)):
    auth_url = facebook_integration.generate_auth_url()
    logger.info(f"Generated auth URL for Meta OAuth flow for user: {current_user}")
    return {"auth_url": auth_url}

@app.get("/oauth/callback/facebook")
async def oauth_callback_facebook(code: str, response: Response, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        logger.info(f"Received Meta callback with code: {code} for user: {current_user}")
        success, result = await facebook_integration.exchange_code_for_token(code)
        if not success:
            logger.error(f"Meta OAuth callback error: {result}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(result.get('error', 'Meta OAuth callback failed'))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        try:
            await save_facebook_token(current_user, result, mongo_client)
        except Exception as e:
            logger.error(f"Failed to save Meta tokens: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        result_for_cookie = result.copy()
        try:
            cookie_value = json.dumps(result_for_cookie)
            response.set_cookie(
                key="facebook_tokens",
                value=cookie_value,
                httponly=True,
                secure=True,
                samesite="none",
                max_age=result.get("expires_in", 60 * 24 * 60 * 60)
            )
        except Exception as e:
            logger.error(f"Failed to set Meta cookie: {str(e)}")
            redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=error&error={urllib.parse.quote(str(e))}"
            response.headers["Location"] = redirect_url
            response.status_code = 302
            return {"redirect_url": redirect_url}
        redirect_url = f"https://birdy-beta.vercel.app/settings?tab=integrations&status=success&tokens={urllib.parse.quote(cookie_value)}"
        response.headers["Location"] = redirect_url
        response.status_code = 302
        return {"redirect_url": redirect_url}

@app.get("/api/status")
async def api_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            agency_token = await get_agency_token(current_user, mongo_client)
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
            facebook_token = await get_facebook_token(current_user, mongo_client)
            status = {
                "connected": bool(agency_token or facebook_token),
                "gohighlevel": {
                    "agency": {},
                    "subaccounts": {}
                },
                "facebook": {}
            }
            if agency_token:
                expires_at = agency_token.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["gohighlevel"]["agency"] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired,
                    "company_id": agency_token.get("company_id")
                }
            for location_id, token_data in subaccount_tokens.items():
                expires_at = token_data.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["gohighlevel"]["subaccounts"][location_id] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired,
                    "name": token_data.get("name", "Unknown Location")
                }
            if facebook_token:
                expires_at = facebook_token.get("expires_at")
                is_expired = expires_at and datetime.now() >= expires_at
                status["facebook"] = {
                    "connected": True,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                    "token_expired": is_expired
                }
            return status
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting status for user {current_user}: {str(e)}")
            return {
                "connected": False,
                "error": str(e),
                "gohighlevel": {"agency": {}, "subaccounts": {}},
                "facebook": {}
            }

@app.get("/api/location-data")
async def get_all_location_data(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
            if not subaccount_tokens:
                return {"locations": [], "message": "No subaccount tokens found"}
            locations_data = []
            for location_id, token_data in subaccount_tokens.items():
                combined_data = {
                    "id": location_id,
                    "location_id": location_id,
                    "token_expires_at": token_data.get("expires_at").isoformat() if token_data.get("expires_at") else None,
                    "token_expired": token_data.get("expires_at") and datetime.now() >= token_data.get("expires_at"),
                    "name": token_data.get("name", "Unknown Location"),
                    "address": token_data.get("address"),
                    "isInstalled": token_data.get("isInstalled", False),
                    "trial": token_data.get("trial", {})
                }
                locations_data.append(combined_data)
            return {
                "locations": locations_data,
                "total_count": len(locations_data),
                "message": f"Successfully fetched {len(locations_data)} locations"
            }
        except Exception as e:
            logger.error(f"Error in get_all_location_data for user {current_user}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch location data: {str(e)}")


async def save_facebook_data(user_id: str, account_id: str, data: dict, data_type: str, mongo_client: AsyncIOMotorClient):
    try:
        data_doc = {
            "data": data,
            "updated_at": datetime.now(),
            "created_at": datetime.now()
        }
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        await db["users"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    f"integrations.facebook.accounts.{account_id}.{data_type}": data_doc,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )
        logger.info(f"Saved Meta {data_type} data for account {account_id} for user: {user_id}")
    except Exception as e:
        logger.error(f"Failed to save Meta {data_type} data for account {account_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save {data_type} data: {str(e)}")

async def get_facebook_data(user_id: str, account_id: str, data_type: str, mongo_client: AsyncIOMotorClient):
    db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
    user_doc = await db["users"].find_one({"user_id": user_id})
    if (
        user_doc
        and user_doc.get("integrations", {})
        .get("facebook", {})
        .get("accounts", {})
        .get(account_id, {})
        .get(data_type)
    ):
        data_doc = user_doc["integrations"]["facebook"]["accounts"][account_id][data_type]
        updated_at = data_doc.get("updated_at")
        cache_valid = updated_at and (datetime.now() - updated_at).total_seconds() < 300
        if cache_valid:
            logger.info(f"Returning cached Meta {data_type} data for account {account_id} for user: {user_id}")
            return data_doc["data"]
    return None


@app.get("/api/facebook/adaccounts")
async def get_facebook_adaccounts(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            token = await get_facebook_token(current_user, mongo_client)
            if not token or not token.get("access_token"):
                raise HTTPException(
                    status_code=400,
                    detail="No Meta token available. Please connect Meta in Settings."
                )

            # Fetch directly from Meta API - no caching
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.get(
                    "https://graph.facebook.com/v23.0/me/adaccounts",
                    params={
                        "fields": "name,currency,created_time,owner",
                        "access_token": token["access_token"],
                        "limit": 1000
                    }
                )

                if response.status_code != 200:
                    error_detail = response.json().get("error", {})
                    logger.error(f"Meta API error: {response.status_code} - {error_detail}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=error_detail.get("message", "Failed to fetch ad accounts")
                    )

                data = response.json()
                accounts = data.get("data", [])

                logger.info(f"✅ Fetched {len(accounts)} ad accounts for user {current_user}")

                return {
                    "data": data,
                    "meta": {"total": len(accounts)},
                    "message": f"Successfully fetched {len(accounts)} ad accounts"
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching ad accounts: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch ad accounts: {str(e)}"
            )



@app.get("/api/facebook-leads/paginated")
async def get_facebook_leads_paginated(
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        groups: str = Query(default=""),
        current_user: str = Depends(get_current_user)
):
    """
    Get Facebook leads in DESCENDING order (newest first) with pagination.
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            leads_collection = db["facebook_leads"]

            # Parse group IDs
            group_ids = [g.strip() for g in groups.split(',') if g.strip()] if groups else []

            # Build query
            query = {"user_id": current_user}
            if group_ids:
                query["client_group_id"] = {"$in": group_ids}

            # Get total count
            total_leads = await leads_collection.count_documents(query)

            if total_leads == 0:
                return {
                    "leads": [],
                    "meta": {
                        "total_leads": 0,
                        "current_page": page,
                        "total_pages": 0,
                        "per_page": limit,
                        "has_next": False,
                        "has_prev": False
                    },
                    "message": "No leads found"
                }

            # Calculate pagination
            skip = (page - 1) * limit
            total_pages = (total_leads + limit - 1) // limit

            # Fetch leads (sorted newest first)
            cursor = leads_collection.find(
                query,
                {
                    "lead_data": 1,
                    "client_group_name": 1,
                    "ad_account_id": 1,
                    "client_group_id": 1
                }
            ).sort("lead_data.created_time", -1).skip(skip).limit(limit)

            lead_docs = await cursor.to_list(length=limit)

            # Format leads
            leads = []
            for doc in lead_docs:
                lead_data = doc.get("lead_data", {})
                leads.append({
                    "lead_id": lead_data.get("id"),
                    "full_name": lead_data.get("full_name", ""),
                    "email": lead_data.get("email", ""),
                    "phone_number": lead_data.get("phone_number", ""),
                    "ad_name": lead_data.get("ad_name", ""),
                    "platform": lead_data.get("platform", ""),
                    "created_time": lead_data.get("created_time", ""),
                    "group_name": doc.get("client_group_name", "Unknown Group"),
                    "ad_account_id": doc.get("ad_account_id"),
                    "field_data": lead_data.get("field_data", {})
                })

            elapsed = time.time() - start_time

            logger.info(
                f"⚡ Fetched page {page} (newest first): {len(leads)} leads "
                f"in {elapsed:.3f}s"
            )

            return {
                "leads": leads,
                "meta": {
                    "total_leads": total_leads,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(leads),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "newest_first"
                },
                "message": f"Retrieved {len(leads)} leads (newest first)",
                "performance": {
                    "response_time_ms": int(elapsed * 1000)
                }
            }

        except Exception as e:
            logger.error(f"Error fetching paginated leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


@app.get("/disconnect")
async def disconnect(response: Response, current_user: str = Depends(get_current_user)):
    try:
        response.delete_cookie(
            key="gohighlevel_tokens",
            path="/",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        response.delete_cookie(
            key="facebook_tokens",
            path="/",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        logger.info(f"Cleared integration data for user: {current_user}")
        return {"status": "GoHighLevel and Meta integrations disconnected successfully"}
    except Exception as e:
        logger.error(f"Failed to disconnect integration for user {current_user}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to disconnect integration: {str(e)}")

@app.post("/api/hotprospector/connect")
async def connect_hotprospector(request: Request, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            api_uid = body.get("api_uid")
            api_key = body.get("api_key")
            if not api_uid or not api_key:
                raise HTTPException(status_code=400, detail="api_uid and api_key are required")
            test_integration = HotProspectorIntegration(api_uid, api_key)
            success, result = await test_integration.get_member_users()
            if not success:
                raise HTTPException(status_code=400, detail=f"Invalid credentials: {result.get('error', 'Unknown error')}")
            await save_hotprospector_credentials(current_user, api_uid, api_key, mongo_client)
            return {
                "success": True,
                "message": "Hot Prospector connected successfully"
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error connecting Hot Prospector for user {current_user}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to connect Hot Prospector: {str(e)}")

@app.get("/api/hotprospector/status")
async def hotprospector_status(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            return {
                "connected": bool(credentials and credentials.get("connected")),
                "api_uid": credentials.get("api_uid") if credentials else None
            }
        except Exception as e:
            logger.error(f"Error checking Hot Prospector status for user {current_user}: {str(e)}")
            return {
                "connected": False,
                "error": str(e)
            }


# ============================================
# OPTIMIZED GET ENDPOINT WITH CALL LOGS CACHE
# ============================================

@app.get("/api/hotprospector/leads")
async def get_all_hotprospector_leads(
        skip: int = 0,
        limit: int = 100,
        include_call_logs: bool = True,
        current_user: str = Depends(get_current_user)
):
    """
    CACHE-FIRST WITH CALL LOGS: Always check MongoDB cache first (1-hour freshness).
    Call logs are now cached with leads to avoid repeated API calls.

    Cache Strategy:
    - MongoDB cache: 1 hour (includes call logs)
    - Client cache: 5 minutes (in frontend localStorage)
    - API fetch: Only if MongoDB cache is stale
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            # ============================================
            # STEP 1: Get credentials and locations
            # ============================================
            base_tasks = [
                get_hotprospector_credentials(current_user, mongo_client),
                get_subaccount_tokens(current_user, mongo_client),
                get_client_group_mapping(current_user, mongo_client)
            ]

            base_results = await asyncio.gather(*base_tasks, return_exceptions=True)

            credentials = base_results[0] if not isinstance(base_results[0], Exception) else None
            subaccount_tokens = base_results[1] if not isinstance(base_results[1], Exception) else {}
            client_group_mapping = base_results[2] if not isinstance(base_results[2], Exception) else {}

            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected. Please connect via /api/hotprospector/connect"
                )

            if not subaccount_tokens:
                return {
                    "data": [],
                    "meta": {
                        "total": 0,
                        "skip": skip,
                        "limit": limit,
                        "returned": 0,
                        "locations_processed": 0,
                        "cache_status": "no_locations"
                    },
                    "message": "No GHL locations found."
                }

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            # ============================================
            # STEP 2: CHECK MONGODB CACHE FIRST (1 hour freshness)
            # Now includes call logs in cache check
            # ============================================
            all_cached_leads = []
            stale_locations = []
            fresh_locations = []

            CACHE_TTL = 3600  # 1 hour in seconds

            for ghl_location_id, location_data in subaccount_tokens.items():
                # Check client_groups cache timestamp
                client_group = await client_groups_collection.find_one({
                    "user_id": current_user,
                    "ghl_location_id": ghl_location_id
                })

                cache_is_fresh = False

                if client_group:
                    last_refresh = client_group.get("last_hp_refresh")

                    if last_refresh:
                        cache_age = (datetime.utcnow() - last_refresh).total_seconds()
                        cache_is_fresh = cache_age < CACHE_TTL

                        logger.info(
                            f"Cache for {ghl_location_id}: age={cache_age:.0f}s, "
                            f"fresh={cache_is_fresh}"
                        )

                if cache_is_fresh:
                    # Use cached data (now includes call logs!)
                    cached_leads, _ = await get_hotprospector_leads_from_collection(
                        current_user, ghl_location_id, mongo_client, skip=0, limit=None
                    )

                    if cached_leads:
                        # Update client_name and location_name
                        ghl_location_name = location_data.get("name", "Unknown Location")
                        client_group_name = client_group_mapping.get(ghl_location_id)

                        for lead in cached_leads:
                            lead["client_name"] = client_group_name or "No Client Group"
                            lead["ghl_location_name"] = ghl_location_name
                            # Call logs are already included in cached_leads!

                        all_cached_leads.extend(cached_leads)
                        fresh_locations.append(ghl_location_id)

                        total_calls = sum(lead.get("call_logs_count", 0) for lead in cached_leads)
                        logger.info(
                            f"✅ Using {len(cached_leads)} cached leads with {total_calls} calls for "
                            f"{ghl_location_name} (age: {cache_age:.0f}s)"
                        )
                    else:
                        stale_locations.append(ghl_location_id)
                else:
                    stale_locations.append(ghl_location_id)

            # ============================================
            # STEP 3: Fetch fresh data WITH CALL LOGS for stale locations
            # ============================================
            if stale_locations:
                logger.info(
                    f"🔄 Fetching fresh data with call logs for {len(stale_locations)} stale locations"
                )

                async def fetch_fresh_location_with_calls(ghl_location_id: str):
                    """Fetch and cache fresh data INCLUDING call logs for one location"""
                    try:
                        location_data = subaccount_tokens.get(ghl_location_id, {})
                        ghl_location_name = location_data.get("name", "Unknown Location")
                        client_group_name = client_group_mapping.get(ghl_location_id)

                        # Fetch fresh leads from API
                        success, hp_leads = await integration.fetch_all_leads_from_ghl_location(
                            ghl_location_id
                        )

                        if not success:
                            logger.warning(f"Failed to fetch leads for {ghl_location_id}")
                            return []

                        # Normalize leads
                        normalized_leads = [
                            integration.normalize_lead(
                                lead,
                                ghl_location_id,
                                ghl_location_name,
                                client_group_name
                            )
                            for lead in hp_leads
                        ]

                        # 🔥 FETCH CALL LOGS FOR ALL LEADS
                        if include_call_logs and normalized_leads:
                            lead_ids = [
                                str(lead.get("id"))
                                for lead in normalized_leads
                                if lead.get("id")
                            ]

                            logger.info(f"📞 Fetching call logs for {len(lead_ids)} leads")

                            call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                            # Add call logs to normalized leads
                            for lead in normalized_leads:
                                lead_id = str(lead.get("id"))
                                lead["call_logs"] = call_logs_map.get(lead_id, [])
                                lead["call_logs_count"] = len(lead["call_logs"])

                        # Save to MongoDB cache WITH CALL LOGS
                        await save_hotprospector_leads_to_collection(
                            current_user,
                            ghl_location_id,
                            normalized_leads,  # Now includes call_logs!
                            mongo_client
                        )

                        # Update cache timestamp in client_groups
                        total_calls = sum(lead.get("call_logs_count", 0) for lead in normalized_leads)
                        await client_groups_collection.update_one(
                            {
                                "user_id": current_user,
                                "ghl_location_id": ghl_location_id
                            },
                            {
                                "$set": {
                                    "hotprospector_cache.metrics.total_leads": len(normalized_leads),
                                    "hotprospector_cache.metrics.total_calls": total_calls,
                                    "last_hp_refresh": datetime.utcnow()
                                }
                            }
                        )

                        logger.info(
                            f"✅ Fetched and cached {len(normalized_leads)} leads with "
                            f"{total_calls} calls for {ghl_location_name}"
                        )

                        return normalized_leads

                    except Exception as e:
                        logger.error(f"Error fetching {ghl_location_id}: {str(e)}")
                        return []

                # Fetch all stale locations in parallel
                fetch_tasks = [
                    fetch_fresh_location_with_calls(loc_id)
                    for loc_id in stale_locations
                ]

                fresh_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                for result in fresh_results:
                    if not isinstance(result, Exception):
                        all_cached_leads.extend(result)

            # ============================================
            # STEP 4: Deduplicate and process leads
            # Call logs already included, no need to fetch again!
            # ============================================
            unique_leads = {}
            for lead in all_cached_leads:
                email = lead.get("email", "").lower().strip()
                lead_id = lead.get("id")
                key = email if email else f"no_email_{lead_id}"

                if key not in unique_leads:
                    unique_leads[key] = lead
                else:
                    # Merge data
                    existing = unique_leads[key]
                    for field in ["phone", "company"]:
                        if not existing.get(field) and lead.get(field):
                            existing[field] = lead.get(field)

                    # Merge call logs if duplicate has more calls
                    if lead.get("call_logs_count", 0) > existing.get("call_logs_count", 0):
                        existing["call_logs"] = lead.get("call_logs", [])
                        existing["call_logs_count"] = lead.get("call_logs_count", 0)

            deduplicated_leads = list(unique_leads.values())

            # ============================================
            # STEP 5: Calculate total calls from cached data
            # ============================================
            total_calls = sum(lead.get("call_logs_count", 0) for lead in deduplicated_leads)

            # ============================================
            # STEP 6: Sort and paginate
            # ============================================
            deduplicated_leads.sort(
                key=lambda x: (x.get("client_name", ""), x.get("email", ""))
            )

            total_leads = len(deduplicated_leads)
            paginated_leads = deduplicated_leads[skip:skip + limit]

            elapsed = time.time() - start_time

            # Build location stats
            location_stats = {}
            for location_id in subaccount_tokens.keys():
                location_data = subaccount_tokens[location_id]
                location_stats[location_id] = {
                    "name": location_data.get("name", "Unknown"),
                    "cached": location_id in fresh_locations
                }

            cache_status = "full_cache" if not stale_locations else (
                "partial_cache" if fresh_locations else "no_cache"
            )

            logger.info(
                f"⚡ COMPLETED in {elapsed:.2f}s: "
                f"{len(paginated_leads)} leads with {total_calls} calls returned, "
                f"cache: {len(fresh_locations)} fresh, {len(stale_locations)} stale"
            )

            return {
                "data": paginated_leads,
                "meta": {
                    "total": total_leads,
                    "skip": skip,
                    "limit": limit,
                    "returned": len(paginated_leads),
                    "locations_processed": len(subaccount_tokens),
                    "location_stats": location_stats,
                    "total_calls": total_calls,
                    "response_time_ms": int(elapsed * 1000),
                    "cache_status": cache_status,
                    "fresh_from_cache": len(fresh_locations),
                    "fetched_fresh": len(stale_locations)
                },
                "message": (
                    f"Retrieved {len(paginated_leads)} leads with {total_calls} calls "
                    f"({len(fresh_locations)} from cache, {len(stale_locations)} fresh)"
                )
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching HotProspector leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


# ============================================
# OPTIMIZED REFRESH ENDPOINT WITH CALL LOGS
# ============================================

@app.get("/api/hotprospector/leads/refresh")
async def refresh_hotprospector_leads(current_user: str = Depends(get_current_user)):
    """
    ULTRA-OPTIMIZED: Force refresh all HotProspector leads WITH CALL LOGS.
    Single API call per location + batch call log fetching.

    Expected: 10-20x faster than old version
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            # Get credentials and locations
            base_tasks = [
                get_hotprospector_credentials(current_user, mongo_client),
                get_subaccount_tokens(current_user, mongo_client),
                get_client_group_mapping(current_user, mongo_client)
            ]

            base_results = await asyncio.gather(*base_tasks, return_exceptions=True)

            credentials = base_results[0] if not isinstance(base_results[0], Exception) else None
            subaccount_tokens = base_results[1] if not isinstance(base_results[1], Exception) else {}
            client_group_mapping = base_results[2] if not isinstance(base_results[2], Exception) else {}

            if not credentials:
                raise HTTPException(status_code=400, detail="Hot Prospector not connected")

            if not subaccount_tokens:
                raise HTTPException(status_code=400, detail="No GHL locations found")

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            # Clear cache
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            leads_collection = db["hotprospector_leads"]
            await leads_collection.delete_many({"user_id": current_user})
            logger.info(f"Cleared all cached leads for user {current_user}")

            # Refresh function with call logs
            async def refresh_location_with_calls(ghl_location_id: str, location_data: dict):
                """Refresh leads for a single location - INCLUDES CALL LOGS"""
                try:
                    ghl_location_name = location_data.get("name", "Unknown Location")
                    client_group_name = client_group_mapping.get(ghl_location_id)

                    # SINGLE API CALL for leads
                    success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)

                    if not success:
                        return {
                            "ghl_location_id": ghl_location_id,
                            "ghl_location_name": ghl_location_name,
                            "client_group_name": client_group_name or "No Client Group",
                            "leads_count": 0,
                            "calls_count": 0,
                            "success": False,
                            "error": hp_leads.get('error')
                        }

                    # Normalize leads
                    normalized_leads = [
                        integration.normalize_lead(
                            lead,
                            ghl_location_id,
                            ghl_location_name,
                            client_group_name
                        )
                        for lead in hp_leads
                    ]

                    # 🔥 FETCH CALL LOGS
                    total_calls = 0
                    if normalized_leads:
                        lead_ids = [str(lead.get("id")) for lead in normalized_leads if lead.get("id")]

                        logger.info(f"📞 Fetching call logs for {len(lead_ids)} leads in {ghl_location_name}")
                        call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                        for lead in normalized_leads:
                            lead_id = str(lead.get("id"))
                            lead["call_logs"] = call_logs_map.get(lead_id, [])
                            lead["call_logs_count"] = len(lead["call_logs"])
                            total_calls += lead["call_logs_count"]

                    # Save to cache WITH CALL LOGS
                    await save_hotprospector_leads_to_collection(
                        current_user,
                        ghl_location_id,
                        normalized_leads,
                        mongo_client
                    )

                    return {
                        "ghl_location_id": ghl_location_id,
                        "ghl_location_name": ghl_location_name,
                        "client_group_name": client_group_name or "No Client Group",
                        "leads_count": len(normalized_leads),
                        "calls_count": total_calls,
                        "success": True
                    }

                except Exception as e:
                    logger.error(f"Error refreshing {ghl_location_id}: {str(e)}")
                    return {
                        "ghl_location_id": ghl_location_id,
                        "ghl_location_name": location_data.get("name", "Unknown"),
                        "client_group_name": client_group_mapping.get(ghl_location_id, "No Client Group"),
                        "leads_count": 0,
                        "calls_count": 0,
                        "success": False,
                        "error": str(e)
                    }

            # Launch all refreshes in parallel
            refresh_tasks = [
                refresh_location_with_calls(location_id, location_data)
                for location_id, location_data in subaccount_tokens.items()
            ]

            logger.info(f"🚀 Refreshing {len(refresh_tasks)} locations with call logs in parallel")

            refresh_results = await asyncio.gather(*refresh_tasks, return_exceptions=True)

            # Process results
            results = []
            for result in refresh_results:
                if not isinstance(result, Exception):
                    results.append(result)

            total_leads = sum(r["leads_count"] for r in results if r["success"])
            total_calls = sum(r["calls_count"] for r in results if r["success"])
            successful = sum(1 for r in results if r["success"])
            failed = sum(1 for r in results if not r["success"])

            elapsed = time.time() - start_time

            logger.info(
                f"⚡ REFRESH COMPLETED in {elapsed:.2f}s: "
                f"{total_leads} leads with {total_calls} calls, "
                f"{successful} locations succeeded, {failed} failed"
            )

            return {
                "success": True,
                "message": f"Refreshed {total_leads} leads with {total_calls} calls from {len(results)} locations in {elapsed:.2f}s",
                "results": results,
                "meta": {
                    "total_locations": len(results),
                    "successful": successful,
                    "failed": failed,
                    "total_leads": total_leads,
                    "total_calls": total_calls,
                    "response_time_ms": int(elapsed * 1000)
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error refreshing leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to refresh leads: {str(e)}")

@app.get("/api/hotprospector/status")
async def hotprospector_status(current_user: str = Depends(get_current_user)):
    """Check HotProspector connection status and show linked GHL locations"""
    async with get_mongo_client() as mongo_client:
        try:
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)

            ghl_locations = []
            for location_id, location_data in subaccount_tokens.items():
                ghl_locations.append({
                    "location_id": location_id,
                    "name": location_data.get("name", "Unknown"),
                    "address": location_data.get("address")
                })

            return {
                "connected": bool(credentials and credentials.get("connected")),
                "api_uid": credentials.get("api_uid") if credentials else None,
                "ghl_locations": ghl_locations,
                "total_ghl_locations": len(ghl_locations)
            }
        except Exception as e:
            logger.error(f"Error checking Hot Prospector status: {str(e)}")
            return {
                "connected": False,
                "error": str(e),
                "ghl_locations": [],
                "total_ghl_locations": 0
            }

@app.get("/api/hotprospector/members")
async def get_hotprospector_members(current_user: str = Depends(get_current_user)):
    """Get all HotProspector team members"""
    async with get_mongo_client() as mongo_client:
        try:
            # Check cache first
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]

            user_doc = await users_collection.find_one(
                {"user_id": current_user},
                projection={"integrations.hotprospector.members": 1}
            )

            # Check if cached data exists and is fresh (1 hour)
            if user_doc:
                members_data = (user_doc.get("integrations", {})
                                .get("hotprospector", {})
                                .get("members"))

                if members_data:
                    updated_at = members_data.get("updated_at")
                    if updated_at:
                        cache_age = (datetime.now() - updated_at).total_seconds()
                        if cache_age < 3600:  # 1 hour cache
                            members = members_data.get("data", [])
                            return {
                                "data": members,
                                "meta": {"total": len(members)},
                                "message": f"Retrieved {len(members)} members from cache"
                            }

            # Fetch fresh data
            credentials = await get_hotprospector_credentials(current_user, mongo_client)
            if not credentials:
                raise HTTPException(
                    status_code=400,
                    detail="Hot Prospector not connected"
                )

            integration = HotProspectorIntegration(
                credentials.get("api_uid"),
                credentials.get("api_key")
            )

            success, members = await integration.get_member_users()
            if not success:
                raise HTTPException(
                    status_code=members.get("status_code", 500),
                    detail=members.get("error", "Failed to fetch members")
                )

            # Save to cache
            await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$set": {
                        "integrations.hotprospector.members": {
                            "data": members,
                            "updated_at": datetime.now()
                        },
                        "updated_at": datetime.now()
                    }
                },
                upsert=True
            )

            return {
                "data": members,
                "meta": {"total": len(members)},
                "message": f"Successfully fetched {len(members)} members"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching Hot Prospector members: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to fetch members: {str(e)}")


"""
OPTIMIZED /api/client-groups WITH PARALLEL META FETCHING
This version fetches Meta data for all groups simultaneously for maximum speed
"""

import logging

logger = logging.getLogger(__name__)


async def fetch_meta_data_for_group(
        meta_ad_account_id: str,
        current_user: str,
        mongo_client,
        facebook_ad_accounts: list
):
    """
    Fetch Meta data for a single group in parallel.
    NOW SAVES: campaigns, adsets, ads, leads, AND summary metrics
    """
    try:
        account = next(
            (acc for acc in facebook_ad_accounts if acc["id"] == meta_ad_account_id),
            {}
        )

        # Try cache first (5 minutes)
        account_data = await get_facebook_data(
            current_user,
            meta_ad_account_id,
            "account_data",
            mongo_client
        )

        # If no cache or cache expired, fetch fresh data
        if not account_data:
            token = await get_facebook_token(current_user, mongo_client)
            if token and token.get("access_token"):
                async with httpx.AsyncClient(timeout=300.0) as client:
                    response = await client.get(
                        f"https://graph.facebook.com/v23.0/{meta_ad_account_id}/campaigns",
                        params={
                            "fields": "name,status,insights.date_preset(maximum){actions,attribution_setting,spend,results,reach,frequency,cost_per_result,impressions,cpm,clicks,cpc,ctr},adsets{name,status,insights.date_preset(maximum){actions,attribution_setting,spend,results,reach,frequency,impressions,cpm,clicks,cpc,ctr}},ads{name,status,creative{title,body,image_url},insights.date_preset(maximum){actions,attribution_setting,results,reach,frequency,spend,quality_ranking,engagement_rate_ranking,conversion_rate_ranking,impressions,cpm,inline_link_clicks,cpc,clicks}}",
                            "access_token": token["access_token"]
                        }
                    )
                    if response.status_code == 200:
                        account_data = response.json()
                        # Save to cache for next time
                        await save_facebook_data(
                            current_user,
                            meta_ad_account_id,
                            account_data,
                            "account_data",
                            mongo_client
                        )
                    else:
                        logger.warning(f"Meta API error for {meta_ad_account_id}: {response.status_code}")
                        return None

        # Get leads count
        leads = await get_facebook_data(
            current_user,
            meta_ad_account_id,
            "leads",
            mongo_client
        )

        leads_count = 0
        if not leads:
            token = await get_facebook_token(current_user, mongo_client)
            if token and token.get("access_token"):
                async with httpx.AsyncClient(timeout=300.0) as client:
                    response = await client.get(
                        f"https://graph.facebook.com/v23.0/{meta_ad_account_id}/campaigns",
                        params={
                            "fields": "ads{leads.date_preset(maximum){id}}",
                            "access_token": token["access_token"]
                        }
                    )
                    if response.status_code == 200:
                        data = response.json()
                        for campaign in data.get("data", []):
                            for ad in campaign.get("ads", {}).get("data", []):
                                leads_count += len(ad.get("leads", {}).get("data", []))
        else:
            leads_count = len(leads) if isinstance(leads, list) else 0

        # Process campaigns and aggregate metrics
        campaigns = account_data.get("data", []) if account_data else []

        # 🔥 NEW: Store detailed campaign, adset, and ad data
        campaigns_list = []
        adsets_list = []
        ads_list = []

        # Initialize cumulative metrics
        total_spend = 0.0
        total_impressions = 0
        total_clicks = 0
        total_reach = 0
        total_results = 0

        for campaign in campaigns:
            # ============================================
            # PROCESS CAMPAIGN
            # ============================================
            campaign_insights = campaign.get("insights", {}).get("data", [])
            campaign_spend = 0
            campaign_impressions = 0
            campaign_clicks = 0
            campaign_reach = 0
            campaign_results = 0

            if campaign_insights:
                insight = campaign_insights[0]

                try:
                    campaign_spend = float(insight.get("spend", 0) or 0)
                    campaign_impressions = int(insight.get("impressions", 0) or 0)
                    campaign_clicks = int(insight.get("clicks", 0) or 0)
                    campaign_reach = int(insight.get("reach", 0) or 0)
                    campaign_results = get_result_value(campaign_insights, "lead")

                    total_spend += campaign_spend
                    total_impressions += campaign_impressions
                    total_clicks += campaign_clicks
                    total_reach += campaign_reach
                    total_results += campaign_results
                except (ValueError, TypeError):
                    pass

            # 🔥 NEW: Add full campaign details
            campaigns_list.append({
                "id": campaign.get("id"),
                "name": campaign.get("name"),
                "status": campaign.get("status", "").title(),
                "spend": round(campaign_spend, 2),
                "impressions": campaign_impressions,
                "clicks": campaign_clicks,
                "reach": campaign_reach,
                "results": campaign_results,
                "cpm": round((campaign_spend / campaign_impressions * 1000), 2) if campaign_impressions > 0 else 0,
                "cpc": round((campaign_spend / campaign_clicks), 2) if campaign_clicks > 0 else 0,
                "ctr": round((campaign_clicks / campaign_impressions * 100), 2) if campaign_impressions > 0 else 0,
                "leads": campaign_results
            })

            # ============================================
            # PROCESS ADSETS
            # ============================================
            for adset in campaign.get("adsets", {}).get("data", []):
                adset_insights = adset.get("insights", {}).get("data", [])

                adset_spend = 0
                adset_impressions = 0
                adset_clicks = 0
                adset_reach = 0

                if adset_insights:
                    insight = adset_insights[0]
                    try:
                        adset_spend = float(insight.get("spend", 0) or 0)
                        adset_impressions = int(insight.get("impressions", 0) or 0)
                        adset_clicks = int(insight.get("clicks", 0) or 0)
                        adset_reach = int(insight.get("reach", 0) or 0)
                    except (ValueError, TypeError):
                        pass

                # 🔥 NEW: Add full adset details
                adsets_list.append({
                    "id": adset.get("id"),
                    "name": adset.get("name"),
                    "campaign_id": campaign.get("id"),
                    "status": adset.get("status", "").title(),
                    "spend": round(adset_spend, 2),
                    "impressions": adset_impressions,
                    "clicks": adset_clicks,
                    "reach": adset_reach,
                    "cpm": round((adset_spend / adset_impressions * 1000), 2) if adset_impressions > 0 else 0,
                    "cpc": round((adset_spend / adset_clicks), 2) if adset_clicks > 0 else 0,
                    "ctr": round((adset_clicks / adset_impressions * 100), 2) if adset_impressions > 0 else 0
                })

            # ============================================
            # PROCESS ADS
            # ============================================
            for ad in campaign.get("ads", {}).get("data", []):
                ad_insights = ad.get("insights", {}).get("data", [])

                ad_spend = 0
                ad_impressions = 0
                ad_clicks = 0
                ad_reach = 0
                ad_results = 0

                if ad_insights:
                    insight = ad_insights[0]
                    try:
                        ad_spend = float(insight.get("spend", 0) or 0)
                        ad_impressions = int(insight.get("impressions", 0) or 0)
                        ad_clicks = int(insight.get("clicks", 0) or 0)
                        ad_reach = int(insight.get("reach", 0) or 0)
                        ad_results = int(insight.get("results", 0) or 0)
                    except (ValueError, TypeError):
                        pass

                # Extract creative info
                creative = ad.get("creative", {})

                # 🔥 NEW: Add full ad details with creative info
                ads_list.append({
                    "id": ad.get("id"),
                    "name": ad.get("name"),
                    "campaign_id": campaign.get("id"),
                    "status": ad.get("status", "").title(),
                    "spend": round(ad_spend, 2),
                    "impressions": ad_impressions,
                    "clicks": ad_clicks,
                    "reach": ad_reach,
                    "results": ad_results,
                    "cpm": round((ad_spend / ad_impressions * 1000), 2) if ad_impressions > 0 else 0,
                    "cpc": round((ad_spend / ad_clicks), 2) if ad_clicks > 0 else 0,
                    "ctr": round((ad_clicks / ad_impressions * 100), 2) if ad_impressions > 0 else 0,
                    # Creative details
                    "creative_title": creative.get("title", ""),
                    "creative_body": creative.get("body", ""),
                    "creative_image": creative.get("image_url", "")
                })

        # Calculate derived metrics
        avg_cpm = (total_spend / total_impressions * 1000) if total_impressions > 0 else 0
        avg_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
        avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
        cost_per_lead = (total_spend / leads_count) if leads_count > 0 else 0

        logger.info(
            f"✅ Meta data for {meta_ad_account_id}: "
            f"{len(campaigns_list)} campaigns, {len(adsets_list)} adsets, {len(ads_list)} ads, "
            f"${total_spend:.2f} spend, {leads_count} leads"
        )

        # 🔥 ENHANCED RETURN: Now includes campaigns, adsets, ads arrays
        return {
            "ad_account_id": meta_ad_account_id,
            "name": account.get("name", "Unknown Ad Account"),
            "currency": account.get("currency"),
            "created_time": account.get("created_time"),

            # 🔥 NEW: Detailed campaign, adset, and ad data
            "campaigns": campaigns_list,
            "adsets": adsets_list,
            "ads": ads_list,
            "leads": leads if leads else [],

            # Summary metrics (for backward compatibility)
            "metrics": {
                "total_campaigns": len(campaigns_list),
                "total_adsets": len(adsets_list),
                "total_ads": len(ads_list),
                "total_leads": leads_count,
                "insights": {
                    "spend": round(total_spend, 2),
                    "impressions": total_impressions,
                    "clicks": total_clicks,
                    "reach": total_reach,
                    "results": total_results,
                    "cpm": round(avg_cpm, 2),
                    "cpc": round(avg_cpc, 2),
                    "ctr": round(avg_ctr, 2),
                    "cost_per_result": round(cost_per_lead, 2)
                }
            }
        }

    except Exception as e:
        logger.error(f"Error fetching Meta data for {meta_ad_account_id}: {str(e)}", exc_info=True)
        return None


async def fetch_ghl_contacts_for_group(
        ghl_location_id: str,
        current_user: str,
        mongo_client,
        subaccount_tokens: dict
):
    """
    Fetch GHL contact COUNT for a single group (optimized for cache).
    Returns: (location_id, contact_count, was_refreshed)
    """
    try:
        location_data = subaccount_tokens.get(ghl_location_id, {})

        # Get cached count from token data
        cached_count = location_data.get("contact_count", 0)
        contacts_updated_at = location_data.get("contacts_updated_at")

        # Check cache validity (5 minutes)
        cache_valid = contacts_updated_at and (
                datetime.now() - contacts_updated_at
        ).total_seconds() < 300

        # If cache valid, return cached count
        if cache_valid:
            return ghl_location_id, cached_count, False

        # Fetch fresh count if stale
        if location_data.get("access_token"):
            success, result = await ghl_integration.fetch_location_contacts_client_groups(
                ghl_location_id,
                location_data.get("access_token"),
                limit=100
            )

            if success and isinstance(result, int):
                contact_count = result

                # Update user doc with new count
                db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
                await db["users"].update_one(
                    {"user_id": current_user},
                    {
                        "$set": {
                            f"integrations.gohighlevel.subaccounts.{ghl_location_id}.contact_count": contact_count,
                            f"integrations.gohighlevel.subaccounts.{ghl_location_id}.contacts_updated_at": datetime.now()
                        }
                    }
                )

                logger.info(f"✅ Updated contact count to {contact_count} for {ghl_location_id}")
                return ghl_location_id, contact_count, True
            else:
                logger.warning(f"Failed to fetch count for {ghl_location_id}, using cached")
                return ghl_location_id, cached_count, False

        return ghl_location_id, cached_count, False

    except Exception as e:
        logger.error(f"Error fetching GHL contact count for {ghl_location_id}: {str(e)}")
        return ghl_location_id, 0, False


# ============================================
# BACKGROUND JOB: Refresh GHL data for all users (PARALLEL)
# ============================================

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
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
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

async def refresh_ghl_data_for_user(user_id: str):
    """
    🚀 PARALLEL OPTIMIZED: Refresh GHL data for a specific user

    Uses parallel fetching to refresh all user's locations simultaneously.
    Flat collection with hierarchical indexes for efficient querying.
    """
    async with get_mongo_client() as mongo_client:
        try:
            logger.info(f"🔄 Starting parallel refresh for user {user_id}")

            # 🔥 USE PARALLEL FETCHING FOR ALL USER'S LOCATIONS
            result = await fetch_and_cache_multiple_ghl_locations(
                user_id,
                mongo_client,
                is_initial_load=False  # Incremental mode
            )

            logger.info(
                f"✅ Parallel refresh complete for user {user_id}: "
                f"{result['successful']} succeeded, {result['failed']} failed"
            )

            return result

        except Exception as e:
            logger.error(f"❌ Error refreshing user {user_id}: {e}", exc_info=True)
            raise


# ============================================
# NEW: Manual Full Refresh Endpoint
# ============================================
@app.post("/api/contacts/ghl/full-refresh")
async def force_full_ghl_refresh(
        group_id: str = Query(...),
        current_user: str = Depends(get_current_user)
):
    """
    Force a FULL refresh of GHL contacts for a specific client group.

    This will:
    - Delete all existing cached contacts
    - Fetch ALL contacts from GHL (not incremental)
    - Rebuild the entire contact database

    Use this when:
    - You suspect data inconsistencies
    - Initial cache was incomplete
    - Major changes were made in GHL

    ⚠️ Warning: This can take several minutes for large contact lists
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            # Verify group belongs to user
            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            if not group.get("ghl_location_id"):
                raise HTTPException(
                    status_code=400,
                    detail="Client group does not have a GHL location"
                )

            logger.info(f"🔄 Starting FULL refresh for group {group_id}")

            # ✅ Force FULL refresh (initial load mode)
            await fetch_and_cache_ghl_data_optimized(
                group_id,
                group["ghl_location_id"],
                current_user,
                mongo_client,
                is_initial_load=True  # 🔥 This forces full reload
            )

            # Get updated count
            contacts_collection = db["ghl_contacts"]
            total_contacts = await contacts_collection.count_documents({
                "user_id": current_user,
                "location_id": group["ghl_location_id"]
            })

            logger.info(f"✅ FULL refresh complete for group {group_id}: {total_contacts} contacts")

            return {
                "success": True,
                "message": f"Full refresh completed for '{group['name']}'",
                "group_id": group_id,
                "total_contacts": total_contacts,
                "refresh_mode": "full",
                "timestamp": datetime.utcnow().isoformat()
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in full refresh: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to refresh contacts: {str(e)}"
            )


# ============================================
# NEW: Incremental Refresh Status Endpoint
# ============================================
@app.get("/api/contacts/ghl/refresh-status")
async def get_ghl_refresh_status(current_user: str = Depends(get_current_user)):
    """
    Get refresh status for all GHL client groups.

    Shows:
    - Last refresh time
    - Time since last refresh
    - Total contacts cached
    - Refresh mode used (full/incremental)
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]
            contacts_collection = db["ghl_contacts"]

            groups = await client_groups_collection.find({
                "user_id": current_user,
                "ghl_location_id": {"$exists": True, "$ne": None}
            }).to_list(None)

            status_list = []
            now = datetime.utcnow()

            for group in groups:
                last_refresh = group.get("last_ghl_refresh")

                # Get actual contact count from database
                contact_count = await contacts_collection.count_documents({
                    "user_id": current_user,
                    "location_id": group["ghl_location_id"]
                })

                time_since_refresh = None
                if last_refresh:
                    time_since_refresh = (now - last_refresh).total_seconds() / 3600  # hours

                status_list.append({
                    "group_id": group["id"],
                    "group_name": group["name"],
                    "location_id": group["ghl_location_id"],
                    "total_contacts": contact_count,
                    "last_refresh": last_refresh.isoformat() if last_refresh else None,
                    "hours_since_refresh": round(time_since_refresh, 1) if time_since_refresh else None,
                    "next_incremental_refresh": "Automatic (hourly)" if last_refresh else "Not yet refreshed"
                })

            return {
                "groups": status_list,
                "total_groups": len(status_list),
                "message": "Refresh status retrieved successfully"
            }

        except Exception as e:
            logger.error(f"Error getting refresh status: {e}")
            raise HTTPException(status_code=500, detail=str(e))


async def refresh_ghl_data_for_user(user_id: str):
    """Refresh GHL data for a specific user"""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        client_groups_collection = db["client_groups"]

        groups = await client_groups_collection.find(
            {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}}
        ).to_list(None)

        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)

        tasks = []
        for group in groups:
            if group.get("ghl_location_id"):
                tasks.append(
                    fetch_ghl_contacts_for_group(
                        group["ghl_location_id"],
                        user_id,
                        mongo_client,
                        subaccount_tokens
                    )
                )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, group in enumerate(groups):
            if not isinstance(results[i], Exception):
                location_id, contact_count, was_refreshed = results[i]

                # Safety check: ensure contact_count is an integer
                if not isinstance(contact_count, int):
                    logger.warning(f"Contact count for {location_id} ")


                location_data = subaccount_tokens.get(location_id, {})

                cache_data = {
                    "location_id": location_id,
                    "name": location_data.get("name", "Unknown Location"),
                    "address": location_data.get("address"),
                    "metrics": {
                        "total_contacts": contact_count
                    }
                }

                update_fields = {
                    "gohighlevel_cache": cache_data
                }

                # Only update refresh time if we actually fetched new data
                if was_refreshed:
                    update_fields["last_ghl_refresh"] = datetime.utcnow()

                await client_groups_collection.update_one(
                    {"id": group["id"]},
                    {"$set": update_fields}
                )

async def fetch_hp_leads_for_group(
        ghl_location_id: str,
        current_user: str,
        mongo_client,
        hp_credentials: dict,
        location_name: str
):
    """Fetch HotProspector leads for a single group in parallel"""
    try:
        if not hp_credentials:
            return ghl_location_id, 0

        # Try cache first
        cached_leads, total_count = await get_hotprospector_leads_from_collection(
            current_user,
            ghl_location_id,
            mongo_client,
            skip=0,
            limit=None
        )

        # Fetch fresh if no cache
        if not cached_leads:
            integration = HotProspectorIntegration(
                hp_credentials.get("api_uid"),
                hp_credentials.get("api_key")
            )

            try:
                success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)
                if success:
                    normalized_leads = [
                        integration.normalize_lead(lead, ghl_location_id, location_name)
                        for lead in hp_leads
                    ]
                    await save_hotprospector_leads_to_collection(
                        current_user,
                        ghl_location_id,
                        normalized_leads,
                        mongo_client
                    )
                    total_count = len(normalized_leads)
                else:
                    total_count = 0
            except Exception as e:
                logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
                total_count = 0

        return ghl_location_id, total_count

    except Exception as e:
        logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
        return ghl_location_id, 0


# ============================================
# MAIN OPTIMIZED ENDPOINT WITH PARALLEL FETCHING
# ============================================

@app.get("/api/client-groups")
async def get_client_groups(current_user: str = Depends(get_current_user)):
    """
    CACHE-OPTIMIZED: Fetch client groups from cached database data

    Returns pre-computed metrics stored in MongoDB.
    Data is refreshed hourly by background jobs.
    Expected response time: <100ms
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            # Fetch client groups with all cached data
            client_groups = await client_groups_collection.find(
                {"user_id": current_user},
                {
                    "_id": 1,
                    "id": 1,
                    "name": 1,
                    "ghl_location_id": 1,
                    "meta_ad_account_id": 1,
                    "notes": 1,
                    "created_at": 1,
                    "updated_at": 1,
                    # Cached metrics
                    "gohighlevel_cache": 1,
                    "facebook_cache": 1,
                    "hotprospector_cache": 1,
                    "last_ghl_refresh": 1,
                    "last_meta_refresh": 1,
                    "last_hp_refresh": 1
                }
            ).to_list(None)

            if not client_groups:
                return {
                    "client_groups": [],
                    "meta": {
                        "total_groups": 0,
                        "total_ghl_locations": 0,
                        "total_meta_ad_accounts": 0,
                        "total_hotprospector_enabled": 0
                    },
                    "message": "No client groups found"
                }

            result = []
            for group in client_groups:
                group_data = mongo_to_dict(group)

                # Use cached data with fallback to empty
                group_data["gohighlevel"] = group.get("gohighlevel_cache", {})
                group_data["facebook"] = group.get("facebook_cache", {})
                group_data["hotprospector"] = group.get("hotprospector_cache", {})

                # Remove cache keys from response
                group_data.pop("gohighlevel_cache", None)
                group_data.pop("facebook_cache", None)
                group_data.pop("hotprospector_cache", None)

                result.append(group_data)

            elapsed = time.time() - start_time
            logger.info(f"⚡ Retrieved {len(result)} cached client groups in {elapsed:.3f}s")

            return {
                "client_groups": result,
                "meta": {
                    "total_groups": len(result),
                    "total_ghl_locations": sum(1 for g in result if g.get("gohighlevel", {}).get("location_id")),
                    "total_meta_ad_accounts": sum(1 for g in result if g.get("facebook", {}).get("ad_account_id")),
                    "total_hotprospector_enabled": sum(
                        1 for g in result if g.get("hotprospector", {}).get("metrics", {}).get("total_leads", 0) > 0
                    )
                },
                "message": f"Successfully fetched {len(result)} client groups from cache"
            }

        except Exception as e:
            logger.error(f"Error fetching client groups for user {current_user}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch client groups: {str(e)}")


@app.post("/api/client-groups")
async def create_client_group_optimized(
        request: ClientGroupRequest,
        current_user: str = Depends(get_current_user)
):
    """
    Create a new client group with OPTIMIZED GHL data fetching.

    Features:
    - Fetches ALL contacts in descending chronological order
    - Calculates tag metrics and stores in cache
    - Smart data organization (user > client_group > contacts)
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            if not request.name:
                raise HTTPException(status_code=400, detail="Client group name is required")

            # ============================================
            # STEP 1: Generate GHL location token if provided
            # ============================================
            if request.ghl_location_id:
                agency_token = await get_agency_token(current_user, mongo_client)
                if not agency_token:
                    raise HTTPException(
                        status_code=400,
                        detail="No agency token available. Please connect GoHighLevel in Settings."
                    )

                company_id = agency_token.get("company_id")
                access_token = agency_token.get("access_token")

                if not company_id or not access_token:
                    raise HTTPException(status_code=400, detail="Invalid agency token")

                # Generate location token
                success, loc_tokens = await ghl_integration.generate_location_token(
                    company_id, request.ghl_location_id, access_token
                )

                if not success:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to generate location token: {loc_tokens.get('error', 'Unknown error')}"
                    )

                # Fetch location details
                location_details = await fetch_location_details(
                    request.ghl_location_id,
                    loc_tokens.get("access_token")
                )

                # Fetch contact count only (lightweight)
                contact_count = await get_contact_count_from_ghl(
                    request.ghl_location_id,
                    loc_tokens.get("access_token")
                )

                # Save location token
                await save_subaccount_token(
                    current_user,
                    request.ghl_location_id,
                    loc_tokens,
                    mongo_client,
                    location_details,
                    contact_count=contact_count
                )

                logger.info(f"✅ Generated and saved token for GHL location {request.ghl_location_id}")

            # ============================================
            # STEP 2: Create client group
            # ============================================
            group_id = f"{current_user}_{int(datetime.now().timestamp())}"
            client_group = {
                "id": group_id,
                "user_id": current_user,
                "name": request.name,
                "ghl_location_id": request.ghl_location_id,
                "meta_ad_account_id": request.meta_ad_account_id,
                "notes": request.notes or "",
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "status": "creating",
                "status_message": "Fetching data...",
                "gohighlevel_cache": {},
                "facebook_cache": {},
                "hotprospector_cache": {},
                "last_ghl_refresh": None,
                "last_meta_refresh": None,
                "last_hp_refresh": None
            }

            await client_groups_collection.insert_one(client_group)
            logger.info(f"✅ Created client group {group_id}")

            # ============================================
            # STEP 3: Fetch GHL data with OPTIMIZED function
            # ============================================
            if request.ghl_location_id:

                await fetch_and_cache_ghl_data_optimized(
                    group_id,
                    request.ghl_location_id,
                    current_user,
                    mongo_client,
                    is_initial_load=True  # Full load on creation
                )

            # Fetch Meta data (unchanged)
            if request.meta_ad_account_id:
                await fetch_and_cache_meta_data(
                    group_id,
                    request.meta_ad_account_id,
                    current_user,
                    mongo_client
                )
                # Fetch Meta campaign/adset/ad data
                await fetch_and_cache_campaign_insights(
                    group_id,
                    request.meta_ad_account_id,
                    current_user,
                    mongo_client,
                    is_initial_load=True
                )
                await fetch_and_cache_adset_insights(
                        group_id,  # 1st: group ID
                        request.meta_ad_account_id,  # 2nd: ad account ID
                        current_user,  # 3rd: user ID
                        mongo_client,  # 4th: mongo client
                        is_initial_load=True  # 5th: initial load flag
                )
                await fetch_and_cache_ad_insights(
                    group_id,
                    request.meta_ad_account_id,
                    current_user,
                    mongo_client
                )
                # Fetch ALL Meta leads into database
                await fetch_and_cache_facebook_leads_FIXED(
                    group_id,
                    request.meta_ad_account_id,
                    current_user,
                    mongo_client,
                    is_initial_load=True
                )



            # Fetch HP data (unchanged)
            # if request.ghl_location_id:
            #     await fetch_and_cache_hp_data(
            #         group_id,
            #         request.ghl_location_id,
            #         current_user,
            #         mongo_client
            #     )

            # ============================================
            # STEP 4: Mark as complete
            # ============================================
            await client_groups_collection.update_one(
                {"id": group_id},
                {
                    "$set": {
                        "status": "complete",
                        "status_message": "Client group created successfully"
                    }
                }
            )

            # Get final group data
            final_group = await client_groups_collection.find_one({"id": group_id})

            return {
                "client_group": mongo_to_dict(final_group),
                "message": "Client group created successfully"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating client group: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to create client group: {str(e)}")


@app.get("/api/client-groups/{group_id}/tag-metrics")
async def get_client_group_tag_metrics(
        group_id: str,
        current_user: str = Depends(get_current_user)
):
    """
    NEW ENDPOINT: Get tag metrics for a client group.

    Returns:
    {
        "tag_metrics": {
            "missed consult hp": 42,
            "fb lead form submitted": 128,
            "zombie lead": 15,
            ...
        },
        "total_contacts": 185,
        "total_unique_tags": 12
    }
    """
    async with get_mongo_client() as mongo_client:
        try:

            # Verify group belongs to user
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            # Get tag metrics from cache
            tag_metrics = await get_tag_metrics_from_cache(
                mongo_client,
                group_id,
                current_user
            )

            total_contacts = (group.get("gohighlevel_cache", {})
                              .get("metrics", {})
                              .get("total_contacts", 0))

            return {
                "tag_metrics": tag_metrics,
                "total_contacts": total_contacts,
                "total_unique_tags": len(tag_metrics),
                "message": f"Retrieved metrics for {len(tag_metrics)} unique tags"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting tag metrics: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/contacts/ghl-sorted")
async def get_ghl_contacts_sorted_endpoint(
        group_id: str = Query(...),
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        current_user: str = Depends(get_current_user)
):
    """
    UPDATED: Get GHL contacts in DESCENDING chronological order (newest first).

    Now properly sorted by dateAdded DESC.
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()


            # Verify group belongs to user
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            # Calculate skip
            skip = (page - 1) * limit

            # Fetch sorted contacts
            contacts, total_count = await get_ghl_contacts_sorted(
                current_user,
                group_id,
                mongo_client,
                skip=skip,
                limit=limit
            )

            elapsed = time.time() - start_time

            total_pages = (total_count + limit - 1) // limit

            logger.info(
                f"⚡ Fetched page {page} in descending order: {len(contacts)} contacts "
                f"in {elapsed:.3f}s"
            )

            return {
                "contacts": contacts,
                "meta": {
                    "total_contacts": total_count,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(contacts),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "dateAdded_desc"
                },
                "message": f"Retrieved {len(contacts)} contacts in descending chronological order",
                "performance": {
                    "response_time_ms": int(elapsed * 1000)
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching sorted contacts: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contacts/ghl/refresh-incremental")
async def refresh_ghl_incremental(
        group_id: str = Query(...),
        current_user: str = Depends(get_current_user)
):
    """
    NEW ENDPOINT: Smart incremental refresh for GHL contacts.

    Only fetches NEW contacts since last refresh.
    Stops when hitting last known contact.
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()


            # Verify group belongs to user
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            if not group.get("ghl_location_id"):
                raise HTTPException(
                    status_code=400,
                    detail="Client group does not have a GHL location"
                )

            logger.info(f"🔄 Starting incremental refresh for group {group_id}")

            # Run incremental refresh
            await fetch_and_cache_ghl_data_optimized(
                group_id,
                group["ghl_location_id"],
                current_user,
                mongo_client,
                is_initial_load=False  # 🔥 Incremental mode
            )

            # Get updated metrics
            updated_group = await client_groups_collection.find_one({"id": group_id})

            total_contacts = (updated_group.get("gohighlevel_cache", {})
                              .get("metrics", {})
                              .get("total_contacts", 0))

            tag_metrics = (updated_group.get("gohighlevel_cache", {})
                           .get("metrics", {})
                           .get("tag_breakdown", {}))

            elapsed = time.time() - start_time

            logger.info(
                f"✅ Incremental refresh complete for group {group_id}: "
                f"{total_contacts} total contacts, {len(tag_metrics)} unique tags "
                f"in {elapsed:.2f}s"
            )

            return {
                "success": True,
                "message": f"Incremental refresh completed for '{group['name']}'",
                "group_id": group_id,
                "total_contacts": total_contacts,
                "total_unique_tags": len(tag_metrics),
                "refresh_mode": "incremental",
                "timestamp": datetime.utcnow().isoformat(),
                "performance": {
                    "response_time_seconds": round(elapsed, 2)
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error in incremental refresh: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to refresh contacts: {str(e)}"
            )


# ============================================
# NEW ENDPOINT: Check client group status
# ============================================
@app.get("/api/client-groups/{group_id}/status")
async def get_client_group_status(
        group_id: str,
        current_user: str = Depends(get_current_user)
):
    """
    Get the current status of a client group creation.
    Use this endpoint to poll for completion status.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            group = await client_groups_collection.find_one(
                {"id": group_id, "user_id": current_user},
                {
                    "status": 1,
                    "status_message": 1,
                    "creation_progress": 1,
                    "updated_at": 1
                }
            )

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")

            return {
                "status": group.get("status", "unknown"),
                "status_message": group.get("status_message", ""),
                "progress": group.get("creation_progress", {}),
                "updated_at": group.get("updated_at").isoformat() if group.get("updated_at") else None,
                "is_complete": group.get("status") == "complete"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error checking status: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


# ============================================
# NEW: Parallel fetching for multiple locations
# ============================================

async def fetch_and_cache_multiple_ghl_locations(
        user_id: str,
        mongo_client,
        is_initial_load: bool = False
):
    """
    🚀 PARALLEL: Fetch GHL contacts for all locations of a user in parallel.

    Each location's contacts are stored as separate documents with compound indexes.

    Returns:
        dict: {"total_locations": int, "successful": int, "failed": int}
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        client_groups_collection = db["client_groups"]

        # Get all client groups for this user with GHL locations
        groups = await client_groups_collection.find({
            "user_id": user_id,
            "ghl_location_id": {"$exists": True, "$ne": None}
        }).to_list(None)

        if not groups:
            logger.info(f"No GHL locations found for user {user_id}")
            return {
                "total_locations": 0,
                "successful": 0,
                "failed": 0
            }

        logger.info(f"🚀 Starting parallel fetch for {len(groups)} locations")

        # Create parallel fetch tasks
        fetch_tasks = [
            fetch_and_cache_ghl_data_optimized(
                group["id"],
                group["ghl_location_id"],
                user_id,
                mongo_client,
                is_initial_load=is_initial_load
            )
            for group in groups
        ]

        # Execute all fetches in parallel
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        # Log results
        success_count = sum(1 for r in results if not isinstance(r, Exception))
        failure_count = len(results) - success_count

        logger.info(
            f"✅ Parallel fetch complete: "
            f"{success_count} succeeded, {failure_count} failed"
        )

        return {
            "total_locations": len(groups),
            "successful": success_count,
            "failed": failure_count
        }

    except Exception as e:
        logger.error(f"Error in parallel fetch: {e}", exc_info=True)
        raise


async def fetch_and_cache_meta_data(
        group_id: str,
        meta_ad_account_id: str,
        user_id: str,
        mongo_client
):
    """Fetch Meta data and save FULL details to cache"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        client_groups_collection = db["client_groups"]

        # Get Facebook ad accounts
        facebook_ad_accounts_data = await get_facebook_data(
            user_id, "global", "adaccounts", mongo_client
        )
        facebook_ad_accounts = facebook_ad_accounts_data.get("data", []) if facebook_ad_accounts_data else []

        # Fetch Meta data (now includes campaigns, adsets, ads)
        meta_data = await fetch_meta_data_for_group(
            meta_ad_account_id,
            user_id,
            mongo_client,
            facebook_ad_accounts
        )

        if not meta_data:
            logger.warning(f"Failed to fetch Meta data for {meta_ad_account_id}")
            return

        # 🔥 SAVE FULL DATA: campaigns, adsets, ads, leads, AND metrics
        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "facebook_cache": meta_data,  # Now includes campaigns/adsets/ads arrays!
                    "last_meta_refresh": datetime.utcnow()
                }
            }
        )

        logger.info(
            f"✅ Cached Meta data for group {group_id}: "
            f"{len(meta_data.get('campaigns', []))} campaigns, "
            f"{len(meta_data.get('adsets', []))} adsets, "
            f"{len(meta_data.get('ads', []))} ads"
        )

    except Exception as e:
        logger.error(f"Error caching Meta data: {e}")
        raise



async def fetch_and_cache_hp_data(
        group_id: str,
        ghl_location_id: str,
        user_id: str,
        mongo_client
):
    """Fetch Hot Prospector data and save to cache"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        client_groups_collection = db["client_groups"]

        # Get HP credentials
        hp_credentials = await get_hotprospector_credentials(user_id, mongo_client)
        if not hp_credentials:
            logger.info(f"No HP credentials for user {user_id}")
            return

        # Get location name
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_name = subaccount_tokens.get(ghl_location_id, {}).get("name", "Unknown Location")

        # Fetch HP leads
        location_id, count = await fetch_hp_leads_for_group(
            ghl_location_id,
            user_id,
            mongo_client,
            hp_credentials,
            location_name
        )

        cache_data = {
            "ghl_location_id": location_id,
            "name": location_name,
            "metrics": {
                "total_leads": count
            }
        }

        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "hotprospector_cache": cache_data,
                    "last_hp_refresh": datetime.utcnow()
                }
            }
        )

        logger.info(f"✅ Cached HP data for group {group_id}: {count} leads")

    except Exception as e:
        logger.error(f"Error caching HP data: {e}")
        raise
# ============================================
# HELPER: Extract result value by action_type
# ============================================
def get_result_value(insights_data, action_type="lead"):
    """Safely extract numeric value from insights.results list by action_type."""
    if not insights_data or not isinstance(insights_data, list) or len(insights_data) == 0:
        return 0
    insight = insights_data[0]
    results = insight.get("results") or []
    for res in results:
        if res.get("action_type") == action_type:
            try:
                return int(res.get("value", "0"))
            except (ValueError, TypeError):
                return 0
    return 0

@app.get("/api/client-groups/{group_id}")
async def get_client_group_comprehensive(group_id: str, current_user: str = Depends(get_current_user)):
    """
    OPTIMIZED: Fetch comprehensive data for a single client group.

    Performance improvements:
    - Parallel fetching of all integrations (GHL, Meta, HP)
    - Single-pass lead processing
    - Efficient caching strategy (1.5 days = 129600 seconds)
    - Reduced database queries

    Expected improvement: 3-5x faster
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            # ============================================
            # STEP 1: Fetch client group and check cache
            # ============================================
            group = await client_groups_collection.find_one({
                "id": group_id,
                "user_id": current_user
            })

            if not group:
                raise HTTPException(status_code=404, detail="Client group not found")


            CACHE_DURATION_SECONDS = 929600
            cache_age = None
            has_fresh_cache = False

            if all([
                group.get("gohighlevel_cache"),
                group.get("facebook_cache"),
                group.get("last_ghl_refresh")
            ]):
                oldest_refresh = min([
                    group.get("last_ghl_refresh", datetime.min),
                    group.get("last_meta_refresh", datetime.min),
                    group.get("last_hp_refresh", datetime.min)
                ])
                cache_age = (datetime.utcnow() - oldest_refresh).total_seconds()
                has_fresh_cache = cache_age < CACHE_DURATION_SECONDS

            # Initialize response structure
            response_data = {
                "group_info": mongo_to_dict(group),
                "ghl_data": {},
                "meta_data": {},
                "hotprospector_data": {},
                "insights": {},
                "marketing": {},
                "leads": {},
                "call_center": {},
                "cache_info": {
                    "used_cache": has_fresh_cache,
                    "cache_age_seconds": cache_age,
                    "cache_duration_seconds": CACHE_DURATION_SECONDS
                }
            }

            # ============================================
            # STEP 2: Parallel data fetching
            # ============================================
            async def fetch_ghl_data():
                """Fetch GHL data with caching"""
                if not group.get("ghl_location_id"):
                    return None

                try:
                    ghl_location_id = group["ghl_location_id"]

                    # Use cached data if available (1.5 days)
                    if has_fresh_cache and group.get("gohighlevel_cache"):
                        cached = group["gohighlevel_cache"]
                        return {
                            "location_id": ghl_location_id,
                            "location_name": cached.get("name", ""),
                            "location_address": cached.get("address"),
                            "total_contacts": cached.get("metrics", {}).get("total_contacts", 0),
                            "contacts": [],  # Contacts fetched separately if needed
                            "status_breakdown": {"Open": 0, "Won": 0, "Abandoned": 0, "Lost": 0},
                            "total_value": 0,
                            "from_cache": True
                        }

                    # Fetch fresh data
                    subaccount_tokens = await get_subaccount_tokens(current_user, mongo_client)
                    location_data = subaccount_tokens.get(ghl_location_id, {})

                    # Get contacts from cache or fetch
                    contacts = location_data.get("contacts", [])
                    contacts_updated_at = location_data.get("contacts_updated_at")
                    cache_valid = contacts_updated_at and (datetime.now() - contacts_updated_at).total_seconds() < CACHE_DURATION_SECONDS

                    if not cache_valid and location_data.get("access_token"):
                        success, fresh_contacts = await ghl_integration.fetch_location_contacts(
                            ghl_location_id,
                            location_data.get("access_token"),
                            limit=100
                        )
                        if success:
                            contacts = fresh_contacts

                    # Process contacts efficiently
                    ghl_leads = []
                    contact_status_counts = {"Open": 0, "Won": 0, "Abandoned": 0, "Lost": 0}
                    total_contact_value = 0

                    for contact in contacts:
                        contact_status = contact.get("contactType", "Open")
                        if contact_status not in contact_status_counts:
                            contact_status = "Open"
                        contact_status_counts[contact_status] += 1

                        # Fast value extraction
                        contact_value = 0
                        custom_fields = contact.get("customField", {})
                        if isinstance(custom_fields, dict):
                            try:
                                contact_value = float(custom_fields.get("value", 0) or 0)
                            except (ValueError, TypeError):
                                contact_value = 0
                        total_contact_value += contact_value

                        ghl_leads.append({
                            "id": contact.get("id"),
                            "name": contact.get("fullName", ""),
                            "email": contact.get("email", ""),
                            "phone": contact.get("phone", ""),
                            "status": contact_status,
                            "source": "GHL",
                            "created": contact.get("dateAdded", ""),
                            "tags": contact.get("tags", []),
                            "value": contact_value,
                            "businessName": location_data.get("name", ""),
                            "contactType": contact.get("contactType", "contact"),
                            "website": contact.get("website", ""),
                            "address1": contact.get("address1", ""),
                            "country": contact.get("country", "")
                        })

                    return {
                        "location_id": ghl_location_id,
                        "location_name": location_data.get("name", ""),
                        "location_address": location_data.get("address"),
                        "total_contacts": len(contacts),
                        "contacts": ghl_leads,
                        "status_breakdown": contact_status_counts,
                        "total_value": total_contact_value,
                        "from_cache": False
                    }

                except Exception as e:
                    logger.error(f"Error fetching GHL data: {str(e)}")
                    return None

            async def fetch_meta_data():
                """Fetch Meta data with caching"""
                if not group.get("meta_ad_account_id"):
                    return None

                try:
                    meta_ad_account_id = group["meta_ad_account_id"]

                    # Use cached data if available (1.5 days)
                    if has_fresh_cache and group.get("facebook_cache"):
                        cached = group["facebook_cache"]
                        return {
                            "ad_account_id": meta_ad_account_id,
                            "campaigns": [],
                            "adsets": [],
                            "ads": [],
                            "leads": [],
                            "summary": cached.get("metrics", {}).get("insights", {}),
                            "from_cache": True
                        }

                    # Fetch fresh data
                    token = await get_facebook_token(current_user, mongo_client)
                    if not token or not token.get("access_token"):
                        return None

                    # Check cache first
                    account_data = await get_facebook_data(
                        current_user,
                        meta_ad_account_id,
                        "account_data",
                        mongo_client
                    )

                    # Fetch if not cached
                    if not account_data:
                        async with httpx.AsyncClient(timeout=300.0) as client:
                            response = await client.get(
                                f"https://graph.facebook.com/v23.0/{meta_ad_account_id}/campaigns",
                                params={
                                    "fields": "name,status,insights.date_preset(maximum){spend,results,reach,impressions,cpm,clicks,cpc,ctr},adsets{name,status,insights.date_preset(maximum){spend,results,impressions,cpm,clicks,cpc,ctr}},ads{name,status,insights.date_preset(maximum){spend,results,impressions,cpm,clicks,cpc,ctr}}",
                                    "access_token": token["access_token"]
                                }
                            )
                            if response.status_code == 200:
                                account_data = response.json()
                                await save_facebook_data(
                                    current_user,
                                    meta_ad_account_id,
                                    account_data,
                                    "account_data",
                                    mongo_client
                                )

                    # Process campaigns efficiently (single pass)
                    campaigns_list = []
                    adsets_list = []
                    ads_list = []

                    total_spend = 0
                    total_impressions = 0
                    total_clicks = 0
                    total_reach = 0
                    total_results = 0

                    if account_data:
                        for campaign in account_data.get("data", []):
                            # Process campaign
                            campaign_insights = campaign.get("insights", {}).get("data", [])
                            campaign_spend = 0
                            campaign_impressions = 0
                            campaign_clicks = 0
                            campaign_reach = 0
                            campaign_results = 0

                            if campaign_insights:
                                insight = campaign_insights[0]
                                try:
                                    campaign_spend = float(insight.get("spend", 0) or 0)
                                    campaign_impressions = int(insight.get("impressions", 0) or 0)
                                    campaign_clicks = int(insight.get("clicks", 0) or 0)
                                    campaign_reach = int(insight.get("reach", 0) or 0)
                                    campaign_results = get_result_value(campaign_insights, "lead")

                                    total_spend += campaign_spend
                                    total_impressions += campaign_impressions
                                    total_clicks += campaign_clicks
                                    total_reach += campaign_reach
                                    total_results += campaign_results
                                except (ValueError, TypeError):
                                    pass

                            campaigns_list.append({
                                "id": campaign.get("id"),
                                "name": campaign.get("name"),
                                "status": campaign.get("status", "").title(),
                                "spend": round(campaign_spend, 2),
                                "impressions": campaign_impressions,
                                "clicks": campaign_clicks,
                                "reach": campaign_reach,
                                "results": campaign_results,
                                "cpm": round((campaign_spend / campaign_impressions * 1000),
                                             2) if campaign_impressions > 0 else 0,
                                "cpc": round((campaign_spend / campaign_clicks), 2) if campaign_clicks > 0 else 0,
                                "ctr": round((campaign_clicks / campaign_impressions * 100),
                                             2) if campaign_impressions > 0 else 0,
                                "leads": campaign_results
                            })

                            # Process adsets and ads inline (avoid nested loops)
                            for adset in campaign.get("adsets", {}).get("data", []):
                                adset_insights = adset.get("insights", {}).get("data", [])
                                if adset_insights:
                                    insight = adset_insights[0]
                                    adsets_list.append({
                                        "id": adset.get("id"),
                                        "name": adset.get("name"),
                                        "status": adset.get("status", "").title(),
                                        "spend": round(float(insight.get("spend", 0) or 0), 2),
                                        "impressions": int(insight.get("impressions", 0) or 0),
                                        "clicks": int(insight.get("clicks", 0) or 0)
                                    })

                            for ad in campaign.get("ads", {}).get("data", []):
                                ad_insights = ad.get("insights", {}).get("data", [])
                                if ad_insights:
                                    insight = ad_insights[0]
                                    ads_list.append({
                                        "id": ad.get("id"),
                                        "name": ad.get("name"),
                                        "status": ad.get("status", "").title(),
                                        "spend": round(float(insight.get("spend", 0) or 0), 2),
                                        "impressions": int(insight.get("impressions", 0) or 0),
                                        "clicks": int(insight.get("clicks", 0) or 0)
                                    })

                        # Fetch leads separately (cached)
                        leads_data = await get_facebook_data(
                            current_user,
                            meta_ad_account_id,
                            "leads",
                            mongo_client
                        )

                        if not leads_data:
                            leads_data = []

                        total_leads = len(leads_data) if isinstance(leads_data, list) else 0

                        # Calculate derived metrics
                        avg_cpm = (total_spend / total_impressions * 1000) if total_impressions > 0 else 0
                        avg_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
                        avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
                        cost_per_lead = (total_spend / total_leads) if total_leads > 0 else 0

                        return {
                            "ad_account_id": meta_ad_account_id,
                            "campaigns": campaigns_list,
                            "adsets": adsets_list,
                            "ads": ads_list,
                            "leads": leads_data,
                            "summary": {
                                "total_campaigns": len(campaigns_list),
                                "total_adsets": len(adsets_list),
                                "total_ads": len(ads_list),
                                "total_leads": total_leads,
                                "total_spend": round(total_spend, 2),
                                "total_impressions": total_impressions,
                                "total_clicks": total_clicks,
                                "total_reach": total_reach,
                                "total_results": total_results,
                                "avg_cpm": round(avg_cpm, 2),
                                "avg_cpc": round(avg_cpc, 2),
                                "avg_ctr": round(avg_ctr, 2),
                                "cost_per_lead": round(cost_per_lead, 2)
                            },
                            "from_cache": False
                        }

                    return None

                except Exception as e:
                    logger.error(f"Error fetching Meta data: {str(e)}")
                    return None

            async def fetch_hp_data():
                """Fetch HotProspector data with caching"""
                if not group.get("ghl_location_id"):
                    return None

                try:
                    hp_credentials = await get_hotprospector_credentials(current_user, mongo_client)
                    if not hp_credentials:
                        return None

                    ghl_location_id = group["ghl_location_id"]

                    # Use cached data if available (1.5 days)
                    if has_fresh_cache and group.get("hotprospector_cache"):
                        cached = group["hotprospector_cache"]
                        return {
                            "ghl_location_id": ghl_location_id,
                            "total_leads": cached.get("metrics", {}).get("total_leads", 0),
                            "leads": [],
                            "total_calls": 0,
                            "from_cache": True
                        }

                    # Fetch from collection cache (1.5 days TTL)
                    cached_leads, total_count = await get_hotprospector_leads_from_collection(
                        current_user,
                        ghl_location_id,
                        mongo_client,
                        skip=0,
                        limit=None
                    )

                    # Fetch fresh if no cache
                    if not cached_leads:
                        integration = HotProspectorIntegration(
                            hp_credentials.get("api_uid"),
                            hp_credentials.get("api_key")
                        )

                        success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)

                        if success:
                            location_name = response_data.get("ghl_data", {}).get("location_name", "")
                            cached_leads = [
                                integration.normalize_lead(lead, ghl_location_id, location_name, group.get("name"))
                                for lead in hp_leads
                            ]

                            await save_hotprospector_leads_to_collection(
                                current_user,
                                ghl_location_id,
                                cached_leads,
                                mongo_client
                            )

                    # Fetch call logs if leads exist
                    if cached_leads:
                        lead_ids = [str(lead.get("id")) for lead in cached_leads if lead.get("id")]
                        integration = HotProspectorIntegration(
                            hp_credentials.get("api_uid"),
                            hp_credentials.get("api_key")
                        )
                        call_logs_map = await integration.fetch_call_logs_for_leads_batch(lead_ids)

                        for lead in cached_leads:
                            lead_id = str(lead.get("id"))
                            lead["call_logs"] = call_logs_map.get(lead_id, [])
                            lead["call_logs_count"] = len(lead["call_logs"])

                    total_calls = sum(lead.get("call_logs_count", 0) for lead in (cached_leads or []))

                    return {
                        "ghl_location_id": ghl_location_id,
                        "total_leads": len(cached_leads) if cached_leads else 0,
                        "leads": cached_leads or [],
                        "total_calls": total_calls,
                        "from_cache": False
                    }

                except Exception as e:
                    logger.error(f"Error fetching HP data: {str(e)}")
                    return None

            # Execute all fetches in parallel
            ghl_data, meta_data, hp_data = await asyncio.gather(
                fetch_ghl_data(),
                fetch_meta_data(),
                fetch_hp_data(),
                return_exceptions=True
            )

            # Handle exceptions
            if isinstance(ghl_data, Exception):
                logger.error(f"GHL fetch failed: {ghl_data}")
                ghl_data = None
            if isinstance(meta_data, Exception):
                logger.error(f"Meta fetch failed: {meta_data}")
                meta_data = None
            if isinstance(hp_data, Exception):
                logger.error(f"HP fetch failed: {hp_data}")
                hp_data = None

            # Assign fetched data
            if ghl_data:
                response_data["ghl_data"] = ghl_data
            if meta_data:
                response_data["meta_data"] = meta_data
            if hp_data:
                response_data["hotprospector_data"] = hp_data

            # ============================================
            # STEP 3: Build aggregated views (single pass)
            # ============================================

            # Build leads data efficiently
            all_leads = []

            # Add GHL contacts
            if ghl_data and ghl_data.get("contacts"):
                all_leads.extend(ghl_data["contacts"])

            # Add Meta leads
            if meta_data and meta_data.get("leads"):
                for lead in meta_data["leads"]:
                    all_leads.append({
                        "id": lead.get("id"),
                        "name": lead.get("full_name", ""),
                        "email": lead.get("email", ""),
                        "phone": lead.get("phone_number", ""),
                        "status": "Open",
                        "created": lead.get("created_time", ""),
                        "businessName": group.get("name", ""),
                        "callCount": 0,
                        "lastContact": "N/A",
                        "value": 0
                    })

            # Merge HP call data (efficient single-pass)
            if hp_data and hp_data.get("leads"):
                hp_leads_dict = {
                    (lead.get("email") or "").lower().strip(): lead
                    for lead in hp_data["leads"]
                    if lead.get("email")
                }

                for lead in all_leads:
                    email = (lead.get("email") or "").lower().strip()
                    if email in hp_leads_dict:
                        hp_lead = hp_leads_dict[email]
                        lead["callCount"] = hp_lead.get("call_logs_count", 0)
                        lead["call_logs"] = hp_lead.get("call_logs", [])
                        if hp_lead.get("call_logs"):
                            last_call = max(hp_lead["call_logs"], key=lambda x: x.get("created_date", ""))
                            lead["lastContact"] = last_call.get("created_date", "N/A")

            # Build insights
            status_breakdown = ghl_data.get("status_breakdown", {}) if ghl_data else {}
            response_data["insights"] = {
                "tag_with_best_roas": {
                    "tag_name": "Summer Sale",
                    "roas": 3.5,
                    "change_percentage": 15
                },
                "offer_with_best_booking_rate": {
                    "offer_name": "Free Consultation",
                    "booking_rate": 0.18,
                    "change_percentage": 8
                },
                "avg_booking_delay_days": {
                    "days": 2.3,
                    "change_percentage": 12
                },
                "total_bookings_this_month": {
                    "count": status_breakdown.get("Won", 0),
                    "change_percentage": 18
                }
            }

            # Build marketing data
            response_data["marketing"] = {
                "campaigns": meta_data.get("campaigns", []) if meta_data else [],
                "adsets": meta_data.get("adsets", []) if meta_data else [],
                "ads": meta_data.get("ads", []) if meta_data else [],
                "summary": meta_data.get("summary", {}) if meta_data else {}
            }

            # Build leads data
            response_data["leads"] = {
                "all_leads": all_leads,
                "status_breakdown": status_breakdown,
                "total_leads": len(all_leads),
                "qualified_leads": status_breakdown.get("Won", 0),
                "conversion_rate": round(
                    (status_breakdown.get("Won", 0) / len(all_leads) * 100) if len(all_leads) > 0 else 0,
                    2
                )
            }

            # Build call center data
            total_calls = hp_data.get("total_calls", 0) if hp_data else 0
            response_data["call_center"] = {
                "total_calls": total_calls,
                "leads_with_calls": sum(1 for lead in all_leads if lead.get("callCount", 0) > 0),
                "avg_calls_per_lead": round(
                    (total_calls / len(all_leads)) if len(all_leads) > 0 else 0,
                    2
                ),
                "leads_by_call_count": all_leads
            }

            elapsed = time.time() - start_time
            logger.info(
                f"⚡ Fetched comprehensive data for group {group_id} in {elapsed:.2f}s "
                f"(cache: {has_fresh_cache}, {len(all_leads)} leads, {total_calls} calls)"
            )

            return {
                "success": True,
                "data": response_data,
                "message": f"Successfully fetched comprehensive data for client group {group_id}",
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                    "used_cache": has_fresh_cache,
                    "cache_age_seconds": cache_age,
                    "cache_duration_seconds": CACHE_DURATION_SECONDS
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching comprehensive data for client group {group_id}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch client group data: {str(e)}")


@app.patch("/api/client-groups/{group_id}/notes")
async def update_client_group_notes(
        group_id: str,
        request: Request,
        current_user: str = Depends(get_current_user)
):
    """Update notes for a client group"""
    async with get_mongo_client() as mongo_client:
        try:
            body = await request.json()
            notes = body.get("notes", "")

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups_collection = db["client_groups"]

            result = await client_groups_collection.update_one(
                {"id": group_id, "user_id": current_user},
                {
                    "$set": {
                        "notes": notes,
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Client group not found")

            return {"message": "Notes updated successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating notes: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to update notes: {str(e)}")



@app.get("/api/facebook/adaccounts/{account_id}/data/optimized")
async def get_facebook_account_data_optimized(
        account_id: str,
        current_user: str = Depends(get_current_user)
):
    """OPTIMIZED: Fetch account data with 5-minute cache"""
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()

            token = await get_facebook_token(current_user, mongo_client)
            if not token or not token.get("access_token"):
                raise HTTPException(status_code=400, detail="No Meta token available")

            fetcher = MetaDataFetcher(current_user, mongo_client, token)
            data = await fetcher.fetch_account_data_parallel(account_id)

            if not data:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to fetch data for account {account_id}"
                )

            elapsed = time.time() - start_time

            return {
                "data": data,
                "meta": {
                    "total_campaigns": len(data.get("data", [])),
                    "response_time_ms": int(elapsed * 1000)
                },
                "message": f"Successfully fetched data for account {account_id}"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/facebook/batch-accounts")
async def get_facebook_batch_accounts(
        request: Request,
        current_user: str = Depends(get_current_user)
):
    """NEW ENDPOINT: Fetch multiple ad accounts in parallel"""
    async with get_mongo_client() as mongo_client:
        try:
            start_time = time.time()
            body = await request.json()
            account_ids = body.get("account_ids", [])

            if not account_ids:
                raise HTTPException(status_code=400, detail="account_ids is required")

            token = await get_facebook_token(current_user, mongo_client)
            if not token:
                raise HTTPException(status_code=400, detail="No Meta token")

            # Fetch all in parallel
            account_data = await fetch_all_accounts_parallel(
                current_user,
                mongo_client,
                token,
                account_ids
            )

            elapsed = time.time() - start_time
            logger.info(
                f"⚡ Fetched {len(account_data)} accounts in {elapsed:.2f}s "
                f"(avg {elapsed / len(account_data):.2f}s per account)"
            )

            return {
                "data": account_data,
                "meta": {
                    "total_accounts": len(account_data),
                    "requested": len(account_ids),
                    "response_time_ms": int(elapsed * 1000)
                },
                "message": f"Successfully fetched {len(account_data)} accounts"
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error: {str(e)}")
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/prefetch-data")
async def prefetch_marketing_data(
        current_user: str = Depends(get_current_user)
):
    """NEW ENDPOINT: Prefetch all marketing data in the background"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            client_groups = await db["client_groups"].find(
                {"user_id": current_user},
                {"meta_ad_account_id": 1}
            ).to_list(None)

            account_ids = list(set(
                group["meta_ad_account_id"]
                for group in client_groups
                if group.get("meta_ad_account_id")
            ))

            if not account_ids:
                return {"message": "No ad accounts to prefetch", "prefetched": 0}

            token = await get_facebook_token(current_user, mongo_client)
            if not token:
                return {"message": "No Meta token available", "prefetched": 0}

            # Start prefetching in background
            asyncio.create_task(
                fetch_all_accounts_parallel(
                    current_user,
                    mongo_client,
                    token,
                    account_ids
                )
            )

            return {
                "message": "Prefetch started",
                "account_ids": account_ids,
                "prefetched": len(account_ids),
                "note": "Data will be cached in ~2-5 seconds"
            }

        except Exception as e:
            logger.error(f"Prefetch error: {str(e)}")
            return {"message": "Prefetch failed", "error": str(e), "prefetched": 0}


# Updated GET /api/contacts/ghl-paginated endpoint

# Updated GET /api/contacts/ghl-paginated endpoint

@app.get("/api/contacts/ghl-paginated")
async def get_ghl_contacts_paginated_v2(
        page: int = Query(default=1, ge=1),
        limit: int = Query(default=100, ge=1, le=500),
        groups: str = Query(default=""),
        start_date: Optional[str] = Query(default=None),
        end_date: Optional[str] = Query(default=None),
        current_user: str = Depends(get_current_user)
):
    """
    UPDATED: Fetch GHL contacts (works with new /contacts/search data structure)

    Returns contacts in DESCENDING order (newest first)
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            contacts_collection = db["ghl_contacts"]

            # Parse group IDs
            group_ids = [g.strip() for g in groups.split(',') if g.strip()] if groups else []

            # Build query
            query = {"user_id": current_user}

            if group_ids:
                query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}

                if start_date:
                    try:
                        start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00'))
                        start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                        date_filter["$gte"] = start_dt.isoformat()
                    except ValueError:
                        logger.warning(f"Invalid start_date format: {start_date}")

                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00'))
                        end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
                        date_filter["$lte"] = end_dt.isoformat()
                    except ValueError:
                        logger.warning(f"Invalid end_date format: {end_date}")

                if date_filter:
                    query["contact_data.dateAdded"] = date_filter

            # Get total count
            total_contacts = await contacts_collection.count_documents(query)

            if total_contacts == 0:
                return {
                    "contacts": [],
                    "meta": {
                        "total_contacts": 0,
                        "current_page": page,
                        "total_pages": 0,
                        "per_page": limit,
                        "has_next": False,
                        "has_prev": False
                    },
                    "message": "No contacts found"
                }

            # Calculate pagination
            skip = (page - 1) * limit
            total_pages = (total_contacts + limit - 1) // limit

            # Fetch contacts (sorted newest first)
            cursor = contacts_collection.find(
                query,
                {
                    "contact_data": 1,
                    "client_group_name": 1,
                    "location_name": 1,
                    "client_group_id": 1,
                    "location_id": 1
                }
            ).sort("contact_data.dateAdded", -1).skip(skip).limit(limit)

            contact_docs = await cursor.to_list(length=limit)

            # Format contacts - extract ALL fields from contact_data
            contacts = []
            for doc in contact_docs:
                contact = doc.get("contact_data", {})

                # Normalize email
                email = contact.get("email")
                if email is None or (isinstance(email, str) and not email.strip()):
                    email = f"no_email_ghl_{contact.get('id')}"
                elif isinstance(email, str):
                    email = email.strip().lower()

                # Normalize name
                contact_name = contact.get("contactName", "") or ""
                if isinstance(contact_name, str):
                    contact_name = contact_name.strip()

                if not contact_name:
                    first_name = (contact.get("firstName") or "").strip()
                    last_name = (contact.get("lastName") or "").strip()
                    contact_name = f"{first_name} {last_name}".strip() if first_name or last_name else "Unknown"

                # Extract ALL fields from the contact data
                formatted_contact = {
                    # Basic info
                    "contactId": contact.get("id"),
                    "contactName": contact_name,
                    "firstName": contact.get("firstName") or "",
                    "lastName": contact.get("lastName") or "",
                    "email": email,
                    "phone": contact.get("phone") or "",

                    # Location/group info
                    "locationId": doc.get("location_id"),
                    "groupName": doc.get("client_group_name", "Unknown Group"),

                    # Dates
                    "dateAdded": contact.get("dateAdded") or "",
                    "dateUpdated": contact.get("dateUpdated") or "",
                    "dateOfBirth": contact.get("dateOfBirth") or "",

                    # Contact details
                    "tags": contact.get("tags") or [],
                    "source": contact.get("source") or "",
                    "type": contact.get("type") or "lead",
                    "contactType": contact.get("contactType") or contact.get("type") or "lead",

                    # Address
                    "address1": contact.get("address1") or "",
                    "city": contact.get("city") or "",
                    "state": contact.get("state") or "",
                    "postalCode": contact.get("postalCode") or "",
                    "country": contact.get("country") or "",
                    "timezone": contact.get("timezone") or "",

                    # Business info
                    "companyName": contact.get("companyName") or "",
                    "website": contact.get("website") or "",
                    "businessId": contact.get("businessId") or "",

                    # Additional fields
                    "dnd": contact.get("dnd", False),
                    "dndSettings": contact.get("dndSettings") or {},
                    "customFields": contact.get("customFields") or [],
                    "followers": contact.get("followers") or [],
                    "assignedTo": contact.get("assignedTo") or "",

                    # Opportunities
                    "opportunities": contact.get("opportunities") or [],

                    # Attribution
                    "attributionSource": contact.get("attributionSource") or {},
                    "lastAttributionSource": contact.get("lastAttributionSource") or {},
                }

                contacts.append(formatted_contact)

            elapsed = time.time() - start_time

            logger.info(
                f"⚡ Fetched page {page} (newest first): {len(contacts)} contacts "
                f"in {elapsed:.3f}s"
            )

            return {
                "contacts": contacts,
                "meta": {
                    "total_contacts": total_contacts,
                    "current_page": page,
                    "total_pages": total_pages,
                    "per_page": limit,
                    "returned": len(contacts),
                    "has_next": page < total_pages,
                    "has_prev": page > 1,
                    "sort_order": "newest_first",
                    "date_filtered": bool(start_date or end_date),
                    "start_date": start_date,
                    "end_date": end_date
                },
                "message": f"Retrieved {len(contacts)} contacts (newest first)" +
                           (f" from {start_date} to {end_date}" if (start_date or end_date) else ""),
                "performance": {
                    "response_time_ms": int(elapsed * 1000),
                    "source": "mongodb"
                }
            }

        except Exception as e:
            logger.error(f"Error fetching paginated contacts: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to fetch contacts: {str(e)}"
            )

from datetime import datetime
from typing import Dict, List, Tuple
import httpx
from pymongo import UpdateOne
from collections import Counter


# CORRECTED: fetch_and_cache_ghl_data_optimized for ACTUAL API response

async def fetch_and_cache_ghl_data_optimized(
        group_id: str,
        ghl_location_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    CORRECTED: Fetch GHL contacts using NEW /contacts/search endpoint

    ACTUAL API RESPONSE:
    {
        "contacts": [...],
        "total": 2152,
        "traceId": "..."
    }

    We calculate pagination metadata ourselves since API only returns total.

    Args:
        group_id: Client group ID
        ghl_location_id: GHL location ID
        user_id: User ID
        mongo_client: MongoDB client
        is_initial_load: If True, fetch all. If False, incremental update
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        contacts_collection = db["ghl_contacts"]
        client_groups_collection = db["client_groups"]

        # Get location info
        from integrations.gohighlevel import get_subaccount_tokens, ghl_integration
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_data = subaccount_tokens.get(ghl_location_id, {})

        access_token = location_data.get("access_token")
        if not access_token:
            logger.warning(f"No access token for GHL location {ghl_location_id}")
            return

        location_name = location_data.get("name", "Unknown Location")
        location_address = location_data.get("address", "")

        # Get client group info
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        # ============================================
        # STEP 1: Determine last known contact for incremental refresh
        # ============================================
        last_known_contact_date = None
        last_known_contact_id = None

        if not is_initial_load:
            last_contact = await contacts_collection.find_one(
                {
                    "user_id": user_id,
                    "location_id": ghl_location_id
                },
                sort=[("contact_data.dateAdded", -1)]
            )

            if last_contact:
                last_known_contact_date = last_contact.get("contact_data", {}).get("dateAdded")
                last_known_contact_id = last_contact.get("contact_id")

                if last_known_contact_date:
                    logger.info(
                        f"🔄 INCREMENTAL: Last known contact from {last_known_contact_date} "
                        f"(ID: {last_known_contact_id})"
                    )

        if is_initial_load:
            logger.info(f"🔄 FULL LOAD: Fetching all contacts for {location_name}")
            # Delete existing contacts for clean slate
            delete_result = await contacts_collection.delete_many({
                "user_id": user_id,
                "location_id": ghl_location_id
            })
            logger.info(f"🗑️ Deleted {delete_result.deleted_count} existing contacts")

        # ============================================
        # STEP 2: Fetch contacts using NEW page-based API
        # ============================================
        page = 1
        total_contacts_processed = 0
        total_new_contacts = 0
        total_updated_contacts = 0
        should_continue = True

        # Tag counter for metrics
        from collections import Counter
        tag_counter = Counter()

        # Track total (we'll know after first request)
        total_contacts_api = None
        total_pages = None

        while should_continue:
            try:
                # Fetch page using NEW search endpoint
                success, result = await ghl_integration.fetch_contacts_search(
                    ghl_location_id,
                    access_token,
                    page=page,
                    limit=500
                )

                if not success:
                    logger.error(f"❌ Failed to fetch page {page}: {result.get('error')}")
                    break

                # Extract from ACTUAL response format
                contacts_batch = result.get("contacts", [])
                total_contacts_api = result.get("total", 0)
                meta = result.get("meta", {})  # Our calculated metadata

                # Get pagination info from our calculated meta
                if total_pages is None:
                    total_pages = meta.get("totalPages", 1)
                    logger.info(
                        f"📊 Total pages: {total_pages}, Total contacts: {total_contacts_api}"
                    )

                if not contacts_batch:
                    logger.info(f"✅ No more contacts at page {page}")
                    break

                # ============================================
                # STEP 3: Check for last known contact (incremental mode)
                # ============================================
                contacts_to_save = []

                for contact in contacts_batch:
                    contact_id = contact.get("id")
                    date_added = contact.get("dateAdded")

                    # Stop condition: hit last known contact
                    if not is_initial_load and last_known_contact_id:
                        if contact_id == last_known_contact_id:
                            logger.info(
                                f"⚡ OPTIMIZATION: Found last known contact at page {page}"
                            )
                            should_continue = False
                            break

                        # Also check by date
                        if date_added and last_known_contact_date:
                            try:
                                contact_date = datetime.fromisoformat(
                                    date_added.replace('Z', '+00:00')
                                )
                                last_date = datetime.fromisoformat(
                                    last_known_contact_date.replace('Z', '+00:00')
                                )

                                if contact_date <= last_date:
                                    logger.info(
                                        f"⚡ OPTIMIZATION: Hit older data at page {page}"
                                    )
                                    should_continue = False
                                    break
                            except Exception as e:
                                logger.debug(f"Date comparison error: {e}")

                    # Contact is new, add to save list
                    contacts_to_save.append(contact)

                    # Count tags for metrics
                    tags = contact.get("tags", [])
                    if tags:
                        for tag in tags:
                            tag_counter[tag] += 1

                # ============================================
                # STEP 4: Save contacts to database
                # ============================================
                if contacts_to_save:
                    if is_initial_load:
                        # FULL LOAD: Insert all as new documents
                        contact_docs = []
                        for contact in contacts_to_save:
                            contact_docs.append({
                                "user_id": user_id,
                                "location_id": ghl_location_id,
                                "location_name": location_name,
                                "location_address": location_address,
                                "client_group_id": group_id,
                                "client_group_name": client_group_name,
                                "contact_id": contact.get("id"),
                                "contact_data": contact,  # Save EVERYTHING
                                "created_at": datetime.now(),
                                "updated_at": datetime.now()
                            })

                        if contact_docs:
                            await contacts_collection.insert_many(contact_docs, ordered=False)
                            total_new_contacts += len(contact_docs)
                            logger.info(
                                f"💾 Page {page}/{total_pages}: Inserted {len(contact_docs)} contacts "
                                f"(total: {total_new_contacts}/{total_contacts_api})"
                            )

                    else:
                        # INCREMENTAL: Upsert contacts
                        from pymongo import UpdateOne
                        bulk_operations = []

                        for contact in contacts_to_save:
                            contact_id = contact.get("id")

                            contact_doc = {
                                "user_id": user_id,
                                "location_id": ghl_location_id,
                                "location_name": location_name,
                                "location_address": location_address,
                                "client_group_id": group_id,
                                "client_group_name": client_group_name,
                                "contact_id": contact_id,
                                "contact_data": contact,  # Save EVERYTHING
                                "updated_at": datetime.now()
                            }

                            bulk_operations.append(
                                UpdateOne(
                                    {
                                        "user_id": user_id,
                                        "location_id": ghl_location_id,
                                        "contact_id": contact_id
                                    },
                                    {
                                        "$set": contact_doc,
                                        "$setOnInsert": {"created_at": datetime.now()}
                                    },
                                    upsert=True
                                )
                            )

                        if bulk_operations:
                            result = await contacts_collection.bulk_write(
                                bulk_operations, ordered=False
                            )
                            total_new_contacts += result.upserted_count
                            total_updated_contacts += result.modified_count

                            logger.info(
                                f"💾 Page {page}/{total_pages}: {result.upserted_count} new, "
                                f"{result.modified_count} updated"
                            )

                total_contacts_processed += len(contacts_batch)

                # Check if we should stop
                if not should_continue:
                    logger.info(f"⚡ Stopping early - found last known contact")
                    break

                # Check if there's a next page
                has_next = meta.get("hasNext", False)
                if not has_next or len(contacts_batch) == 0:
                    logger.info(f"✅ Reached last page (page {page}/{total_pages})")
                    break

                # Move to next page
                page += 1

                # Small delay to avoid rate limiting
                await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"❌ Error on page {page}: {str(e)}", exc_info=True)
                break

        # ============================================
        # STEP 5: Update cached tag metrics
        # ============================================
        existing_tag_metrics = {}
        if not is_initial_load:
            existing_group = await client_groups_collection.find_one(
                {"id": group_id},
                {"gohighlevel_cache.metrics.tag_breakdown": 1}
            )

            if existing_group:
                existing_tag_metrics = (existing_group.get("gohighlevel_cache", {})
                                        .get("metrics", {})
                                        .get("tag_breakdown", {}))

        # Merge existing tag counts
        if existing_tag_metrics:
            for tag, count in existing_tag_metrics.items():
                tag_counter[tag] += count

        # Get final contact count
        total_contacts_in_db = await contacts_collection.count_documents({
            "user_id": user_id,
            "location_id": ghl_location_id
        })

        # Convert tag counter to sorted dict
        tag_metrics = dict(tag_counter.most_common())

        # Update cache
        cache_data = {
            "location_id": ghl_location_id,
            "name": location_name,
            "address": location_address,
            "metrics": {
                "total_contacts": total_contacts_in_db,
                "tag_breakdown": tag_metrics
            }
        }

        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "gohighlevel_cache": cache_data,
                    "last_ghl_refresh": datetime.utcnow()
                }
            }
        )

        mode = "FULL LOAD" if is_initial_load else "INCREMENTAL"
        logger.info(
            f"✅ {mode} complete for {location_name}: "
            f"{total_contacts_in_db} total contacts in DB "
            f"({total_new_contacts} new, {total_updated_contacts} updated), "
            f"{len(tag_metrics)} unique tags tracked"
        )

        # Log top 5 tags
        if tag_metrics:
            top_tags = list(tag_metrics.items())[:5]
            logger.info(f"📊 Top tags: {top_tags}")

    except Exception as e:
        logger.error(f"Error in fetch_and_cache_ghl_data_optimized: {e}", exc_info=True)
        raise

async def get_ghl_contacts_sorted(
        user_id: str,
        client_group_id: str,
        mongo_client,
        skip: int = 0,
        limit: int = 100
) -> Tuple[List[Dict], int]:
    """
    Fetch GHL contacts in DESCENDING chronological order (newest first).
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
    contacts_collection = db["ghl_contacts"]

    query = {
        "user_id": user_id,
        "client_group_id": client_group_id
    }

    # Get total count
    total_count = await contacts_collection.count_documents(query)

    # 🔥 FIX: Sort by dateAdded DESC (newest first)
    cursor = contacts_collection.find(query).sort(
        "contact_data.dateAdded", -1  # -1 = DESCENDING (newest first)
    ).skip(skip).limit(limit)

    contacts = []
    async for doc in cursor:
        contact_data = doc.get("contact_data", {})
        contacts.append({
            "contact_id": doc.get("contact_id"),
            "contact_data": contact_data,
            "client_group_name": doc.get("client_group_name"),
            "location_name": doc.get("location_name"),
            "date_added": contact_data.get("dateAdded"),
            "tags": contact_data.get("tags", [])
        })

    return contacts, total_count

async def get_tag_metrics_from_cache(
        mongo_client,
        group_id: str,
        user_id: str
) -> Dict[str, int]:
    """
    Get tag metrics from client_groups cache.

    Returns: {"tag_name": count, ...}
    """
    db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
    client_groups_collection = db["client_groups"]

    group = await client_groups_collection.find_one(
        {"id": group_id, "user_id": user_id},
        {"gohighlevel_cache.metrics.tag_breakdown": 1}
    )

    if not group:
        return {}

    return (group.get("gohighlevel_cache", {})
            .get("metrics", {})
            .get("tag_breakdown", {}))


# In main.py

@app.get("/api/campaign-insights")
async def get_campaign_insights(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        groups: Optional[str] = None,  # Comma-separated group IDs
        current_user: str = Depends(get_current_user)
):
    """Get campaign insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            insights_collection = db["facebook_campaign_insights"]

            # Build query
            query = {"user_id": current_user}

            # Filter by client groups
            if groups:
                group_ids = [g.strip() for g in groups.split(',') if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            # Filter by date range
            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            # Fetch insights
            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights)
            }
        except Exception as e:
            logger.error(f"Error: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/adset-insights")
async def get_adset_insights(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        groups: Optional[str] = None,
        current_user: str = Depends(get_current_user)
):
    """Get adset insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            insights_collection = db["facebook_adset_insights"]

            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(',') if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ad-insights")
async def get_ad_insights(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        groups: Optional[str] = None,
        current_user: str = Depends(get_current_user)
):
    """Get ad insights with date filtering"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            insights_collection = db["facebook_ad_insights"]

            query = {"user_id": current_user}

            if groups:
                group_ids = [g.strip() for g in groups.split(',') if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["date_start"] = date_filter

            cursor = insights_collection.find(query).sort("date_start", -1)
            insights = await cursor.to_list(length=None)

            return {
                "insights": [mongo_to_dict(i) for i in insights],
                "total": len(insights)
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/facebook-leads/filtered")
async def get_facebook_leads_filtered(
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        groups: Optional[str] = None,
        limit: int = Query(default=5000, ge=1, le=10000),
        current_user: str = Depends(get_current_user)
):
    """
    Get Facebook leads filtered by date range and groups.
    Used by the Marketing Hub for date-based queries.
    """
    async with get_mongo_client() as mongo_client:
        try:
            import time
            start_time = time.time()

            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            leads_collection = db["facebook_leads"]

            # Build query
            query = {"user_id": current_user}

            # Filter by client groups
            if groups:
                group_ids = [g.strip() for g in groups.split(',') if g.strip()]
                if group_ids:
                    query["client_group_id"] = {"$in": group_ids}

            # Filter by date range
            if start_date or end_date:
                date_filter = {}
                if start_date:
                    date_filter["$gte"] = start_date
                if end_date:
                    date_filter["$lte"] = end_date
                query["lead_data.created_time"] = date_filter

            # Fetch leads (sorted newest first)
            cursor = leads_collection.find(
                query,
                {
                    "lead_data": 1,
                    "client_group_name": 1,
                    "ad_account_id": 1,
                    "client_group_id": 1
                }
            ).sort("lead_data.created_time", -1).limit(limit)

            lead_docs = await cursor.to_list(length=limit)

            # Format leads
            leads = []
            for doc in lead_docs:
                lead_data = doc.get("lead_data", {})
                leads.append({
                    "lead_id": lead_data.get("id"),
                    "full_name": lead_data.get("full_name", ""),
                    "email": lead_data.get("email", ""),
                    "phone_number": lead_data.get("phone_number", ""),
                    "ad_name": lead_data.get("ad_name", ""),
                    "campaign_name": lead_data.get("campaign_name", ""),
                    "platform": lead_data.get("platform", ""),
                    "created_time": lead_data.get("created_time", ""),
                    "group_name": doc.get("client_group_name", "Unknown Group"),
                    "ad_account_id": doc.get("ad_account_id"),
                    "field_data": lead_data.get("field_data", {})
                })

            elapsed = time.time() - start_time

            logger.info(
                f"⚡ Fetched {len(leads)} filtered leads in {elapsed:.3f}s "
                f"(date range: {start_date} to {end_date})"
            )

            return {
                "leads": leads,
                "meta": {
                    "total": len(leads),
                    "returned": len(leads),
                    "limit": limit,
                    "start_date": start_date,
                    "end_date": end_date
                },
                "message": f"Retrieved {len(leads)} leads",
                "performance": {
                    "response_time_ms": int(elapsed * 1000)
                }
            }

        except Exception as e:
            logger.error(f"Error fetching filtered leads: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to fetch leads: {str(e)}")


# Add these three endpoints to your main.py
# Place them near your other integration endpoints (e.g. after /api/hotprospector/connect)

# ============================================================
# REMOVE INTEGRATION ENDPOINTS
# ============================================================

@app.delete("/api/integrations/gohighlevel/remove")
async def remove_gohighlevel_integration(
    response: Response,
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove GoHighLevel integration for the current user.
    Deletes agency token AND all subaccount tokens from MongoDB.
    Also clears the gohighlevel_tokens cookie.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.gohighlevel": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            # Clear the cookie
            response.delete_cookie(
                key="gohighlevel_tokens",
                path="/",
                domain=COOKIE_DOMAIN,
                samesite=COOKIE_SAMESITE,
                secure=COOKIE_SECURE,
            )

            logger.info(f"✅ Removed GoHighLevel integration for user: {current_user}")
            return {"success": True, "message": "GoHighLevel integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing GHL integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")


@app.delete("/api/integrations/facebook/remove")
async def remove_facebook_integration(
    response: Response,
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove Meta (Facebook) integration for the current user.
    Deletes the access token from MongoDB and clears the facebook_tokens cookie.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.facebook": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            # Clear the cookie
            response.delete_cookie(
                key="facebook_tokens",
                path="/",
                domain=COOKIE_DOMAIN,
                samesite=COOKIE_SAMESITE,
                secure=COOKIE_SECURE,
            )

            logger.info(f"✅ Removed Meta integration for user: {current_user}")
            return {"success": True, "message": "Meta integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing Facebook integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")


@app.delete("/api/integrations/hotprospector/remove")
async def remove_hotprospector_integration(
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove HotProspector integration for the current user.
    Deletes API credentials from MongoDB.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.hotprospector": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            logger.info(f"✅ Removed HotProspector integration for user: {current_user}")
            return {"success": True, "message": "HotProspector integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing HotProspector integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")