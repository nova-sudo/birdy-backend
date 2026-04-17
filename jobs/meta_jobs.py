import asyncio
import logging
import os
import time
from datetime import datetime

import httpx

from core.database import DB_NAME
from core.constants import META_PRESETS_FREQUENT, META_PRESETS_SLOW
from dependencies import get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from services.meta_service import (
    fetch_meta_data_for_group,
    fetch_meta_all_presets_for_group,
    update_preset_lead_counts,
)

logger = logging.getLogger(__name__)


class InvalidTokenError(Exception):
    """Raised when the Facebook access token is invalid/expired."""
    pass


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
                logger.warning(f"No data for {meta_ad_account_id}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited
                wait_time = 5 * (2 ** retry)
                logger.warning(f"Rate limited on {meta_ad_account_id}, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
            elif e.response.status_code >= 500:
                # Server error, retry
                wait_time = 2 ** retry
                logger.warning(f"Server error for {meta_ad_account_id}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                # Client error, don't retry
                logger.error(f"Client error for {meta_ad_account_id}: {e.response.status_code}")
                return None

        except Exception as e:
            if retry < max_retries - 1:
                wait_time = 2 ** retry
                logger.warning(f"Error for {meta_ad_account_id}: {e}, retrying in {wait_time}s")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Failed to fetch Meta data for {meta_ad_account_id} after {max_retries} retries: {e}")
                return None

    return None


async def _validate_token(access_token: str) -> bool:
    """Quick token validation against the Meta API. Returns False if invalid."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://graph.facebook.com/v25.0/me",
                params={"access_token": access_token},
            )
            if resp.status_code == 200:
                return True
            body = resp.text
            # Code 190: invalid/expired token
            # Code 200: missing permissions
            if ('"code":190' in body or "not allowed" in body
                    or "not a confirmed user" in body
                    or '"code":200' in body or "ads_management" in body):
                return False
            # Other errors — token might still be valid, don't block
            return True
    except Exception:
        return True  # network error — don't block on validation failure


def _should_run_slow_presets() -> bool:
    """
    Slow presets run once daily at the first scheduled run after midnight.
    We use hour < 2 as a window to catch the first run of the day
    (scheduler runs at :10 every hour).
    """
    return datetime.utcnow().hour < 2


async def refresh_meta_data_for_all_users():
    """
    TIERED REFRESH: Frequent presets every hour, slow presets once daily.

    Frequent (hourly): today, yesterday, this_week, last_7d
    Slow (daily):      maximum, last_14d, last_30d, this_month, last_month,
                       this_quarter, last_quarter, this_year, last_year

    Also fetches today's granular per-day insight rows and leads incrementally.
    """
    async with get_mongo_client() as mongo_client:
        start_time = datetime.utcnow()
        success_count = 0
        failure_count = 0
        skipped_token = 0

        run_slow = _should_run_slow_presets()
        presets_to_fetch = META_PRESETS_FREQUENT + (META_PRESETS_SLOW if run_slow else [])
        refresh_mode = "full" if run_slow else "frequent_only"

        try:
            logger.info(
                f"Starting Meta refresh — mode: {refresh_mode}, "
                f"presets: {len(presets_to_fetch)} "
                f"({'+ slow presets' if run_slow else 'frequent only'})"
            )
            db = mongo_client[DB_NAME]
            client_groups_collection = db["client_groups"]

            # Get all unique users with Meta ad accounts
            pipeline = [
                {"$match": {"meta_ad_account_id": {"$exists": True, "$ne": None}}},
                {"$group": {"_id": "$user_id", "groups": {"$push": "$$ROOT"}}}
            ]

            users_with_groups = await client_groups_collection.aggregate(pipeline).to_list(None)

            if not users_with_groups:
                logger.info("No users with Meta ad accounts found")
                return

            logger.info(f"Found {len(users_with_groups)} users with Meta ad accounts")

            for user_index, user_data in enumerate(users_with_groups, 1):
                user_id = user_data["_id"]
                groups = user_data["groups"]

                try:
                    logger.info(
                        f"[{user_index}/{len(users_with_groups)}] "
                        f"Processing user {user_id} ({len(groups)} ad accounts)"
                    )

                    # Check token validity
                    facebook_token = await get_facebook_token(user_id, mongo_client)

                    if not facebook_token or not facebook_token.get("access_token"):
                        logger.warning(f"User {user_id} has no Facebook token, skipping")
                        skipped_token += len(groups)
                        continue

                    access_token = facebook_token["access_token"]

                    # Validate token before wasting API calls on all presets
                    token_valid = await _validate_token(access_token)
                    if not token_valid:
                        logger.warning(
                            f"User {user_id} has an invalid/expired Facebook token "
                            f"(OAuthException). Skipping all {len(groups)} groups. "
                            f"User needs to re-authenticate."
                        )
                        skipped_token += len(groups)

                        # Mark the groups so the frontend can show a warning
                        await client_groups_collection.update_many(
                            {"user_id": user_id, "meta_ad_account_id": {"$exists": True, "$ne": None}},
                            {"$set": {"meta_token_error": True, "meta_token_error_at": datetime.utcnow()}},
                        )
                        continue

                    # Clear any previous token error flag
                    await client_groups_collection.update_many(
                        {"user_id": user_id, "meta_token_error": True},
                        {"$unset": {"meta_token_error": "", "meta_token_error_at": ""}},
                    )

                    # Process each group's Meta account
                    for group_index, group in enumerate(groups):
                        if not group.get("meta_ad_account_id"):
                            continue

                        group_id = group["id"]
                        meta_ad_account_id = group["meta_ad_account_id"]
                        group_name = group.get("name", "Unknown Group")

                        ad_account_currency = group.get("ad_account_currency")

                        if not ad_account_currency:
                            logger.warning(
                                f"Group '{group_name}' missing 'ad_account_currency', "
                                f"fetching from Meta API..."
                            )
                            try:
                                async with httpx.AsyncClient() as client:
                                    url = f"https://graph.facebook.com/v25.0/{meta_ad_account_id}"
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
                                        f"Fetched and saved currency '{ad_account_currency}' "
                                        f"for '{group_name}'"
                                    )
                                else:
                                    logger.error(
                                        f"Could not fetch currency for '{group_name}', skipping..."
                                    )
                                    failure_count += 1
                                    continue

                            except Exception as e:
                                logger.error(
                                    f"Error fetching currency for '{group_name}': {e}, skipping..."
                                )
                                failure_count += 1
                                continue

                        try:
                            logger.info(
                                f"  Refreshing {len(presets_to_fetch)} presets for '{group_name}' "
                                f"(account: {meta_ad_account_id}, mode: {refresh_mode})"
                            )

                            # ── STEP 1: Refresh tiered Meta date-preset buckets ──────
                            await fetch_meta_all_presets_for_group(
                                group_id,
                                meta_ad_account_id,
                                user_id,
                                mongo_client,
                                ad_account_currency,
                                presets=presets_to_fetch,
                            )

                            # Cool down between preset fetch and granular rows
                            cooldown = 60 if run_slow else 30
                            logger.info(f"  Cooling down {cooldown}s before granular insight fetch...")
                            await asyncio.sleep(cooldown)

                            # ── STEP 2: Update today's granular per-day rows (incremental)
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

                            # ── STEP 3: Incremental leads ────────────────────────────
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
                                    logger.info(f"  Saved {len(lead_docs)} new leads")
                                except Exception as e:
                                    logger.warning(f"  Some duplicate leads: {str(e)}")

                            await update_preset_lead_counts(
                                group_id,
                                user_id,
                                mongo_client,
                            )

                            logger.info(
                                f"  Done '{group_name}': {len(presets_to_fetch)} presets + "
                                f"{campaign_count} campaign rows, {adset_count} adset rows, "
                                f"{ad_count} ad rows, {new_leads_count} new leads"
                            )
                            success_count += 1

                        except Exception as e:
                            logger.error(
                                f"  Error refreshing '{group_name}': {str(e)}",
                                exc_info=True
                            )
                            failure_count += 1

                        # Cooldown between groups
                        if group_index < len(groups) - 1:
                            wait = 30 if run_slow else 15
                            logger.info(f"  Cooling down {wait}s before next group...")
                            await asyncio.sleep(wait)

                    # Delay between users
                    if user_index < len(users_with_groups):
                        await asyncio.sleep(10)

                except Exception as e:
                    failure_count += len(groups)
                    logger.error(f"Error refreshing Meta data for user {user_id}: {e}", exc_info=True)

            # ── SUMMARY ──────────────────────────────────────────────────
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            total_groups = success_count + failure_count + skipped_token

            logger.info("=" * 70)
            logger.info(f"META REFRESH COMPLETED — mode: {refresh_mode}")
            logger.info(f"  Duration:       {elapsed:.1f}s")
            logger.info(f"  Presets fetched: {len(presets_to_fetch)}")
            logger.info(f"  Successful:     {success_count}")
            logger.info(f"  Failed:         {failure_count}")
            logger.info(f"  Skipped (token): {skipped_token}")
            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"Critical error in Meta refresh job: {e}", exc_info=True)
