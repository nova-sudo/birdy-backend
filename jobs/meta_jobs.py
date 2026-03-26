import asyncio
import logging
import os
import time
from datetime import datetime

import httpx

from core.database import DB_NAME
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_service import (
    fetch_meta_data_for_group,
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)

logger = logging.getLogger(__name__)


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
            db = mongo_client[DB_NAME]
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

                        # ✅ CHECK FOR REQUIRED ad_account_currency
                        ad_account_currency = group.get("ad_account_currency")

                        if not ad_account_currency:
                            logger.warning(
                                f"⚠️ Group '{group_name}' missing 'ad_account_currency', "
                                f"fetching from Meta API..."
                            )
                            try:
                                async with httpx.AsyncClient() as client:
                                    url = f"https://graph.facebook.com/v18.0/{meta_ad_account_id}"
                                    params = {"fields": "currency", "access_token": access_token}
                                    resp = await client.get(url, params=params)
                                    data = resp.json()
                                    ad_account_currency = data.get("currency")

                                if ad_account_currency:
                                    await client_groups_collection.update_one(
                                        {"id": group_id},
                                        {"$set": {"ad_account_currency": ad_account_currency}}
                                    )
                                    logger.info(
                                        f"✅ Fetched and saved currency '{ad_account_currency}' "
                                        f"for '{group_name}'"
                                    )
                                else:
                                    logger.error(
                                        f"❌ Could not fetch currency for '{group_name}', skipping..."
                                    )
                                    failure_count += 1
                                    continue

                            except Exception as e:
                                logger.error(
                                    f"❌ Error fetching currency for '{group_name}': {e}, skipping..."
                                )
                                failure_count += 1
                                continue

                        try:
                            logger.info(
                                f"  🔄 Refreshing ALL presets for '{group_name}' "
                                f"(account: {meta_ad_account_id}, currency: {ad_account_currency})"
                            )

                            # ── STEP 1: Refresh all Meta date-preset buckets ──────────────
                            await fetch_meta_all_presets_for_group(
                                group_id,
                                meta_ad_account_id,
                                user_id,
                                mongo_client,
                                ad_account_currency
                            )

                            # Cool down 60s between preset fetch and granular
                            # per-day rows — same rate limit window concern.
                            logger.info("⏳ Cooling down 60s before granular insight fetch...")
                            await asyncio.sleep(60)

                            # ── STEP 2: Update today's granular per-day rows (incremental) ─
                            from integrations.facebook_utils.meta_incremental_refresh import (
                                update_todays_campaign_insights,
                                update_todays_adset_insights,
                                update_todays_ad_insights,
                                fetch_todays_facebook_leads_incremental
                            )

                            tasks = [
                                update_todays_campaign_insights(
                                    meta_ad_account_id, access_token, user_id,
                                    group_id, group_name, mongo_client, ad_account_currency
                                ),
                                update_todays_adset_insights(
                                    meta_ad_account_id, access_token, user_id,
                                    group_id, group_name, mongo_client, ad_account_currency
                                ),
                                update_todays_ad_insights(
                                    meta_ad_account_id, access_token, user_id,
                                    group_id, group_name, mongo_client, ad_account_currency
                                ),
                            ]
                            insights_results = await asyncio.gather(*tasks, return_exceptions=True)
                            campaign_count = insights_results[0] if not isinstance(insights_results[0], Exception) else 0
                            adset_count    = insights_results[1] if not isinstance(insights_results[1], Exception) else 0
                            ad_count       = insights_results[2] if not isinstance(insights_results[2], Exception) else 0

                            # ── STEP 3: Incremental leads ─────────────────────────────────
                            new_leads_count, new_leads = await fetch_todays_facebook_leads_incremental(
                                meta_ad_account_id, access_token, user_id,
                                group_id, group_name, mongo_client, max_concurrent_ads=5
                            )

                            if new_leads:
                                leads_collection = db["facebook_leads"]
                                lead_docs = [
                                    {
                                        "user_id": user_id,
                                        "ad_account_id": meta_ad_account_id,
                                        "client_group_id": group_id,
                                        "client_group_name": group_name,
                                        "lead_id": lead.get("lead_id"),
                                        "lead_data": lead,
                                        "created_at": datetime.now(),
                                        "updated_at": datetime.now(),
                                    }
                                    for lead in new_leads
                                ]
                                try:
                                    await leads_collection.insert_many(lead_docs, ordered=False)
                                    logger.info(f"  💾 Saved {len(lead_docs)} new leads")
                                except Exception as e:
                                    logger.warning(f"  ⚠️ Some duplicate leads: {str(e)}")

                            await update_preset_lead_counts(
                                group_id,
                                user_id,
                                mongo_client,
                            )

                            logger.info(
                                f"  ✅ Refreshed '{group_name}': all presets + "
                                f"{campaign_count} campaign rows, {adset_count} adset rows, "
                                f"{ad_count} ad rows, {new_leads_count} new leads"
                            )
                            success_count += 1

                        except Exception as e:
                            logger.error(
                                f"  ❌ Error refreshing '{group_name}': {str(e)}",
                                exc_info=True
                            )
                            failure_count += 1

                    # ============================================
                    # 🔥 DELAY BETWEEN USERS
                    # ============================================
                    if user_index < len(users_with_groups):
                        await asyncio.sleep(10)
                        logger.info(f"⏳ Waiting 10s before next user...")

                except Exception as e:
                    failure_count += len(groups)
                    logger.error(f"❌ Error refreshing Meta data for user {user_id}: {e}", exc_info=True)

            # ============================================
            # 🎉 FINAL SUMMARY
            # ============================================
            elapsed = (datetime.utcnow() - start_time).total_seconds()

            total_groups = success_count + failure_count
            summary_lines = [
                "=" * 70,
                "🎉 INCREMENTAL META DATA REFRESH COMPLETED",
                "=" * 70,
                f"⏱️  Duration: {elapsed:.2f} seconds",
                f"📊 Total Groups: {total_groups}",
                f"✅ Successful: {success_count}",
                f"❌ Failed: {failure_count}",
                f"📈 Success Rate: {(success_count / total_groups * 100):.1f}%" if total_groups > 0 else "📈 Success Rate: N/A",
                "=" * 70
            ]

            for line in summary_lines:
                logger.info(line)

            if success_count > 0 and failure_count == 0:
                logger.info(
                    f"🎊 ALL GROUPS REFRESHED SUCCESSFULLY! "
                    f"{success_count} group{'s' if success_count != 1 else ''} updated with today's data."
                )
            elif success_count > 0 and failure_count > 0:
                logger.warning(
                    f"⚠️  PARTIAL SUCCESS: {success_count} groups updated successfully, "
                    f"but {failure_count} groups failed. Check errors above."
                )
            elif success_count == 0 and failure_count > 0:
                logger.error(
                    f"❌ ALL GROUPS FAILED! {failure_count} groups could not be refreshed. "
                    f"Check errors above for details."
                )
            else:
                logger.info("ℹ️  No groups were processed.")

        except Exception as e:
            logger.error(f"❌ Critical error in Meta refresh job: {e}", exc_info=True)
