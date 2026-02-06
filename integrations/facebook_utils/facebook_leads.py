
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
import asyncio
import logging
from datetime import datetime
from typing import List, Tuple
import httpx
import os

logger = logging.getLogger(__name__)

async def create_facebook_leads_indexes(mongo_client):
    """Create optimized indexes for Facebook leads collection"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        leads_collection = db["facebook_leads"]

        # Unique index
        await leads_collection.create_index(
            [
                ("user_id", 1),
                ("ad_account_id", 1),
                ("lead_id", 1)
            ],
            unique=True,
            name="user_account_lead_unique"
        )

        # Index for chronological sorting (newest first)
        await leads_collection.create_index(
            [
                ("user_id", 1),
                ("ad_account_id", 1),
                ("lead_data.created_time", -1)
            ],
            name="user_account_date_desc"
        )

        # Index for client group queries
        await leads_collection.create_index(
            [
                ("user_id", 1),
                ("client_group_id", 1),
                ("lead_data.created_time", -1)
            ],
            name="user_group_date_desc"
        )

        logger.info("✅ Created indexes for facebook_leads")

    except Exception as e:
        logger.error(f"Error creating Facebook leads indexes: {e}")

# ============================================
# INTEGRATION WITH EXISTING CODE
# ============================================


async def fetch_and_cache_facebook_leads_FIXED(
        group_id: str,
        meta_ad_account_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    UPDATED: Fetch Facebook leads and update cache with accurate count.

    Now calculates and updates metrics directly during save.
    """
    try:
        # Import the staged leads fetcher from main.py

        from integrations.facebook_utils.facebook import get_facebook_token

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        facebook_leads_collection = db["facebook_leads"]
        client_groups_collection = db["client_groups"]

        # Get Facebook token
        token = await get_facebook_token(user_id, mongo_client)

        if not token or not token.get("access_token"):
            logger.warning(f"No Facebook token for user {user_id}")
            return

        # Get client group info
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        logger.info(
            f"🔄 Starting staged leads fetch for '{client_group_name}' "
            f"(account: {meta_ad_account_id})"
        )

        # Clear existing if initial load
        if is_initial_load:
            delete_result = await facebook_leads_collection.delete_many({
                "user_id": user_id,
                "ad_account_id": meta_ad_account_id,
                "client_group_id": group_id
            })
            logger.info(f"🗑️ Deleted {delete_result.deleted_count} existing leads")

        # Fetch all leads using staged approach
        total_leads, all_leads = await fetch_all_facebook_leads_staged(
            meta_ad_account_id,
            token["access_token"],
            user_id,
            group_id,
            client_group_name,
            max_concurrent_ads=5
        )

        # ============================================
        # Save to database AND count
        # ============================================
        total_saved = 0

        if all_leads:
            lead_docs = []
            for lead in all_leads:
                lead_docs.append({
                    "user_id": user_id,
                    "ad_account_id": meta_ad_account_id,
                    "client_group_id": group_id,
                    "client_group_name": client_group_name,
                    "lead_id": lead.get("lead_id"),
                    "lead_data": lead,
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                })

            # Insert in batches
            batch_size = 500
            for i in range(0, len(lead_docs), batch_size):
                batch = lead_docs[i:i + batch_size]

                try:
                    result = await facebook_leads_collection.insert_many(
                        batch,
                        ordered=False
                    )
                    total_saved += len(result.inserted_ids)

                    logger.info(
                        f"💾 Saved batch {i // batch_size + 1}: {len(batch)} leads"
                    )
                except Exception as e:
                    logger.error(f"Error saving batch: {str(e)}")

        logger.info(
            f"✅ COMPLETE: Saved {total_saved} leads for "
            f"'{client_group_name}' (account: {meta_ad_account_id})"
        )

        # ============================================
        # 🔥 UPDATE CACHE with actual saved count
        # ============================================
        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "facebook_cache.total_leads": total_saved,
                    "facebook_cache.metrics.total_leads": total_saved,
                    "last_meta_refresh": datetime.utcnow()
                }
            }
        )

        logger.info(f"📊 Updated cache: {total_saved} leads")

    except Exception as e:
        logger.error(
            f"❌ Error in fetch_and_cache_facebook_leads_FIXED: {str(e)}",
            exc_info=True
        )
        raise

async def stage1_get_all_ad_ids(
        ad_account_id: str,
        access_token: str
) -> List[str]:
    """
    STAGE 1: Get ALL ad IDs from the account.

    Handles pagination to get complete list.

    Returns: List of ad IDs
    """
    all_ad_ids = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Initial request
            url = f"https://graph.facebook.com/v23.0/{ad_account_id}/ads"
            params = {
                "fields": "id",  # Only need IDs
                "access_token": access_token,
                "limit": 500,  # Max per page
                "date_preset": "maximum"
            }

            page = 0
            next_url = None

            while True:
                page += 1
                logger.info(f"📄 Fetching ad IDs page {page}")

                # Fetch page
                if page == 1:
                    response = await client.get(url, params=params)
                else:
                    response = await client.get(next_url)

                if response.status_code != 200:
                    logger.error(
                        f"❌ Failed to fetch ads: {response.status_code} - {response.text}"
                    )
                    break

                data = response.json()
                ads = data.get("data", [])

                if not ads:
                    logger.info(f"✅ No more ads at page {page}")
                    break

                # Extract ad IDs
                ad_ids = [ad["id"] for ad in ads]
                all_ad_ids.extend(ad_ids)

                logger.info(
                    f"✅ Page {page}: Got {len(ad_ids)} ad IDs "
                    f"(total: {len(all_ad_ids)})"
                )

                # Check for next page
                paging = data.get("paging", {})
                next_url = paging.get("next")

                if not next_url:
                    logger.info(f"✅ Reached last page at page {page}")
                    break

                # Small delay
                await asyncio.sleep(0.1)

            logger.info(f"✅ STAGE 1 COMPLETE: Got {len(all_ad_ids)} total ad IDs")
            return all_ad_ids

    except Exception as e:
        logger.error(f"❌ Error in stage 1: {str(e)}", exc_info=True)
        return all_ad_ids


async def stage2_get_all_leads_for_ad(
        ad_id: str,
        access_token: str
) -> List[dict]:
    """
    STAGE 2: Get ALL leads for a specific ad.

    Uses the clean endpoint: /{ad_id}?fields=leads&date_preset=maximum

    Returns: List of leads for this ad
    """
    all_leads = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Initial request - REMOVED campaign_name and adset_name
            url = f"https://graph.facebook.com/v23.0/{ad_id}"
            params = {
                "fields": "leads,name",  # 🔥 FIXED: Only request ad-level fields
                "date_preset": "maximum",
                "access_token": access_token
            }

            # Get ad info and first page of leads
            response = await client.get(url, params=params)

            if response.status_code != 200:
                logger.error(
                    f"❌ Failed to fetch ad {ad_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return []

            ad_data = response.json()
            ad_name = ad_data.get("name", "")

            # 🔥 NOTE: Campaign/adset info comes from the lead's field_data
            # not from the ad object itself

            leads_data = ad_data.get("leads", {})

            # Process first page of leads
            first_leads = leads_data.get("data", [])
            for lead in first_leads:
                lead["ad_name"] = ad_name
                # campaign_name and platform come from lead's own data
                all_leads.append(lead)

            logger.info(
                f"  📊 Ad {ad_id}: Got {len(first_leads)} leads (page 1)"
            )

            # Get pagination URL
            paging = leads_data.get("paging", {})
            next_url = paging.get("next")

            page = 1

            # Paginate through remaining leads
            while next_url:
                page += 1

                try:
                    response = await client.get(next_url)

                    if response.status_code != 200:
                        logger.error(
                            f"❌ Failed to fetch page {page} for ad {ad_id}: "
                            f"{response.status_code}"
                        )
                        break

                    page_data = response.json()
                    more_leads = page_data.get("data", [])

                    if not more_leads:
                        logger.info(f"  ✅ No more leads at page {page}")
                        break

                    # Add ad metadata to leads
                    for lead in more_leads:
                        lead["ad_name"] = ad_name
                        all_leads.append(lead)

                    logger.info(
                        f"  📊 Ad {ad_id}: Got {len(more_leads)} leads (page {page})"
                    )

                    # Get next page
                    next_paging = page_data.get("paging", {})
                    next_url = next_paging.get("next")

                    # Small delay
                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(
                        f"❌ Error fetching page {page} for ad {ad_id}: {str(e)}"
                    )
                    break

            logger.info(
                f"  ✅ Ad {ad_id}: COMPLETE - {len(all_leads)} total leads"
            )
            return all_leads

    except Exception as e:
        logger.error(f"❌ Error fetching leads for ad {ad_id}: {str(e)}", exc_info=True)
        return all_leads

async def fetch_all_facebook_leads_staged(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        max_concurrent_ads: int = 5
) -> Tuple[int, List[dict]]:
    """
    MAIN FUNCTION: Fetch all Facebook leads using staged approach.

    Stage 1: Get all ad IDs
    Stage 2: Fetch leads for each ad (with controlled concurrency)

    Args:
        ad_account_id: Meta ad account ID
        access_token: Facebook access token
        user_id: User ID
        client_group_id: Client group ID
        client_group_name: Client group name
        max_concurrent_ads: Max ads to fetch in parallel (default: 5)

    Returns:
        (total_leads_count, all_leads_list)
    """
    start_time = datetime.utcnow()

    logger.info(f"🚀 Starting staged Facebook leads fetch for {ad_account_id}")

    # ============================================
    # STAGE 1: Get all ad IDs
    # ============================================
    logger.info("📋 STAGE 1: Fetching all ad IDs...")
    ad_ids = await stage1_get_all_ad_ids(ad_account_id, access_token)

    if not ad_ids:
        logger.warning("⚠️ No ad IDs found")
        return 0, []

    logger.info(f"✅ STAGE 1 COMPLETE: {len(ad_ids)} ads to process")

    # ============================================
    # STAGE 2: Fetch leads for each ad (with concurrency control)
    # ============================================
    logger.info(f"📋 STAGE 2: Fetching leads from {len(ad_ids)} ads...")

    all_leads = []

    # Process ads in batches to control concurrency
    for i in range(0, len(ad_ids), max_concurrent_ads):
        batch = ad_ids[i:i + max_concurrent_ads]
        batch_num = (i // max_concurrent_ads) + 1
        total_batches = (len(ad_ids) + max_concurrent_ads - 1) // max_concurrent_ads

        logger.info(
            f"🔄 Processing batch {batch_num}/{total_batches} "
            f"({len(batch)} ads)"
        )

        # Fetch leads for this batch in parallel
        tasks = [
            stage2_get_all_leads_for_ad(ad_id, access_token)
            for ad_id in batch
        ]

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect results
        for j, result in enumerate(batch_results):
            if isinstance(result, Exception):
                logger.error(f"❌ Error in batch {batch_num}, ad {j}: {result}")
            else:
                all_leads.extend(result)

        logger.info(
            f"✅ Batch {batch_num}/{total_batches} complete: "
            f"{sum(len(r) for r in batch_results if not isinstance(r, Exception))} leads"
        )

        # Small delay between batches
        if i + max_concurrent_ads < len(ad_ids):
            await asyncio.sleep(0.5)

    # ============================================
    # Process and normalize leads
    # ============================================
    normalized_leads = []

    for lead in all_leads:
        # Parse field_data
        field_data = {}
        for field in lead.get("field_data", []):
            field_name = field.get("name", "")
            field_values = field.get("values", [])
            if field_values:
                field_data[field_name] = field_values[0]

        normalized_lead = {
            "lead_id": lead.get("id"),
            "ad_name": lead.get("ad_name", ""),
            "adset_name": lead.get("adset_name", ""),
            "platform": lead.get("platform", ""),
            "created_time": lead.get("created_time", ""),
            "full_name": field_data.get("full_name", ""),
            "email": field_data.get("email", ""),
            "phone_number": field_data.get("phone_number", ""),
            "field_data": field_data,
            "client_group_id": client_group_id,
            "client_group_name": client_group_name,
            "user_id": user_id,
            "ad_account_id": ad_account_id
        }

        normalized_leads.append(normalized_lead)

    elapsed = (datetime.utcnow() - start_time).total_seconds()

    logger.info(
        f"✅ STAGE 2 COMPLETE: {len(normalized_leads)} total leads "
        f"in {elapsed:.2f}s"
    )

    return len(normalized_leads), normalized_leads


# ============================================
# INTEGRATION WITH MAIN.PY
# ============================================

async def fetch_and_cache_facebook_leads_STAGED(
        group_id: str,
        meta_ad_account_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    PRODUCTION-READY: Drop-in replacement for existing function.

    Uses staged approach for reliability and clarity.
    """
    try:
        import os
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        facebook_leads_collection = db["facebook_leads"]
        client_groups_collection = db["client_groups"]

        # Get Facebook token
        from integrations.facebook_utils.facebook import get_facebook_token
        token = await get_facebook_token(user_id, mongo_client)

        if not token or not token.get("access_token"):
            logger.warning(f"No Facebook token for user {user_id}")
            return

        # Get client group info
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        logger.info(
            f"🔄 Starting staged leads fetch for '{client_group_name}' "
            f"(account: {meta_ad_account_id})"
        )

        # ============================================
        # Clear existing if initial load
        # ============================================
        if is_initial_load:
            delete_result = await facebook_leads_collection.delete_many({
                "user_id": user_id,
                "ad_account_id": meta_ad_account_id
            })
            logger.info(f"🗑️ Deleted {delete_result.deleted_count} existing leads")

        # ============================================
        # Fetch all leads using staged approach
        # ============================================
        total_leads, all_leads = await fetch_all_facebook_leads_staged(
            meta_ad_account_id,
            token["access_token"],
            user_id,
            group_id,
            client_group_name,
            max_concurrent_ads=5  # Process 5 ads in parallel
        )

        # ============================================
        # Save to database in batches
        # ============================================
        if all_leads:
            lead_docs = []
            for lead in all_leads:
                lead_docs.append({
                    "user_id": user_id,
                    "ad_account_id": meta_ad_account_id,
                    "client_group_id": group_id,
                    "client_group_name": client_group_name,
                    "lead_id": lead.get("lead_id"),
                    "lead_data": lead,
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                })

            # Insert in batches
            batch_size = 500
            for i in range(0, len(lead_docs), batch_size):
                batch = lead_docs[i:i + batch_size]

                try:
                    await facebook_leads_collection.insert_many(
                        batch,
                        ordered=False
                    )
                    logger.info(
                        f"💾 Saved batch {i // batch_size + 1}: {len(batch)} leads"
                    )
                except Exception as e:
                    logger.error(f"Error saving batch: {str(e)}")

        # ============================================
        # Update client group cache
        # ============================================
        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "facebook_cache.total_leads": total_leads,
                    "last_meta_refresh": datetime.utcnow()
                }
            }
        )

        logger.info(
            f"✅ COMPLETE: Saved {total_leads} leads for "
            f"'{client_group_name}' (account: {meta_ad_account_id})"
        )

    except Exception as e:
        logger.error(
            f"❌ Error in fetch_and_cache_facebook_leads_STAGED: {str(e)}",
            exc_info=True
        )
        raise

