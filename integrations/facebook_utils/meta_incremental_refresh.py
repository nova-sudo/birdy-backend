"""
Meta Data Incremental Refresh - TODAY Strategy

Implements smart incremental refresh for Meta data:
1. Leads: Fetch date_preset=today, stop at last known lead
2. Campaigns/Adsets/Ads: Update only today's insight records

This avoids refetching historical data on every refresh.
"""

import asyncio
import logging
from datetime import datetime, date
from typing import List, Dict, Tuple, Optional
import httpx
import os

logger = logging.getLogger(__name__)


# ============================================
# LEADS INCREMENTAL REFRESH
# ============================================

async def fetch_todays_facebook_leads_incremental(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client,
        max_concurrent_ads: int = 5
) -> Tuple[int, List[dict]]:
    """
    INCREMENTAL: Fetch only TODAY's leads and stop at last known lead.

    Strategy:
    1. Get last known lead from database (newest by created_time)
    2. Fetch ads with date_preset=today
    3. For each ad, fetch leads until hitting last known lead

    Returns:
        (new_leads_count, new_leads_list)
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        leads_collection = db["facebook_leads"]

        # ============================================
        # STEP 1: Get last known lead
        # ============================================
        last_known_lead = await leads_collection.find_one(
            {
                "user_id": user_id,
                "ad_account_id": ad_account_id,
                "client_group_id": client_group_id
            },
            sort=[("lead_data.created_time", -1)]
        )

        last_known_lead_id = None
        last_known_created_time = None

        if last_known_lead:
            last_known_lead_id = last_known_lead.get("lead_id")
            last_known_created_time = last_known_lead.get("lead_data", {}).get("created_time")

            logger.info(
                f"🔍 Last known lead: {last_known_lead_id} "
                f"from {last_known_created_time}"
            )
        else:
            logger.info("📋 No previous leads found, fetching all today's leads")

        # ============================================
        # STEP 2: Get all ad IDs (only active ads from today)
        # ============================================
        logger.info("📋 Fetching active ad IDs for today...")
        ad_ids = await _stage1_get_todays_ad_ids(ad_account_id, access_token)

        if not ad_ids:
            logger.info("✅ No active ads found for today")
            return 0, []

        logger.info(f"✅ Found {len(ad_ids)} active ads for today")

        # ============================================
        # STEP 3: Fetch leads from ads (stop at last known)
        # ============================================
        all_new_leads = []
        should_continue = True

        # Process ads in batches
        for i in range(0, len(ad_ids), max_concurrent_ads):
            if not should_continue:
                logger.info("⚡ Stopped early - found last known lead")
                break

            batch = ad_ids[i:i + max_concurrent_ads]
            batch_num = (i // max_concurrent_ads) + 1
            total_batches = (len(ad_ids) + max_concurrent_ads - 1) // max_concurrent_ads

            logger.info(
                f"🔄 Processing batch {batch_num}/{total_batches} "
                f"({len(batch)} ads)"
            )

            # Fetch leads for this batch in parallel
            tasks = [
                _stage2_get_todays_leads_for_ad(
                    ad_id,
                    access_token,
                    last_known_lead_id,
                    last_known_created_time
                )
                for ad_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            # Collect results
            for j, result in enumerate(batch_results):
                if isinstance(result, Exception):
                    logger.error(f"❌ Error in batch {batch_num}, ad {j}: {result}")
                else:
                    new_leads, hit_last_known = result

                    if new_leads:
                        all_new_leads.extend(new_leads)

                    if hit_last_known:
                        logger.info("⚡ Found last known lead, stopping fetch")
                        should_continue = False
                        break

            logger.info(
                f"✅ Batch {batch_num}/{total_batches} complete: "
                f"{sum(len(r[0]) for r in batch_results if not isinstance(r, Exception))} new leads"
            )

            # Small delay between batches
            if should_continue and i + max_concurrent_ads < len(ad_ids):
                await asyncio.sleep(0.5)

        # ============================================
        # 🔥 FALLBACK: If no new leads for today, check last_7d
        # ============================================
        if not all_new_leads:
            logger.warning(
                f"⚠️ No new leads found for today, "
                f"checking last_7d for gaps..."
            )

            # Fetch last 7 days with gap detection
            gap_leads = await _fetch_last_7d_leads_with_gap_detection(
                ad_account_id,
                access_token,
                user_id,
                client_group_id,
                mongo_client,
                max_concurrent_ads
            )

            if gap_leads:
                logger.info(
                    f"✅ Fallback successful: Found {len(gap_leads)} missing leads "
                    f"in last 7 days"
                )
                all_new_leads = gap_leads
            else:
                logger.info("✅ No lead gaps found in last 7 days")

        # ============================================
        # STEP 4: Normalize leads
        # ============================================
        normalized_leads = []

        for lead in all_new_leads:
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
                "platform": lead.get("platform", ""),
                "created_time": lead.get("created_time", ""),
                "full_name": field_data.get("full_name", ""),
                "email": field_data.get("email", ""),
                "phone_number": field_data.get("phone_number", ""),
                "field_data": field_data,
                "client_group_id": client_group_id,
                "client_group_name": client_group_name,
                "user_id": user_id,
                "ad_account_id": ad_account_id,
            }

            normalized_leads.append(normalized_lead)

        logger.info(
            f"✅ INCREMENTAL COMPLETE: {len(normalized_leads)} new leads fetched"
        )

        return len(normalized_leads), normalized_leads

    except Exception as e:
        logger.error(f"❌ Error in incremental leads fetch: {str(e)}", exc_info=True)
        return 0, []


async def _stage1_get_todays_ad_ids(
        ad_account_id: str,
        access_token: str
) -> List[str]:
    """Get ad IDs that have activity today"""
    all_ad_ids = []

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            url = f"https://graph.facebook.com/v23.0/{ad_account_id}/ads"
            params = {
                "fields": "id",
                "access_token": access_token,
                "limit": 500,
                "date_preset": "today"  # 🔥 Only today's active ads
            }

            page = 0
            next_url = None

            while True:
                page += 1

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
                    break

                ad_ids = [ad["id"] for ad in ads]
                all_ad_ids.extend(ad_ids)

                logger.info(f"  📄 Page {page}: {len(ad_ids)} ads")

                paging = data.get("paging", {})
                next_url = paging.get("next")

                if not next_url:
                    break

                await asyncio.sleep(0.1)

            return all_ad_ids

    except Exception as e:
        logger.error(f"❌ Error fetching today's ad IDs: {str(e)}", exc_info=True)
        return all_ad_ids


async def _stage2_get_todays_leads_for_ad(
        ad_id: str,
        access_token: str,
        last_known_lead_id: Optional[str],
        last_known_created_time: Optional[str]
) -> Tuple[List[dict], bool]:
    """
    Fetch today's leads for a specific ad.

    Returns:
        (new_leads_list, hit_last_known_lead)
    """
    new_leads = []
    hit_last_known = False

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            url = f"https://graph.facebook.com/v23.0/{ad_id}"
            params = {
                "fields": "leads,name",
                "date_preset": "today",  # 🔥 Only today's leads
                "access_token": access_token
            }

            # Get ad info and first page of leads
            response = await client.get(url, params=params)

            if response.status_code != 200:
                logger.error(
                    f"❌ Failed to fetch ad {ad_id}: "
                    f"{response.status_code} - {response.text}"
                )
                return [], False

            ad_data = response.json()
            ad_name = ad_data.get("name", "")
            leads_data = ad_data.get("leads", {})

            # Process first page
            first_leads = leads_data.get("data", [])

            for lead in first_leads:
                lead_id = lead.get("id")
                created_time = lead.get("created_time")

                # Check if we hit the last known lead
                if last_known_lead_id and lead_id == last_known_lead_id:
                    logger.info(f"  ⚡ Hit last known lead {lead_id} in ad {ad_id}")
                    hit_last_known = True
                    break

                # Also check by timestamp
                if last_known_created_time and created_time:
                    try:
                        lead_time = datetime.fromisoformat(
                            created_time.replace('Z', '+00:00')
                        )
                        last_time = datetime.fromisoformat(
                            last_known_created_time.replace('Z', '+00:00')
                        )

                        if lead_time <= last_time:
                            logger.info(f"  ⚡ Hit older timestamp in ad {ad_id}")
                            hit_last_known = True
                            break
                    except Exception as e:
                        logger.debug(f"Date comparison error: {e}")

                # Lead is new, add it
                lead["ad_name"] = ad_name
                new_leads.append(lead)

            if hit_last_known:
                return new_leads, True

            logger.info(f"  📊 Ad {ad_id}: {len(first_leads)} leads (page 1)")

            # Get pagination URL
            paging = leads_data.get("paging", {})
            next_url = paging.get("next")
            page = 1

            # Paginate through remaining leads
            while next_url and not hit_last_known:
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
                        break

                    # Check each lead
                    for lead in more_leads:
                        lead_id = lead.get("id")
                        created_time = lead.get("created_time")

                        # Check if we hit the last known lead
                        if last_known_lead_id and lead_id == last_known_lead_id:
                            logger.info(f"  ⚡ Hit last known lead {lead_id}")
                            hit_last_known = True
                            break

                        # Also check by timestamp
                        if last_known_created_time and created_time:
                            try:
                                lead_time = datetime.fromisoformat(
                                    created_time.replace('Z', '+00:00')
                                )
                                last_time = datetime.fromisoformat(
                                    last_known_created_time.replace('Z', '+00:00')
                                )

                                if lead_time <= last_time:
                                    hit_last_known = True
                                    break
                            except Exception:
                                pass

                        lead["ad_name"] = ad_name
                        new_leads.append(lead)

                    if hit_last_known:
                        break

                    logger.info(f"  📊 Ad {ad_id}: {len(more_leads)} leads (page {page})")

                    # Get next page
                    next_paging = page_data.get("paging", {})
                    next_url = next_paging.get("next")

                    await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error(f"❌ Error fetching page {page}: {str(e)}")
                    break

            logger.info(f"  ✅ Ad {ad_id}: {len(new_leads)} new leads")
            return new_leads, hit_last_known

    except Exception as e:
        logger.error(f"❌ Error fetching leads for ad {ad_id}: {str(e)}", exc_info=True)
        return [], False


# ============================================
# GAP DETECTION HELPER FOR LEADS
# ============================================

async def _fetch_last_7d_leads_with_gap_detection(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        mongo_client,
        max_concurrent_ads: int = 5
) -> List[dict]:
    """
    FALLBACK: Fetch last 7 days of leads and identify gaps.

    Returns only leads that are missing from the database.
    """
    try:
        from datetime import timedelta

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        leads_collection = db["facebook_leads"]

        logger.info("📋 Fetching last_7d leads from API...")

        # Get ad IDs for last 7 days
        ad_ids = await _get_ad_ids_date_range(ad_account_id, access_token, "last_7d")

        if not ad_ids:
            logger.info("No active ads in last 7 days")
            return []

        logger.info(f"Found {len(ad_ids)} active ads in last 7 days")

        # Fetch leads from all ads
        all_api_leads = []

        for i in range(0, len(ad_ids), max_concurrent_ads):
            batch = ad_ids[i:i + max_concurrent_ads]

            tasks = [
                _fetch_leads_for_ad_date_range(ad_id, access_token, "last_7d")
                for ad_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_api_leads.extend(result)

            await asyncio.sleep(0.5)

        if not all_api_leads:
            logger.info("No leads in last 7 days from API")
            return []

        logger.info(f"📊 API returned {len(all_api_leads)} leads for last 7 days")

        # Get last 7 days from database
        seven_days_ago = datetime.now() - timedelta(days=7)

        existing_records = await leads_collection.find({
            "user_id": user_id,
            "ad_account_id": ad_account_id,
            "client_group_id": client_group_id,
            "created_at": {"$gte": seven_days_ago}
        }).to_list(None)

        logger.info(f"📊 Database has {len(existing_records)} leads for last 7 days")

        # Find gaps
        existing_lead_ids = {
            record["lead_id"]
            for record in existing_records
        }

        missing_leads = []
        for lead in all_api_leads:
            lead_id = lead.get("id")

            if lead_id not in existing_lead_ids:
                missing_leads.append(lead)

        if missing_leads:
            logger.warning(
                f"⚠️ LEAD GAPS DETECTED: {len(missing_leads)} missing leads"
            )
        else:
            logger.info("✅ No lead gaps found")

        return missing_leads

    except Exception as e:
        logger.error(f"Error in lead gap detection: {str(e)}", exc_info=True)
        return []


async def _get_ad_ids_date_range(
        ad_account_id: str,
        access_token: str,
        date_preset: str = "last_7d"
) -> List[str]:
    """Get ad IDs that have activity in date range"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_account_id}/ads",
                params={
                    "fields": "id",
                    "access_token": access_token,
                    "limit": 500,
                    "date_preset": date_preset
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [ad["id"] for ad in data.get("data", [])]

            return []

    except Exception as e:
        logger.error(f"Error fetching ad IDs: {str(e)}")
        return []


async def _fetch_leads_for_ad_date_range(
        ad_id: str,
        access_token: str,
        date_preset: str = "last_7d"
) -> List[dict]:
    """Fetch leads for an ad in a date range"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_id}",
                params={
                    "fields": "leads,name",
                    "date_preset": date_preset,
                    "access_token": access_token
                }
            )

            if response.status_code == 200:
                ad_data = response.json()
                ad_name = ad_data.get("name", "")
                leads_data = ad_data.get("leads", {})
                leads = leads_data.get("data", [])

                # Add ad name to each lead
                for lead in leads:
                    lead["ad_name"] = ad_name

                return leads

            return []

    except Exception as e:
        logger.error(f"Error fetching leads for ad {ad_id}: {str(e)}")
        return []


# ============================================
# CAMPAIGN/ADSET/AD INSIGHTS INCREMENTAL REFRESH
# ============================================

async def update_todays_campaign_insights(
         ad_account_id: str,
         access_token: str,
         user_id: str,
         client_group_id: str,
         client_group_name: str,
         mongo_client,
         ad_account_currency: str
 ) -> int:
    """
    INCREMENTAL: Update only TODAY's campaign insight records.

    Strategy:
    1. Get all campaign IDs
    2. Fetch insights with date_preset=today (single day)

    Returns:
        Number of insights updated
    """
    try:
        from utils.currency_exchange import CurrencyService

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_campaign_insights"]

        today_str = date.today().isoformat()

        logger.info(f"🔄 Updating today's campaign insights ({today_str})")

        # ✅ GET USER CURRENCY WITH FALLBACK
        try:
            user_currency = await CurrencyService.get_user_currency(user_id)
            logger.info(
                f"💱 User currency: {user_currency}, "
                f"Ad account currency: {ad_account_currency}"
            )
        except (ValueError, RuntimeError) as e:
            logger.warning(
                f"⚠️ Could not get user currency: {e}. "
                f"Using ad account currency {ad_account_currency} as fallback."
            )
            user_currency = ad_account_currency

        # ============================================
        # STEP 1: Get all campaign IDs
        # ============================================
        campaign_ids = await _get_all_campaign_ids(ad_account_id, access_token)

        if not campaign_ids:
            logger.info("✅ No campaigns found")
            return 0

        logger.info(f"✅ Found {len(campaign_ids)} campaigns")

        # ============================================
        # STEP 2: Fetch today's insights for each campaign
        # ============================================
        all_insights = []

        # Process campaigns in batches
        max_concurrent = 3
        for i in range(0, len(campaign_ids), max_concurrent):
            batch = campaign_ids[i:i + max_concurrent]

            tasks = [
                _fetch_todays_campaign_insights(campaign_id, access_token)
                for campaign_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_insights.extend(result)

            await asyncio.sleep(0.5)

        # ============================================
        # 🔥 FALLBACK: If no data for today, fetch last_7d
        # ============================================
        if not all_insights:
            logger.warning(
                f"⚠️ No insights found for today ({today_str}), "
                f"fetching last_7d to check for gaps..."
            )

            all_insights = await _fetch_last_7d_campaign_insights_with_gap_detection(
                campaign_ids,
                access_token,
                user_id,
                ad_account_id,
                client_group_id,
                mongo_client
            )

            if not all_insights:
                logger.info("✅ No insights in last 7 days either")
                return 0
            else:
                logger.info(
                    f"✅ Fallback successful: Found {len(all_insights)} insights "
                    f"in last 7 days"
                )

        # ============================================
        # ✅ CONVERT CURRENCIES BEFORE SAVING
        # ============================================
        if all_insights and user_currency != ad_account_currency:
            converted_count = 0
            total_original_spend = 0.0
            total_converted_spend = 0.0

            # Fields to convert
            currency_fields = ['spend', 'cpc', 'cpm', 'cpp']

            for insight in all_insights:
                converted_fields = {}
                conversion_failed = False
            for field in currency_fields:
                if field in insight and insight[field] is not None:
                    try:
                        original_value = float(insight[field])
                        converted_value = CurrencyService.convert(
                        amount = original_value,
                        from_currency = ad_account_currency,
                        to_currency = user_currency,
                        )
                        converted_fields[field] = converted_value
                        converted_fields[f"{field}_original"] = original_value
                    except (ValueError, TypeError) as e:\
                        logger.warning(f"Failed to convert {field}: {e}")
                    conversion_failed = True
                    break
                if conversion_failed:
                    insight["currency"] = ad_account_currency
                    insight["original_currency"] = ad_account_currency
                    continue

            insight.update(converted_fields)
            insight["currency"] = user_currency
            insight["original_currency"] = ad_account_currency

            if total_original_spend > 0:
                logger.info(
                    f"💱 Converted {len(all_insights)} campaign insights: "
                    f"${total_original_spend:.2f} {ad_account_currency} → "
                    f"${total_converted_spend:.2f} {user_currency}"
                )
        elif all_insights:
            # Same currency, just add metadata
            for insight in all_insights:
                insight['currency'] = user_currency
                insight['original_currency'] = ad_account_currency
            logger.info(f"💱 Same currency ({user_currency}), no conversion needed")

        # ============================================
        # STEP 3: Upsert today's records
        # ============================================
        from pymongo import UpdateOne

        bulk_operations = []

        for insight in all_insights:
            bulk_operations.append(
                UpdateOne(
                    {
                        "user_id": user_id,
                        "ad_account_id": ad_account_id,
                        "campaign_id": insight.get("campaign_id"),
                        "date_start": insight.get("date_start")
                    },
                    {
                        "$set": {
                            "client_group_id": client_group_id,
                            "client_group_name": client_group_name,
                            "insight_data": insight,
                            "currency": insight.get('currency'),  # ✅ ADDED
                            "original_currency": insight.get('original_currency'),  # ✅ ADDED
                            "updated_at": datetime.now()
                        },
                        "$setOnInsert": {
                            "created_at": datetime.now()
                        }
                    },
                    upsert=True
                )
            )

        if bulk_operations:
            result = await insights_collection.bulk_write(bulk_operations, ordered=False)

            total_updated = result.upserted_count + result.modified_count

            logger.info(
                f"✅ Updated {total_updated} campaign insights for {today_str} in {user_currency} "
                f"({result.upserted_count} new, {result.modified_count} updated)"
            )

            return total_updated

        return 0

    except Exception as e:
        logger.error(f"❌ Error updating today's campaign insights: {str(e)}", exc_info=True)
        return 0

async def _get_all_campaign_ids(ad_account_id: str, access_token: str) -> List[str]:
    """Get all campaign IDs (light query, no pagination needed for most accounts)"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_account_id}/campaigns",
                params={
                    "fields": "id",
                    "access_token": access_token,
                    "limit": 500
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [campaign["id"] for campaign in data.get("data", [])]

            return []

    except Exception as e:
        logger.error(f"Error fetching campaign IDs: {str(e)}")
        return []


async def _fetch_todays_campaign_insights(
        campaign_id: str,
        access_token: str
) -> List[Dict]:
    """Fetch today's insights for a single campaign"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{campaign_id}/insights",
                params={
                    "date_preset": "today",  # 🔥 Only today
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                # Add campaign_id to each insight
                for insight in insights:
                    insight["campaign_id"] = campaign_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for campaign {campaign_id}: {str(e)}")
        return []


async def _fetch_last_7d_campaign_insights_with_gap_detection(
        campaign_ids: List[str],
        access_token: str,
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        mongo_client
) -> List[Dict]:
    """
    FALLBACK: Fetch last 7 days and identify missing records.

    Compares API data with database to find gaps.
    Only returns insights that are missing from database.
    """
    try:
        from datetime import timedelta

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_campaign_insights"]

        # ============================================
        # STEP 1: Get last 7 days of data from API
        # ============================================
        logger.info("📋 Fetching last_7d from API...")

        all_api_insights = []

        max_concurrent = 3
        for i in range(0, len(campaign_ids), max_concurrent):
            batch = campaign_ids[i:i + max_concurrent]

            tasks = [
                _fetch_campaign_insights_date_range(campaign_id, access_token, "last_7d")
                for campaign_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_api_insights.extend(result)

            await asyncio.sleep(0.5)

        if not all_api_insights:
            logger.info("No insights in last 7 days from API")
            return []

        logger.info(f"📊 API returned {len(all_api_insights)} insights for last 7 days")

        # ============================================
        # STEP 2: Get last 7 days from database
        # ============================================
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        today_str = date.today().isoformat()

        existing_records = await insights_collection.find({
            "user_id": user_id,
            "ad_account_id": ad_account_id,
            "client_group_id": client_group_id,
            "date_start": {
                "$gte": seven_days_ago,
                "$lte": today_str
            }
        }).to_list(None)

        logger.info(f"📊 Database has {len(existing_records)} insights for last 7 days")

        # ============================================
        # STEP 3: Find gaps (missing records)
        # ============================================
        # Create set of existing records (campaign_id, date_start)
        existing_keys = {
            (record["campaign_id"], record["date_start"])
            for record in existing_records
        }

        # Find missing insights
        missing_insights = []
        for insight in all_api_insights:
            key = (insight.get("campaign_id"), insight.get("date_start"))

            if key not in existing_keys:
                missing_insights.append(insight)

        if missing_insights:
            logger.warning(
                f"⚠️ GAPS DETECTED: {len(missing_insights)} missing records "
                f"out of {len(all_api_insights)} total"
            )

            # Log which dates have gaps
            missing_dates = {}
            for insight in missing_insights:
                date_start = insight.get("date_start")
                missing_dates[date_start] = missing_dates.get(date_start, 0) + 1

            for missing_date, count in sorted(missing_dates.items()):
                logger.warning(f"  📅 {missing_date}: {count} missing records")
        else:
            logger.info("✅ No gaps found - all records up to date")

        return missing_insights

    except Exception as e:
        logger.error(f"Error in gap detection: {str(e)}", exc_info=True)
        return []


async def _fetch_campaign_insights_date_range(
        campaign_id: str,
        access_token: str,
        date_preset: str = "last_7d"
) -> List[Dict]:
    """Fetch campaign insights for a date range"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{campaign_id}/insights",
                params={
                    "date_preset": date_preset,
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                for insight in insights:
                    insight["campaign_id"] = campaign_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for campaign {campaign_id}: {str(e)}")
        return []


# Similar functions for adsets and ads...
async def update_todays_adset_insights(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client,
        ad_account_currency: str  # ✅ ADD THIS PARAMETER
) -> int:
    """
    INCREMENTAL: Update only TODAY's adset insight records.

    With fallback to last_7d if no data for today.
    """
    try:
        from utils.currency_exchange import CurrencyService

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_adset_insights"]

        today_str = date.today().isoformat()

        logger.info(f"🔄 Updating today's adset insights ({today_str})")

        # ✅ GET USER CURRENCY WITH FALLBACK
        try:
            user_currency = await CurrencyService.get_user_currency(user_id)
            logger.info(
                f"💱 User currency: {user_currency}, "
                f"Ad account currency: {ad_account_currency}"
            )
        except (ValueError, RuntimeError) as e:
            logger.warning(
                f"⚠️ Could not get user currency: {e}. "
                f"Using ad account currency {ad_account_currency} as fallback."
            )
            user_currency = ad_account_currency

        # Get all adset IDs
        adset_ids = await _get_all_adset_ids(ad_account_id, access_token)

        if not adset_ids:
            logger.info("✅ No adsets found")
            return 0

        logger.info(f"✅ Found {len(adset_ids)} adsets")

        # Fetch today's insights for each adset
        all_insights = []

        max_concurrent = 3
        for i in range(0, len(adset_ids), max_concurrent):
            batch = adset_ids[i:i + max_concurrent]

            tasks = [
                _fetch_todays_adset_insights(adset_id, access_token)
                for adset_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_insights.extend(result)

            await asyncio.sleep(0.5)

        # ============================================
        # 🔥 FALLBACK: If no data for today, fetch last_7d
        # ============================================
        if not all_insights:
            logger.warning(
                f"⚠️ No adset insights found for today ({today_str}), "
                f"fetching last_7d to check for gaps..."
            )

            all_insights = await _fetch_last_7d_adset_insights_with_gap_detection(
                adset_ids,
                access_token,
                user_id,
                ad_account_id,
                client_group_id,
                mongo_client
            )

            if not all_insights:
                logger.info("✅ No adset insights in last 7 days either")
                return 0
            else:
                logger.info(
                    f"✅ Fallback successful: Found {len(all_insights)} adset insights "
                    f"in last 7 days"
                )

        # ============================================
        # ✅ CONVERT CURRENCIES BEFORE SAVING
        # ============================================
        if all_insights and user_currency != ad_account_currency:
            total_original_spend = 0.0
            total_converted_spend = 0.0
            currency_fields = ['spend', 'cpc', 'cpm', 'cpp']

            for insight in all_insights:
                for field in currency_fields:
                    if field in insight and insight[field] is not None:
                        try:
                            original_value = float(insight[field])
                            converted_value = CurrencyService.convert(
                                amount=original_value,
                                from_currency=ad_account_currency,
                                to_currency=user_currency
                            )
                            insight[field] = converted_value
                            insight[f'{field}_original'] = original_value

                            if field == 'spend':
                                total_original_spend += original_value
                                total_converted_spend += converted_value
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Failed to convert {field}: {e}")

                insight['currency'] = user_currency
                insight['original_currency'] = ad_account_currency

            if total_original_spend > 0:
                logger.info(
                    f"💱 Converted {len(all_insights)} adset insights: "
                    f"${total_original_spend:.2f} {ad_account_currency} → "
                    f"${total_converted_spend:.2f} {user_currency}"
                )
        elif all_insights:
            for insight in all_insights:
                insight['currency'] = user_currency
                insight['original_currency'] = ad_account_currency
            logger.info(f"💱 Same currency ({user_currency}), no conversion needed")

        # Upsert today's records
        from pymongo import UpdateOne

        bulk_operations = []

        for insight in all_insights:
            bulk_operations.append(
                UpdateOne(
                    {
                        "user_id": user_id,
                        "ad_account_id": ad_account_id,
                        "adset_id": insight.get("adset_id"),
                        "date_start": insight.get("date_start")
                    },
                    {
                        "$set": {
                            "client_group_id": client_group_id,
                            "client_group_name": client_group_name,
                            "adset_name": insight.get("adset_name"),
                            "campaign_id": insight.get("campaign_id"),
                            "campaign_name": insight.get("campaign_name"),
                            "insight_data": insight,
                            "currency": insight.get('currency'),  # ✅ ADDED
                            "original_currency": insight.get('original_currency'),  # ✅ ADDED
                            "updated_at": datetime.now()
                        },
                        "$setOnInsert": {
                            "created_at": datetime.now()
                        }
                    },
                    upsert=True
                )
            )

        if bulk_operations:
            result = await insights_collection.bulk_write(bulk_operations, ordered=False)

            total_updated = result.upserted_count + result.modified_count

            logger.info(
                f"✅ Updated {total_updated} adset insights for {today_str} in {user_currency}"
            )

            return total_updated

        return 0

    except Exception as e:
        logger.error(f"❌ Error updating today's adset insights: {str(e)}", exc_info=True)
        return 0


async def update_todays_ad_insights(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client,
        ad_account_currency: str  # ✅ ADD THIS PARAMETER
) -> int:
    """
    INCREMENTAL: Update only TODAY's ad insight records.

    With fallback to last_7d if no data for today.
    """
    try:
        from utils.currency_exchange import CurrencyService

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_ad_insights"]

        today_str = date.today().isoformat()

        logger.info(f"🔄 Updating today's ad insights ({today_str})")

        # ✅ GET USER CURRENCY WITH FALLBACK
        try:
            user_currency = await CurrencyService.get_user_currency(user_id)
            logger.info(
                f"💱 User currency: {user_currency}, "
                f"Ad account currency: {ad_account_currency}"
            )
        except (ValueError, RuntimeError) as e:
            logger.warning(
                f"⚠️ Could not get user currency: {e}. "
                f"Using ad account currency {ad_account_currency} as fallback."
            )
            user_currency = ad_account_currency

        # Get all ad IDs
        ad_ids = await _get_all_ad_ids(ad_account_id, access_token)

        if not ad_ids:
            logger.info("✅ No ads found")
            return 0

        logger.info(f"✅ Found {len(ad_ids)} ads")

        # Fetch today's insights for each ad
        all_insights = []

        max_concurrent = 3
        for i in range(0, len(ad_ids), max_concurrent):
            batch = ad_ids[i:i + max_concurrent]

            tasks = [
                _fetch_todays_ad_insights(ad_id, access_token)
                for ad_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_insights.extend(result)

            await asyncio.sleep(0.5)

        # ============================================
        # 🔥 FALLBACK: If no data for today, fetch last_7d
        # ============================================
        if not all_insights:
            logger.warning(
                f"⚠️ No ad insights found for today ({today_str}), "
                f"fetching last_7d to check for gaps..."
            )

            all_insights = await _fetch_last_7d_ad_insights_with_gap_detection(
                ad_ids,
                access_token,
                user_id,
                ad_account_id,
                client_group_id,
                mongo_client
            )

            if not all_insights:
                logger.info("✅ No ad insights in last 7 days either")
                return 0
            else:
                logger.info(
                    f"✅ Fallback successful: Found {len(all_insights)} ad insights "
                    f"in last 7 days"
                )

        # ============================================
        # ✅ CONVERT CURRENCIES BEFORE SAVING
        # ============================================
        if all_insights and user_currency != ad_account_currency:
            total_original_spend = 0.0
            total_converted_spend = 0.0
            currency_fields = ['spend', 'cpc', 'cpm', 'cpp']

            for insight in all_insights:
                for field in currency_fields:
                    if field in insight and insight[field] is not None:
                        try:
                            original_value = float(insight[field])
                            converted_value = CurrencyService.convert(
                                amount=original_value,
                                from_currency=ad_account_currency,
                                to_currency=user_currency
                            )
                            insight[field] = converted_value
                            insight[f'{field}_original'] = original_value

                            if field == 'spend':
                                total_original_spend += original_value
                                total_converted_spend += converted_value
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Failed to convert {field}: {e}")

                insight['currency'] = user_currency
                insight['original_currency'] = ad_account_currency

            if total_original_spend > 0:
                logger.info(
                    f"💱 Converted {len(all_insights)} ad insights: "
                    f"${total_original_spend:.2f} {ad_account_currency} → "
                    f"${total_converted_spend:.2f} {user_currency}"
                )
        elif all_insights:
            for insight in all_insights:
                insight['currency'] = user_currency
                insight['original_currency'] = ad_account_currency
            logger.info(f"💱 Same currency ({user_currency}), no conversion needed")

        # Upsert today's records
        from pymongo import UpdateOne

        bulk_operations = []

        for insight in all_insights:
            bulk_operations.append(
                UpdateOne(
                    {
                        "user_id": user_id,
                        "ad_account_id": ad_account_id,
                        "ad_id": insight.get("ad_id"),
                        "date_start": insight.get("date_start")
                    },
                    {
                        "$set": {
                            "client_group_id": client_group_id,
                            "client_group_name": client_group_name,
                            "ad_name": insight.get("ad_name"),
                            "adset_id": insight.get("adset_id"),
                            "adset_name": insight.get("adset_name"),
                            "campaign_id": insight.get("campaign_id"),
                            "campaign_name": insight.get("campaign_name"),
                            "insight_data": insight,
                            "currency": insight.get('currency'),  # ✅ ADDED
                            "original_currency": insight.get('original_currency'),  # ✅ ADDED
                            "updated_at": datetime.now()
                        },
                        "$setOnInsert": {
                            "created_at": datetime.now()
                        }
                    },
                    upsert=True
                )
            )

        if bulk_operations:
            result = await insights_collection.bulk_write(bulk_operations, ordered=False)

            total_updated = result.upserted_count + result.modified_count

            logger.info(
                f"✅ Updated {total_updated} ad insights for {today_str} in {user_currency}"
            )

            return total_updated

        return 0

    except Exception as e:
        logger.error(f"❌ Error updating today's ad insights: {str(e)}", exc_info=True)
        return 0

async def _get_all_adset_ids(ad_account_id: str, access_token: str) -> List[str]:
    """Get all adset IDs"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_account_id}/adsets",
                params={
                    "fields": "id",
                    "access_token": access_token,
                    "limit": 500
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [adset["id"] for adset in data.get("data", [])]

            return []

    except Exception as e:
        logger.error(f"Error fetching adset IDs: {str(e)}")
        return []


async def _get_all_ad_ids(ad_account_id: str, access_token: str) -> List[str]:
    """Get all ad IDs"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_account_id}/ads",
                params={
                    "fields": "id",
                    "access_token": access_token,
                    "limit": 500
                }
            )

            if response.status_code == 200:
                data = response.json()
                return [ad["id"] for ad in data.get("data", [])]

            return []

    except Exception as e:
        logger.error(f"Error fetching ad IDs: {str(e)}")
        return []


async def _fetch_todays_adset_insights(
        adset_id: str,
        access_token: str
) -> List[Dict]:
    """Fetch today's insights for a single adset"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{adset_id}/insights",
                params={
                    "date_preset": "today",
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results,adset_name,adset_id"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                for insight in insights:
                    insight["adset_id"] = adset_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for adset {adset_id}: {str(e)}")
        return []


async def _fetch_todays_ad_insights(
        ad_id: str,
        access_token: str
) -> List[Dict]:
    """Fetch today's insights for a single ad"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_id}/insights",
                params={
                    "date_preset": "today",
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results,adset_name,adset_id,ad_name,ad_id"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                for insight in insights:
                    insight["ad_id"] = ad_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for ad {ad_id}: {str(e)}")
        return []


# ============================================
# GAP DETECTION HELPERS FOR ADSETS
# ============================================

async def _fetch_last_7d_adset_insights_with_gap_detection(
        adset_ids: List[str],
        access_token: str,
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        mongo_client
) -> List[Dict]:
    """
    FALLBACK: Fetch last 7 days of adset insights and identify gaps.
    """
    try:
        from datetime import timedelta

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_adset_insights"]

        logger.info("📋 Fetching adset last_7d from API...")

        all_api_insights = []

        max_concurrent = 3
        for i in range(0, len(adset_ids), max_concurrent):
            batch = adset_ids[i:i + max_concurrent]

            tasks = [
                _fetch_adset_insights_date_range(adset_id, access_token, "last_7d")
                for adset_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_api_insights.extend(result)

            await asyncio.sleep(0.5)

        if not all_api_insights:
            return []

        logger.info(f"📊 API returned {len(all_api_insights)} adset insights for last 7 days")

        # Get last 7 days from database
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        today_str = date.today().isoformat()

        existing_records = await insights_collection.find({
            "user_id": user_id,
            "ad_account_id": ad_account_id,
            "client_group_id": client_group_id,
            "date_start": {
                "$gte": seven_days_ago,
                "$lte": today_str
            }
        }).to_list(None)

        logger.info(f"📊 Database has {len(existing_records)} adset insights for last 7 days")

        # Find gaps
        existing_keys = {
            (record["adset_id"], record["date_start"])
            for record in existing_records
        }

        missing_insights = []
        for insight in all_api_insights:
            key = (insight.get("adset_id"), insight.get("date_start"))

            if key not in existing_keys:
                missing_insights.append(insight)

        if missing_insights:
            logger.warning(
                f"⚠️ GAPS DETECTED: {len(missing_insights)} missing adset records"
            )
        else:
            logger.info("✅ No adset gaps found")

        return missing_insights

    except Exception as e:
        logger.error(f"Error in adset gap detection: {str(e)}", exc_info=True)
        return []


async def _fetch_adset_insights_date_range(
        adset_id: str,
        access_token: str,
        date_preset: str = "last_7d"
) -> List[Dict]:
    """Fetch adset insights for a date range"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{adset_id}/insights",
                params={
                    "date_preset": date_preset,
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results,adset_name,adset_id"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                for insight in insights:
                    insight["adset_id"] = adset_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for adset {adset_id}: {str(e)}")
        return []


# ============================================
# GAP DETECTION HELPERS FOR ADS
# ============================================

async def _fetch_last_7d_ad_insights_with_gap_detection(
        ad_ids: List[str],
        access_token: str,
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        mongo_client
) -> List[Dict]:
    """
    FALLBACK: Fetch last 7 days of ad insights and identify gaps.
    """
    try:
        from datetime import timedelta

        db = mongo_client[os.getenv("MONGODB_DB", "birdyai")]
        insights_collection = db["facebook_ad_insights"]

        logger.info("📋 Fetching ad last_7d from API...")

        all_api_insights = []

        max_concurrent = 3
        for i in range(0, len(ad_ids), max_concurrent):
            batch = ad_ids[i:i + max_concurrent]

            tasks = [
                _fetch_ad_insights_date_range(ad_id, access_token, "last_7d")
                for ad_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if not isinstance(result, Exception) and result:
                    all_api_insights.extend(result)

            await asyncio.sleep(0.5)

        if not all_api_insights:
            return []

        logger.info(f"📊 API returned {len(all_api_insights)} ad insights for last 7 days")

        # Get last 7 days from database
        seven_days_ago = (date.today() - timedelta(days=7)).isoformat()
        today_str = date.today().isoformat()

        existing_records = await insights_collection.find({
            "user_id": user_id,
            "ad_account_id": ad_account_id,
            "client_group_id": client_group_id,
            "date_start": {
                "$gte": seven_days_ago,
                "$lte": today_str
            }
        }).to_list(None)

        logger.info(f"📊 Database has {len(existing_records)} ad insights for last 7 days")

        # Find gaps
        existing_keys = {
            (record["ad_id"], record["date_start"])
            for record in existing_records
        }

        missing_insights = []
        for insight in all_api_insights:
            key = (insight.get("ad_id"), insight.get("date_start"))

            if key not in existing_keys:
                missing_insights.append(insight)

        if missing_insights:
            logger.warning(
                f"⚠️ GAPS DETECTED: {len(missing_insights)} missing ad records"
            )
        else:
            logger.info("✅ No ad gaps found")

        return missing_insights

    except Exception as e:
        logger.error(f"Error in ad gap detection: {str(e)}", exc_info=True)
        return []


async def _fetch_ad_insights_date_range(
        ad_id: str,
        access_token: str,
        date_preset: str = "last_7d"
) -> List[Dict]:
    """Fetch ad insights for a date range"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(
                f"https://graph.facebook.com/v23.0/{ad_id}/insights",
                params={
                    "date_preset": date_preset,
                    "time_increment": "1",
                    "access_token": access_token,
                    "fields": (
                        "campaign_id,campaign_name,clicks,spend,"
                        "social_spend,conversion_rate_ranking,"
                        "conversions,cpc,cpm,cpp,ctr,impressions,"
                        "reach,results,adset_name,adset_id,ad_name,ad_id"
                    )
                }
            )

            if response.status_code == 200:
                data = response.json()
                insights = data.get("data", [])

                for insight in insights:
                    insight["ad_id"] = ad_id

                return insights

            return []

    except Exception as e:
        logger.error(f"Error fetching insights for ad {ad_id}: {str(e)}")
        return []