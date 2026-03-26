"""
Facebook Ad Set Data Fetcher - Staged Approach

This module implements a two-stage approach for fetching Facebook ad set data:
1. Stage 1: Get all ad set IDs for an ad account
2. Stage 2: Fetch historical insights for each ad set (with pagination)

This approach is more reliable and easier to debug than nested pagination.
"""

import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import httpx
import os
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

from integrations.facebook_utils._api_helpers import api_get_with_backoff as _api_get_with_backoff


class FacebookAdSetFetcher:
    """
    Handles fetching Facebook ad set data using a staged approach.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.facebook.com/v23.0"

    async def stage1_get_all_adset_ids(
            self,
            ad_account_id: str
    ) -> List[Dict]:
        """
        STAGE 1: Get all ad set IDs and basic info for an ad account.

        Handles pagination to get complete list.

        Args:
            ad_account_id: Meta ad account ID (e.g., "act_415096360989203")

        Returns:
            List of ad set objects with id, name, and campaign_id
        """
        all_adsets = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"{self.base_url}/{ad_account_id}/adsets"
                params = {
                    "access_token": self.access_token,
                    "fields": "name,campaign_id",
                    "limit": 100  # Max per page
                }

                page = 0
                next_url = None

                while True:
                    page += 1
                    logger.info(f"📄 Fetching ad set IDs page {page}")

                    # Fetch page with rate-limit backoff
                    fetch_url = url if page == 1 else next_url
                    fetch_params = params if page == 1 else None
                    ok, data = await _api_get_with_backoff(client, fetch_url, fetch_params)
                    if not ok:
                        break
                    adsets = data.get("data", [])

                    if not adsets:
                        logger.info(f"✅ No more ad sets at page {page}")
                        break

                    # Store complete ad set objects (id, name, campaign_id)
                    all_adsets.extend(adsets)

                    logger.info(
                        f"✅ Page {page}: Got {len(adsets)} ad sets "
                        f"(total: {len(all_adsets)})"
                    )

                    # Check for next page
                    paging = data.get("paging", {})
                    next_url = paging.get("next")

                    if not next_url:
                        logger.info(f"✅ Reached last page at page {page}")
                        break

                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.1)

                logger.info(
                    f"✅ STAGE 1 COMPLETE: Got {len(all_adsets)} total ad sets"
                )
                return all_adsets

        except Exception as e:
            logger.error(f"❌ Error in stage 1: {str(e)}", exc_info=True)
            return all_adsets

    async def stage2_get_adset_insights(
            self,
            adset_id: str,
            adset_name: str = None,
            campaign_id: str = None
    ) -> List[Dict]:
        """
        STAGE 2: Get all historical insights for a specific ad set.

        Uses pagination to fetch complete history with comprehensive metrics.

        Args:
            adset_id: Ad Set ID
            adset_name: Ad Set name (for logging)
            campaign_id: Campaign ID (for reference)

        Returns:
            List of daily insight records
        """
        all_insights = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"{self.base_url}/{adset_id}/insights"
                params = {
                    "access_token": self.access_token,
                    "date_preset": "maximum",  # All historical data
                    "time_increment": "1",  # Daily breakdown
                    "limit": 100,  # Max per page
                    "fields": (
                        "campaign_id,"
                        "campaign_name,"
                        "clicks,"
                        "spend,"
                        "social_spend,"
                        "account_currency,"
                        "conversion_rate_ranking,"
                        "conversion_values,"
                        "conversions,"
                        "cpc,"
                        "cpm,"
                        "cpp,"
                        "ctr,"
                        "impressions,"
                        "reach,"
                        "results,"
                        "adset_name,"
                        "adset_id"
                    )
                }

                page = 0
                next_url = None

                while True:
                    page += 1

                    # Fetch page
                    if page == 1:
                        response = await client.get(url, params=params)
                    else:
                        response = await client.get(next_url)

                    if response.status_code != 200:
                        logger.error(
                            f"❌ Failed to fetch insights for ad set {adset_id}: "
                            f"{response.status_code} - {response.text}"
                        )
                        break

                    data = response.json()
                    insights = data.get("data", [])

                    if not insights:
                        logger.info(
                            f"  ✅ No more insights at page {page} for ad set {adset_id}"
                        )
                        break

                    all_insights.extend(insights)

                    logger.info(
                        f"  📊 Ad Set {adset_id} ({adset_name}): Got {len(insights)} insights "
                        f"(page {page}, total: {len(all_insights)})"
                    )

                    # Check for next page
                    paging = data.get("paging", {})
                    next_url = paging.get("next")

                    if not next_url:
                        logger.info(
                            f"  ✅ Reached last page at page {page} for ad set {adset_id}"
                        )
                        break

                    # Small delay
                    await asyncio.sleep(0.1)

                logger.info(
                    f"  ✅ Ad Set {adset_id} ({adset_name}): COMPLETE - {len(all_insights)} total insights"
                )
                return all_insights

        except Exception as e:
            logger.error(
                f"❌ Error fetching insights for ad set {adset_id}: {str(e)}",
                exc_info=True
            )
            return all_insights

    async def fetch_all_adset_insights(
            self,
            ad_account_id: str,
            max_concurrent_adsets: int = 3
    ) -> Tuple[int, List[Dict]]:
        """
        MAIN FUNCTION: Fetch all ad set insights using staged approach.

        Stage 1: Get all ad set IDs with names and campaign associations
        Stage 2: Fetch insights for each ad set (with controlled concurrency)

        Args:
            ad_account_id: Meta ad account ID
            max_concurrent_adsets: Max ad sets to fetch in parallel (default: 3)

        Returns:
            (total_insights_count, all_insights_list)
        """
        start_time = datetime.utcnow()

        logger.info(
            f"🚀 Starting staged Facebook ad set insights fetch for {ad_account_id}"
        )

        # ============================================
        # STAGE 1: Get all ad set IDs
        # ============================================
        logger.info("📋 STAGE 1: Fetching all ad set IDs...")
        adsets = await self.stage1_get_all_adset_ids(ad_account_id)

        if not adsets:
            logger.warning("⚠️ No ad sets found")
            return 0, []

        logger.info(f"✅ STAGE 1 COMPLETE: {len(adsets)} ad sets to process")

        # ============================================
        # STAGE 2: Fetch insights for each ad set (with concurrency control)
        # ============================================
        logger.info(
            f"📋 STAGE 2: Fetching insights from {len(adsets)} ad sets..."
        )

        all_insights = []

        # Process ad sets in batches to control concurrency
        for i in range(0, len(adsets), max_concurrent_adsets):
            batch = adsets[i:i + max_concurrent_adsets]
            batch_num = (i // max_concurrent_adsets) + 1
            total_batches = (
                                    len(adsets) + max_concurrent_adsets - 1
                            ) // max_concurrent_adsets

            logger.info(
                f"🔄 Processing batch {batch_num}/{total_batches} "
                f"({len(batch)} ad sets)"
            )

            # Fetch insights for this batch in parallel
            tasks = [
                self.stage2_get_adset_insights(
                    adset["id"],
                    adset.get("name", "Unknown"),
                    adset.get("campaign_id")
                )
                for adset in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            batch_insights_count = 0
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(
                        f"❌ Error in batch {batch_num}, ad set {j}: {result}"
                    )
                else:
                    # Add ad set metadata to each insight for tracking
                    adset = batch[j]
                    for insight in result:
                        # Ensure ad set metadata is present
                        if "adset_id" not in insight:
                            insight["adset_id"] = adset["id"]
                        if "adset_name" not in insight:
                            insight["adset_name"] = adset.get("name", "Unknown")
                        # Add campaign_id from stage 1 if not in insight
                        if "campaign_id" not in insight and adset.get("campaign_id"):
                            insight["campaign_id"] = adset["campaign_id"]

                    all_insights.extend(result)
                    batch_insights_count += len(result)

            logger.info(
                f"✅ Batch {batch_num}/{total_batches} complete: "
                f"{batch_insights_count} insights"
            )

            # Small delay between batches
            if i + max_concurrent_adsets < len(adsets):
                await asyncio.sleep(0.5)

        elapsed = (datetime.utcnow() - start_time).total_seconds()

        logger.info(
            f"✅ STAGE 2 COMPLETE: {len(all_insights)} total insights "
            f"in {elapsed:.2f}s"
        )

        return len(all_insights), all_insights


async def save_adset_insights_to_db(
        insights: List[Dict],
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client
) -> int:
    """
    Save ad set insights to MongoDB.

    Args:
        insights: List of insight records
        user_id: User ID
        ad_account_id: Meta ad account ID
        client_group_id: Client group ID
        client_group_name: Client group name
        mongo_client: MongoDB client

    Returns:
        Number of insights saved
    """
    try:
        import os
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_adset_insights"]

        if not insights:
            logger.info("No ad set insights to save")
            return 0

        # Prepare documents for insertion
        insight_docs = []
        for insight in insights:
            insight_docs.append({
                "user_id": user_id,
                "ad_account_id": ad_account_id,
                "client_group_id": client_group_id,
                "client_group_name": client_group_name,
                "adset_id": insight.get("adset_id"),
                "adset_name": insight.get("adset_name"),
                "campaign_id": insight.get("campaign_id"),
                "campaign_name": insight.get("campaign_name"),
                "date_start": insight.get("date_start"),
                "date_stop": insight.get("date_stop"),
                "insight_data": insight,
                "created_at": datetime.now(),
                "updated_at": datetime.now()
            })

        # Insert in batches to avoid memory issues
        batch_size = 500
        total_saved = 0

        for i in range(0, len(insight_docs), batch_size):
            batch = insight_docs[i:i + batch_size]

            try:
                # Use insert_many with ordered=False to continue on duplicates
                result = await insights_collection.insert_many(
                    batch,
                    ordered=False
                )
                total_saved += len(result.inserted_ids)

                logger.info(
                    f"💾 Saved batch {i // batch_size + 1}: "
                    f"{len(result.inserted_ids)} ad set insights"
                )

            except Exception as e:
                # Continue even if some duplicates exist
                logger.warning(f"Some duplicates in batch: {str(e)}")

        logger.info(f"✅ Saved {total_saved} ad set insights to database")
        return total_saved

    except Exception as e:
        logger.error(f"❌ Error saving ad set insights to database: {str(e)}", exc_info=True)
        raise


async def create_adset_insights_indexes(mongo_client):
    """
    Create optimized indexes for Facebook ad set insights collection.
    """
    try:
        import os
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_adset_insights"]

        # Compound unique index
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("ad_account_id", 1),
                ("adset_id", 1),
                ("date_start", 1)
            ],
            unique=True,
            name="user_account_adset_date_unique"
        )

        # Index for date range queries
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("ad_account_id", 1),
                ("date_start", -1)  # Descending for newest first
            ],
            name="user_account_date_desc"
        )

        # Index for client group queries
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("client_group_id", 1),
                ("date_start", -1)
            ],
            name="user_group_date_desc"
        )

        # Index for campaign-based queries
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("campaign_id", 1),
                ("date_start", -1)
            ],
            name="user_campaign_date_desc"
        )

        # Index for ad set lookups
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("adset_id", 1),
                ("date_start", -1)
            ],
            name="user_adset_date_desc"
        )

        logger.info("✅ Created indexes for facebook_adset_insights")

    except Exception as e:
        logger.error(f"Error creating ad set insights indexes: {e}")


async def fetch_and_cache_adset_insights(
        ad_account_currency: str,
        group_id: str,
        meta_ad_account_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    Fetch Facebook ad set insights and update cache with accurate counts.
    """
    try:
        from integrations.facebook_utils.facebook_adsets import FacebookAdSetFetcher
        from integrations.facebook_utils.facebook import get_facebook_token
        from utils.currency_exchange import CurrencyService

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        client_groups_collection = db["client_groups"]
        insights_collection = db["facebook_adset_insights"]

        # Get the actual client group name
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        logger.info(
            f"🔄 Starting ad set insights fetch for '{client_group_name}' "
            f"(account: {meta_ad_account_id})"
        )

        # Get user's default currency
        try:
            user_currency = await CurrencyService.get_user_currency(user_id)
            logger.info(
                f"💱 User currency: {user_currency}, Ad account currency: {ad_account_currency}"
            )
        except ValueError as e:
            logger.error(f"Failed to get user currency: {e}")
            raise
        except RuntimeError as e:
            logger.error(f"Database error getting user currency: {e}")
            raise

        # Get token
        token = await get_facebook_token(user_id, mongo_client)
        if not token or not token.get("access_token"):
            logger.warning(f"No Facebook token for user {user_id}")
            return

        # Clear existing data if initial load
        if is_initial_load:
            delete_result = await insights_collection.delete_many({
                "user_id": user_id,
                "ad_account_id": meta_ad_account_id,
                "client_group_id": group_id
            })
            logger.info(f"🗑️ Deleted {delete_result.deleted_count} existing ad set insights")

        # Fetch using staged approach
        fetcher = FacebookAdSetFetcher(token["access_token"])
        total, insights = await fetcher.fetch_all_adset_insights(
            meta_ad_account_id,
            max_concurrent_adsets=3
        )

        # ============================================
        # Save to database AND calculate metrics
        # ============================================
        if insights:
            insight_docs = []

            total_spend = 0.0
            total_impressions = 0
            total_clicks = 0
            total_reach = 0
            unique_adsets = set()

            for insight in insights:
                # Add to unique ad sets
                unique_adsets.add(insight.get("adset_id"))

                # Aggregate metrics (with safe type conversion)
                try:
                    total_spend += float(insight.get("spend", 0) or 0)
                except (ValueError, TypeError):
                    pass

                try:
                    total_impressions += int(insight.get("impressions", 0) or 0)
                except (ValueError, TypeError):
                    pass

                try:
                    total_clicks += int(insight.get("clicks", 0) or 0)
                except (ValueError, TypeError):
                    pass

                try:
                    total_reach += int(insight.get("reach", 0) or 0)
                except (ValueError, TypeError):
                    pass

                # Build document
                insight_docs.append({
                    "user_id": user_id,
                    "ad_account_id": meta_ad_account_id,
                    "client_group_id": group_id,
                    "client_group_name": client_group_name,
                    "adset_id": insight.get("adset_id"),
                    "adset_name": insight.get("adset_name"),
                    "campaign_id": insight.get("campaign_id"),
                    "campaign_name": insight.get("campaign_name"),
                    "date_start": insight.get("date_start"),
                    "date_stop": insight.get("date_stop"),
                    "insight_data": insight,
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                })

            # Insert in batches
            batch_size = 500
            total_saved = 0

            for i in range(0, len(insight_docs), batch_size):
                batch = insight_docs[i:i + batch_size]

                try:
                    result = await insights_collection.insert_many(
                        batch,
                        ordered=False
                    )
                    total_saved += len(result.inserted_ids)

                    logger.info(
                        f"💾 Saved batch {i // batch_size + 1}: "
                        f"{len(result.inserted_ids)} ad set insights"
                    )
                except Exception as e:
                    logger.warning(f"Some duplicates in batch: {str(e)}")

            logger.info(
                f"✅ Saved {total_saved} ad set insights for '{client_group_name}'"
            )

            # ============================================
            # ============================================

            # Convert spend to user's currency
            try:
                total_spend_user_currency = CurrencyService.convert(
                    amount=total_spend,
                    from_currency=ad_account_currency,
                    to_currency=user_currency
                )

                logger.info(
                    f"💱 Converted spend: {total_spend:.2f} {ad_account_currency} ➡️ "
                    f"{total_spend_user_currency:.2f} {user_currency}"
                )
            except ValueError as e:
                logger.error(f"Currency conversion failed: {e}")
                # Fall back to original currency if conversion fails
                total_spend_user_currency = total_spend
                user_currency = ad_account_currency
                logger.warning(
                    f"⚠️ Using original currency {ad_account_currency} due to conversion error"
                )

            # Calculate metrics using converted spend
            avg_cpm = (total_spend_user_currency / total_impressions * 1000) if total_impressions > 0 else 0
            avg_cpc = (total_spend_user_currency / total_clicks) if total_clicks > 0 else 0
            avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0

            await client_groups_collection.update_one(
                {"id": group_id},
                {
                    "$set": {
                        "facebook_cache.total_adsets": len(unique_adsets),
                        "facebook_cache.total_adset_insights": total_saved,
                        "facebook_cache.metrics.adset_insights": {
                            "total_spend": round(total_spend_user_currency, 2),
                            "total_impressions": total_impressions,
                            "total_clicks": total_clicks,
                            "total_reach": total_reach,
                            "avg_cpm": round(avg_cpm, 2),
                            "avg_cpc": round(avg_cpc, 2),
                            "avg_ctr": round(avg_ctr, 2),
                            "currency": user_currency,
                            "original_currency": ad_account_currency
                        },
                        "last_adset_insights_refresh": datetime.utcnow()
                    }
                }
            )

            logger.info(
                f"📊 Updated cache: {len(unique_adsets)} ad sets, "
                f"${total_spend_user_currency:.2f} {user_currency} spend, "
                f"{total_impressions} impressions"
            )

        else:
            logger.info(f"No ad set insights found for '{client_group_name}'")

            # Still update cache to show 0
            await client_groups_collection.update_one(
                {"id": group_id},
                {
                    "$set": {
                        "facebook_cache.total_adsets": 0,
                        "facebook_cache.total_adset_insights": 0,
                        "last_adset_insights_refresh": datetime.utcnow()
                    }
                }
            )

    except Exception as e:
        logger.error(f"Error fetching ad set insights: {e}", exc_info=True)
        raise