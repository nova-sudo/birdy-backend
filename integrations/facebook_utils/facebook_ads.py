"""
Facebook Ads Data Fetcher - Staged Approach

This module implements a two-stage approach for fetching Facebook ads data:
1. Stage 1: Get all ad IDs for an ad account
2. Stage 2: Fetch historical insights for each ad (with pagination)

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

async def _api_get_with_backoff(client, url, params=None, max_retries=4):
    """
    GET with exponential backoff on Meta rate-limit responses (code 17 / HTTP 429).
    Returns (response_ok: bool, data: dict | None).
    """
    import random
    for attempt in range(max_retries):
        try:
            if params is not None:
                resp = await client.get(url, params=params)
            else:
                resp = await client.get(url)

            if resp.status_code == 200:
                return True, resp.json()

            body = resp.text
            is_rate_limit = resp.status_code == 429 or (
                resp.status_code == 400 and ('"code":17' in body or '"code": 17' in body)
            )

            if is_rate_limit and attempt < max_retries - 1:
                wait = (15 * (2 ** attempt)) + random.uniform(0, 3)
                logger.warning(
                    f"⏳ Rate limited (attempt {attempt+1}/{max_retries}). "
                    f"Waiting {wait:.0f}s before retry..."
                )
                await asyncio.sleep(wait)
                continue

            logger.error(f"❌ API error {resp.status_code}: {body[:300]}")
            return False, None

        except Exception as e:
            logger.error(f"❌ Request error: {e}")
            return False, None

    return False, None


class FacebookAdsFetcher:
    """
    Handles fetching Facebook ads data using a staged approach.
    """

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.facebook.com/v23.0"

    async def stage1_get_all_ad_ids(
            self,
            ad_account_id: str
    ) -> List[Dict]:
        """
        STAGE 1: Get all ad IDs and basic info for an ad account.

        Handles pagination to get complete list.

        Args:
            ad_account_id: Meta ad account ID (e.g., "act_415096360989203")

        Returns:
            List of ad objects with id and name
        """
        all_ads = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"{self.base_url}/{ad_account_id}/ads"
                params = {
                    "access_token": self.access_token,
                    "fields": "name",
                    "limit": 100  # Max per page
                }

                page = 0
                next_url = None

                while True:
                    page += 1
                    logger.info(f"📄 Fetching ad IDs page {page}")

                    # Fetch page with rate-limit backoff
                    fetch_url = url if page == 1 else next_url
                    fetch_params = params if page == 1 else None
                    ok, data = await _api_get_with_backoff(client, fetch_url, fetch_params)
                    if not ok:
                        break
                    ads = data.get("data", [])

                    if not ads:
                        logger.info(f"✅ No more ads at page {page}")
                        break

                    # Store complete ad objects (id, name)
                    all_ads.extend(ads)

                    logger.info(
                        f"✅ Page {page}: Got {len(ads)} ads "
                        f"(total: {len(all_ads)})"
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
                    f"✅ STAGE 1 COMPLETE: Got {len(all_ads)} total ads"
                )
                return all_ads

        except Exception as e:
            logger.error(f"❌ Error in stage 1: {str(e)}", exc_info=True)
            return all_ads

    async def stage2_get_ad_insights(
            self,
            ad_id: str,
            ad_name: str = None
    ) -> List[Dict]:
        """
        STAGE 2: Get all historical insights for a specific ad.

        Uses pagination to fetch complete history with comprehensive metrics.

        Args:
            ad_id: Ad ID
            ad_name: Ad name (for logging)

        Returns:
            List of daily insight records
        """
        all_insights = []

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                url = f"{self.base_url}/{ad_id}/insights"
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
                        "adset_id,"
                        "ad_name,"
                        "ad_id"
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
                            f"❌ Failed to fetch insights for ad {ad_id}: "
                            f"{response.status_code} - {response.text}"
                        )
                        break

                    data = response.json()
                    insights = data.get("data", [])

                    if not insights:
                        logger.info(
                            f"  ✅ No more insights at page {page} for ad {ad_id}"
                        )
                        break

                    all_insights.extend(insights)

                    logger.info(
                        f"  📊 Ad {ad_id} ({ad_name}): Got {len(insights)} insights "
                        f"(page {page}, total: {len(all_insights)})"
                    )

                    # Check for next page
                    paging = data.get("paging", {})
                    next_url = paging.get("next")

                    if not next_url:
                        logger.info(
                            f"  ✅ Reached last page at page {page} for ad {ad_id}"
                        )
                        break

                    # Small delay
                    await asyncio.sleep(0.1)

                logger.info(
                    f"  ✅ Ad {ad_id} ({ad_name}): COMPLETE - {len(all_insights)} total insights"
                )
                return all_insights

        except Exception as e:
            logger.error(
                f"❌ Error fetching insights for ad {ad_id}: {str(e)}",
                exc_info=True
            )
            return all_insights

    async def fetch_all_ad_insights(
            self,
            ad_account_id: str,
            max_concurrent_ads: int = 3
    ) -> Tuple[int, List[Dict]]:
        """
        MAIN FUNCTION: Fetch all ad insights using staged approach.

        Stage 1: Get all ad IDs with names
        Stage 2: Fetch insights for each ad (with controlled concurrency)

        Args:
            ad_account_id: Meta ad account ID
            max_concurrent_ads: Max ads to fetch in parallel (default: 3)

        Returns:
            (total_insights_count, all_insights_list)
        """
        start_time = datetime.utcnow()

        logger.info(
            f"🚀 Starting staged Facebook ad insights fetch for {ad_account_id}"
        )

        # ============================================
        # STAGE 1: Get all ad IDs
        # ============================================
        logger.info("📋 STAGE 1: Fetching all ad IDs...")
        ads = await self.stage1_get_all_ad_ids(ad_account_id)

        if not ads:
            logger.warning("⚠️ No ads found")
            return 0, []

        logger.info(f"✅ STAGE 1 COMPLETE: {len(ads)} ads to process")

        # ============================================
        # STAGE 2: Fetch insights for each ad (with concurrency control)
        # ============================================
        logger.info(
            f"📋 STAGE 2: Fetching insights from {len(ads)} ads..."
        )

        all_insights = []

        # Process ads in batches to control concurrency
        for i in range(0, len(ads), max_concurrent_ads):
            batch = ads[i:i + max_concurrent_ads]
            batch_num = (i // max_concurrent_ads) + 1
            total_batches = (
                                    len(ads) + max_concurrent_ads - 1
                            ) // max_concurrent_ads

            logger.info(
                f"🔄 Processing batch {batch_num}/{total_batches} "
                f"({len(batch)} ads)"
            )

            # Fetch insights for this batch in parallel
            tasks = [
                self.stage2_get_ad_insights(
                    ad["id"],
                    ad.get("name", "Unknown")
                )
                for ad in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            batch_insights_count = 0
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(
                        f"❌ Error in batch {batch_num}, ad {j}: {result}"
                    )
                else:
                    # Add ad metadata to each insight for tracking
                    ad = batch[j]
                    for insight in result:
                        # Ensure ad metadata is present
                        if "ad_id" not in insight:
                            insight["ad_id"] = ad["id"]
                        if "ad_name" not in insight:
                            insight["ad_name"] = ad.get("name", "Unknown")

                    all_insights.extend(result)
                    batch_insights_count += len(result)

            logger.info(
                f"✅ Batch {batch_num}/{total_batches} complete: "
                f"{batch_insights_count} insights"
            )

            # Small delay between batches
            if i + max_concurrent_ads < len(ads):
                await asyncio.sleep(0.5)

        elapsed = (datetime.utcnow() - start_time).total_seconds()

        logger.info(
            f"✅ STAGE 2 COMPLETE: {len(all_insights)} total insights "
            f"in {elapsed:.2f}s"
        )

        return len(all_insights), all_insights


async def save_ad_insights_to_db(
        insights: List[Dict],
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client
) -> int:
    """
    Save ad insights to MongoDB.

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
        insights_collection = db["facebook_ad_insights"]

        if not insights:
            logger.info("No ad insights to save")
            return 0

        # Prepare documents for insertion
        insight_docs = []
        for insight in insights:
            insight_docs.append({
                "user_id": user_id,
                "ad_account_id": ad_account_id,
                "client_group_id": client_group_id,
                "client_group_name": client_group_name,
                "ad_id": insight.get("ad_id"),
                "ad_name": insight.get("ad_name"),
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
                    f"{len(result.inserted_ids)} ad insights"
                )

            except Exception as e:
                # Continue even if some duplicates exist
                logger.warning(f"Some duplicates in batch: {str(e)}")

        logger.info(f"✅ Saved {total_saved} ad insights to database")
        return total_saved

    except Exception as e:
        logger.error(f"❌ Error saving ad insights to database: {str(e)}", exc_info=True)
        raise


async def create_ad_insights_indexes(mongo_client):
    """
    Create optimized indexes for Facebook ad insights collection.
    """
    try:
        import os
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_ad_insights"]

        # Compound unique index
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("ad_account_id", 1),
                ("ad_id", 1),
                ("date_start", 1)
            ],
            unique=True,
            name="user_account_ad_date_unique"
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

        # Index for adset-based queries
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("adset_id", 1),
                ("date_start", -1)
            ],
            name="user_adset_date_desc"
        )

        # Index for ad lookups
        await insights_collection.create_index(
            [
                ("user_id", 1),
                ("ad_id", 1),
                ("date_start", -1)
            ],
            name="user_ad_date_desc"
        )

        logger.info("✅ Created indexes for facebook_ad_insights")

    except Exception as e:
        logger.error(f"Error creating ad insights indexes: {e}")


async def fetch_and_cache_ad_insights(
        ad_account_currency: str,
        group_id: str,
        meta_ad_account_id: str,
        user_id: str,
        mongo_client,
        is_initial_load: bool = True
):
    """
    Fetch Facebook ad insights and update cache with accurate counts.
    """
    try:
        from integrations.facebook_utils.facebook_ads import FacebookAdsFetcher
        from integrations.facebook_utils.facebook import get_facebook_token
        from utils.currency_exchange import CurrencyService

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        client_groups_collection = db["client_groups"]
        insights_collection = db["facebook_ad_insights"]

        # Get the actual client group name
        client_group = await client_groups_collection.find_one({"id": group_id})
        client_group_name = client_group.get("name") if client_group else "Unknown Group"

        logger.info(
            f"🔄 Starting ad insights fetch for '{client_group_name}' "
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
            logger.info(f"🗑️ Deleted {delete_result.deleted_count} existing ad insights")

        # Fetch using staged approach
        fetcher = FacebookAdsFetcher(token["access_token"])
        total, insights = await fetcher.fetch_all_ad_insights(
            meta_ad_account_id,
            max_concurrent_ads=3
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
            total_results = 0
            unique_ads = set()

            for insight in insights:
                # Add to unique ads
                unique_ads.add(insight.get("ad_id"))

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

                try:
                    total_results += int(insight.get("results", 0) or 0)
                except (ValueError, TypeError):
                    pass

                # Build document
                insight_docs.append({
                    "user_id": user_id,
                    "ad_account_id": meta_ad_account_id,
                    "client_group_id": group_id,
                    "client_group_name": client_group_name,
                    "ad_id": insight.get("ad_id"),
                    "ad_name": insight.get("ad_name"),
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
                        f"{len(result.inserted_ids)} ad insights"
                    )
                except Exception as e:
                    logger.warning(f"Some duplicates in batch: {str(e)}")

            logger.info(
                f"✅ Saved {total_saved} ad insights for '{client_group_name}'"
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
                        "facebook_cache.total_ads": len(unique_ads),
                        "facebook_cache.total_ad_insights": total_saved,
                        "facebook_cache.metrics.ad_insights": {
                            "total_spend": round(total_spend_user_currency, 2),
                            "total_impressions": total_impressions,
                            "total_clicks": total_clicks,
                            "total_reach": total_reach,
                            "total_results": total_results,
                            "avg_cpm": round(avg_cpm, 2),
                            "avg_cpc": round(avg_cpc, 2),
                            "avg_ctr": round(avg_ctr, 2),
                            "currency": user_currency,
                            "original_currency": ad_account_currency
                        },
                        "last_ad_insights_refresh": datetime.utcnow()
                    }
                }
            )

            logger.info(
                f"📊 Updated cache: {len(unique_ads)} ads, "
                f"${total_spend_user_currency:.2f} {user_currency} spend, "
                f"{total_impressions} impressions"
            )

        else:
            logger.info(f"No ad insights found for '{client_group_name}'")

            # Still update cache to show 0
            await client_groups_collection.update_one(
                {"id": group_id},
                {
                    "$set": {
                        "facebook_cache.total_ads": 0,
                        "facebook_cache.total_ad_insights": 0,
                        "last_ad_insights_refresh": datetime.utcnow()
                    }
                }
            )

    except Exception as e:
        logger.error(f"Error fetching ad insights: {e}", exc_info=True)
        raise