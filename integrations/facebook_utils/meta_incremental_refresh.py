"""
Meta Data Incremental Refresh - TODAY Strategy

Core fix: Use ACCOUNT-LEVEL insights endpoint instead of fetching
each campaign/adset/ad individually. This reduces API calls from
N(campaigns) + N(adsets) + N(ads) → just 3 calls for insights.

Rate limit strategy:
- Exponential backoff with jitter on 429 responses
- All insights fetched at account level (act_xxx/insights with level= param)
- Leads still iterate per-ad but with proper throttling
"""

import asyncio
import logging
import random
from datetime import datetime, date
from typing import List, Dict, Tuple, Optional
import httpx
import os

logger = logging.getLogger(__name__)


# ============================================
# RATE LIMIT HELPERS
# ============================================

async def _api_get_with_retry(
        client: httpx.AsyncClient,
        url: str,
        params: dict,
        max_retries: int = 4
) -> Optional[dict]:
    """
    GET with exponential backoff on 429 / 503 responses.
    Returns parsed JSON or None on permanent failure.
    """
    for attempt in range(max_retries):
        try:
            response = await client.get(url, params=params)

            if response.status_code == 200:
                return response.json()

            if response.status_code in (429, 503, 500):
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Rate limited ({response.status_code}). "
                    f"Retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(wait)
                continue

            logger.error(
                f"API error {response.status_code} for {url}: {response.text[:200]}"
            )
            return None

        except httpx.TimeoutException:
            wait = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Timeout, retrying in {wait:.1f}s")
            await asyncio.sleep(wait)
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {e}")
            return None

    logger.error(f"Exhausted retries for {url}")
    return None


# ============================================
# ACCOUNT-LEVEL INSIGHTS (the key optimisation)
# ============================================

async def _fetch_account_insights_today(
        ad_account_id: str,
        access_token: str,
        level: str,
) -> List[Dict]:
    """
    Fetch ALL insights for the account at once using the account-level
    /insights endpoint with level= parameter.

    Replaces N individual calls with a single paginated request.
    level: "campaign", "adset", or "ad"
    """
    fields_map = {
        "campaign": (
            "campaign_id,campaign_name,clicks,spend,"
            "social_spend,conversion_rate_ranking,"
            "conversions,cpc,cpm,cpp,ctr,impressions,reach,results"
        ),
        "adset": (
            "campaign_id,campaign_name,adset_id,adset_name,clicks,spend,"
            "social_spend,conversion_rate_ranking,"
            "conversions,cpc,cpm,cpp,ctr,impressions,reach,results"
        ),
        "ad": (
            "campaign_id,campaign_name,adset_id,adset_name,"
            "ad_id,ad_name,clicks,spend,"
            "social_spend,conversion_rate_ranking,"
            "conversions,cpc,cpm,cpp,ctr,impressions,reach,results"
        ),
    }

    all_insights = []
    base_url = f"https://graph.facebook.com/v25.0/{ad_account_id}/insights"
    params = {
        "level": level,
        "date_preset": "today",
        "time_increment": "1",
        "access_token": access_token,
        "fields": fields_map[level],
        "limit": 500,
    }

    page = 0
    next_url = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            data = await _api_get_with_retry(
                client,
                base_url if page == 1 else next_url,
                params if page == 1 else {}
            )

            if data is None:
                logger.error(f"Failed to fetch {level} insights page {page}")
                break

            insights = data.get("data", [])
            all_insights.extend(insights)

            next_url = data.get("paging", {}).get("next")
            if not next_url or not insights:
                break

            await asyncio.sleep(0.3)

    logger.info(f"  Account-level {level} insights: {len(all_insights)} records")
    return all_insights


# ============================================
# CAMPAIGN INSIGHTS
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
    try:
        from utils.currency_exchange import CurrencyService
        from pymongo import UpdateOne

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_campaign_insights"]
        today_str = date.today().isoformat()

        logger.info(f"Updating today's campaign insights ({today_str})")

        user_currency = _get_user_currency_safe(
            await _try_get_user_currency(user_id, CurrencyService),
            ad_account_currency
        )
        logger.info(f"User currency: {user_currency}, Ad account currency: {ad_account_currency}")

        all_insights = await _fetch_account_insights_today(ad_account_id, access_token, "campaign")

        if not all_insights:
            logger.info(f"No campaign insights for today ({today_str})")
            return 0

        all_insights = _apply_currency_conversion(
            all_insights, ad_account_currency, user_currency, CurrencyService
        )

        bulk_ops = [
            UpdateOne(
                {
                    "user_id": user_id,
                    "ad_account_id": ad_account_id,
                    "campaign_id": i.get("campaign_id"),
                    "date_start": i.get("date_start"),
                },
                {
                    "$set": {
                        "client_group_id": client_group_id,
                        "client_group_name": client_group_name,
                        "insight_data": i,
                        "currency": i.get("currency"),
                        "original_currency": i.get("original_currency"),
                        "updated_at": datetime.now(),
                    },
                    "$setOnInsert": {"created_at": datetime.now()},
                },
                upsert=True,
            )
            for i in all_insights
        ]

        result = await insights_collection.bulk_write(bulk_ops, ordered=False)
        total = result.upserted_count + result.modified_count
        logger.info(
            f"Updated {total} campaign insights for {today_str} in {user_currency} "
            f"({result.upserted_count} new, {result.modified_count} updated)"
        )
        return total

    except Exception as e:
        logger.error(f"Error updating today's campaign insights: {e}", exc_info=True)
        return 0


# ============================================
# ADSET INSIGHTS
# ============================================

async def update_todays_adset_insights(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client,
        ad_account_currency: str
) -> int:
    try:
        from utils.currency_exchange import CurrencyService
        from pymongo import UpdateOne

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_adset_insights"]
        today_str = date.today().isoformat()

        logger.info(f"Updating today's adset insights ({today_str})")

        user_currency = _get_user_currency_safe(
            await _try_get_user_currency(user_id, CurrencyService),
            ad_account_currency
        )
        logger.info(f"User currency: {user_currency}, Ad account currency: {ad_account_currency}")

        all_insights = await _fetch_account_insights_today(ad_account_id, access_token, "adset")

        if not all_insights:
            logger.info(f"No adset insights for today ({today_str})")
            return 0

        all_insights = _apply_currency_conversion(
            all_insights, ad_account_currency, user_currency, CurrencyService
        )

        bulk_ops = [
            UpdateOne(
                {
                    "user_id": user_id,
                    "ad_account_id": ad_account_id,
                    "adset_id": i.get("adset_id"),
                    "date_start": i.get("date_start"),
                },
                {
                    "$set": {
                        "client_group_id": client_group_id,
                        "client_group_name": client_group_name,
                        "adset_name": i.get("adset_name"),
                        "campaign_id": i.get("campaign_id"),
                        "campaign_name": i.get("campaign_name"),
                        "insight_data": i,
                        "currency": i.get("currency"),
                        "original_currency": i.get("original_currency"),
                        "updated_at": datetime.now(),
                    },
                    "$setOnInsert": {"created_at": datetime.now()},
                },
                upsert=True,
            )
            for i in all_insights
        ]

        result = await insights_collection.bulk_write(bulk_ops, ordered=False)
        total = result.upserted_count + result.modified_count
        logger.info(f"Updated {total} adset insights for {today_str} in {user_currency}")
        return total

    except Exception as e:
        logger.error(f"Error updating today's adset insights: {e}", exc_info=True)
        return 0


# ============================================
# AD INSIGHTS
# ============================================

async def update_todays_ad_insights(
        ad_account_id: str,
        access_token: str,
        user_id: str,
        client_group_id: str,
        client_group_name: str,
        mongo_client,
        ad_account_currency: str
) -> int:
    try:
        from utils.currency_exchange import CurrencyService
        from pymongo import UpdateOne

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        insights_collection = db["facebook_ad_insights"]
        today_str = date.today().isoformat()

        logger.info(f"Updating today's ad insights ({today_str})")

        user_currency = _get_user_currency_safe(
            await _try_get_user_currency(user_id, CurrencyService),
            ad_account_currency
        )
        logger.info(f"User currency: {user_currency}, Ad account currency: {ad_account_currency}")

        all_insights = await _fetch_account_insights_today(ad_account_id, access_token, "ad")

        if not all_insights:
            logger.info(f"No ad insights for today ({today_str})")
            return 0

        all_insights = _apply_currency_conversion(
            all_insights, ad_account_currency, user_currency, CurrencyService
        )

        bulk_ops = [
            UpdateOne(
                {
                    "user_id": user_id,
                    "ad_account_id": ad_account_id,
                    "ad_id": i.get("ad_id"),
                    "date_start": i.get("date_start"),
                },
                {
                    "$set": {
                        "client_group_id": client_group_id,
                        "client_group_name": client_group_name,
                        "ad_name": i.get("ad_name"),
                        "adset_id": i.get("adset_id"),
                        "adset_name": i.get("adset_name"),
                        "campaign_id": i.get("campaign_id"),
                        "campaign_name": i.get("campaign_name"),
                        "insight_data": i,
                        "currency": i.get("currency"),
                        "original_currency": i.get("original_currency"),
                        "updated_at": datetime.now(),
                    },
                    "$setOnInsert": {"created_at": datetime.now()},
                },
                upsert=True,
            )
            for i in all_insights
        ]

        result = await insights_collection.bulk_write(bulk_ops, ordered=False)
        total = result.upserted_count + result.modified_count
        logger.info(f"Updated {total} ad insights for {today_str} in {user_currency}")
        return total

    except Exception as e:
        logger.error(f"Error updating today's ad insights: {e}", exc_info=True)
        return 0


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
    INCREMENTAL: Fetch every Facebook lead created since our last-known
    watermark, stopping when the cursor sees an already-stored lead_id or
    a lead older than the watermark's created_time.

    Historical note: this function was previously misnamed and misbehaved
    when the refresh missed a day boundary. Two bugs fixed here:

      1. Ad set was scoped to "today's active ads" via a
         `date_preset: "today"` filter on /{ad_account}/ads. Ads paused
         today with unread leads from yesterday were skipped entirely
         → those leads never made it into the cache.

      2. Lead fetch was scoped to `date_preset: "today"` too. If a refresh
         was skipped past a day boundary (server outage, missed cron
         cycle, etc.), yesterday's leads never appeared in the "today"
         window, so the watermark cursor never fired and we silently
         lost yesterday's leads.

    Fix: use the FULL ad list (via stage1_get_all_ad_ids — the same helper
    the initial-sync path uses) and let the watermark decide when to stop
    on each ad. `date_preset: "maximum"` explicitly gives us all leads
    with no time filter; Meta returns them newest-first, so the watermark
    stops us within a few pages of a healthy cron cadence.
    """
    try:
        # Late import so the two files stay decoupled at module load time.
        from integrations.facebook_utils.facebook_leads import stage1_get_all_ad_ids

        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        leads_collection = db["facebook_leads"]

        last_known_lead = await leads_collection.find_one(
            {
                "user_id": user_id,
                "ad_account_id": ad_account_id,
                "client_group_id": client_group_id,
            },
            sort=[("lead_data.created_time", -1)],
        )

        last_known_lead_id = None
        last_known_created_time = None

        if last_known_lead:
            last_known_lead_id = last_known_lead.get("lead_id")
            last_known_created_time = last_known_lead.get("lead_data", {}).get("created_time")
            logger.info(f"Last known lead: {last_known_lead_id} from {last_known_created_time}")
        else:
            logger.info("No previous leads — this ad account has never had a lead fetched. "
                        "Fetching everything (watermark will not trigger).")

        # Full ad list — the previous implementation used a "today's active
        # ads" filter which silently dropped ads paused today with unread
        # leads from yesterday. See docstring.
        ad_ids = await stage1_get_all_ad_ids(ad_account_id, access_token)

        if not ad_ids:
            logger.info("No ads found on this account — nothing to fetch")
            return 0, []

        logger.info(f"Found {len(ad_ids)} ads (all lifetime), scanning for new leads since watermark")

        all_new_leads = []
        should_continue = True

        for i in range(0, len(ad_ids), max_concurrent_ads):
            if not should_continue:
                logger.info("Stopped early - found last known lead")
                break

            batch = ad_ids[i:i + max_concurrent_ads]
            batch_num = (i // max_concurrent_ads) + 1
            total_batches = (len(ad_ids) + max_concurrent_ads - 1) // max_concurrent_ads
            logger.info(f"Leads batch {batch_num}/{total_batches} ({len(batch)} ads)")

            tasks = [
                _get_todays_leads_for_ad(
                    ad_id, access_token, last_known_lead_id, last_known_created_time
                )
                for ad_id in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in batch_results:
                if isinstance(result, Exception):
                    logger.error(f"Error in leads batch: {result}")
                    continue
                new_leads, hit_last_known = result
                if new_leads:
                    all_new_leads.extend(new_leads)
                if hit_last_known:
                    should_continue = False
                    break

            if should_continue and i + max_concurrent_ads < len(ad_ids):
                await asyncio.sleep(0.5)

        normalized = _normalize_leads(
            all_new_leads, user_id, ad_account_id, client_group_id, client_group_name
        )
        logger.info(f"LEADS INCREMENTAL COMPLETE: {len(normalized)} new leads")
        return len(normalized), normalized

    except Exception as e:
        logger.error(f"Error in incremental leads fetch: {e}", exc_info=True)
        return 0, []


async def _get_todays_active_ad_ids(ad_account_id: str, access_token: str) -> List[str]:
    all_ids = []
    base_url = f"https://graph.facebook.com/v25.0/{ad_account_id}/ads"
    params = {
        "fields": "id",
        "access_token": access_token,
        "limit": 500,
        "date_preset": "today",
    }
    page = 0
    next_url = None

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            data = await _api_get_with_retry(
                client,
                base_url if page == 1 else next_url,
                params if page == 1 else {}
            )
            if not data:
                break
            ads = data.get("data", [])
            all_ids.extend(ad["id"] for ad in ads)
            next_url = data.get("paging", {}).get("next")
            if not next_url or not ads:
                break
            await asyncio.sleep(0.1)

    return all_ids


async def _get_todays_leads_for_ad(
        ad_id: str,
        access_token: str,
        last_known_lead_id: Optional[str],
        last_known_created_time: Optional[str],
) -> Tuple[List[dict], bool]:
    new_leads = []
    hit_last_known = False

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # `date_preset: "maximum"` = no time filter — Meta returns
            # every lead ever collected for this ad, newest-first. The
            # watermark inside _collect_new_leads is what stops the
            # walk; using "today" here (as this code used to) silently
            # dropped leads created before midnight on any refresh that
            # skipped past a day boundary.
            data = await _api_get_with_retry(
                client,
                f"https://graph.facebook.com/v25.0/{ad_id}",
                {"fields": "leads,name", "date_preset": "maximum", "access_token": access_token}
            )
            if not data:
                return [], False

            ad_name = data.get("name", "")
            leads_data = data.get("leads", {})

            page_leads, hit_last_known = _collect_new_leads(
                leads_data.get("data", []), ad_name,
                last_known_lead_id, last_known_created_time
            )
            new_leads.extend(page_leads)

            if hit_last_known:
                return new_leads, True

            next_url = leads_data.get("paging", {}).get("next")

            while next_url and not hit_last_known:
                page_data = await _api_get_with_retry(client, next_url, {})
                if not page_data:
                    break
                more = page_data.get("data", [])
                if not more:
                    break
                page_leads, hit_last_known = _collect_new_leads(
                    more, ad_name, last_known_lead_id, last_known_created_time
                )
                new_leads.extend(page_leads)
                next_url = page_data.get("paging", {}).get("next")
                await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"Error fetching leads for ad {ad_id}: {e}", exc_info=True)

    return new_leads, hit_last_known


def _collect_new_leads(
        leads: List[dict],
        ad_name: str,
        last_known_lead_id: Optional[str],
        last_known_created_time: Optional[str],
) -> Tuple[List[dict], bool]:
    new_leads = []
    hit_last_known = False

    for lead in leads:
        lead_id = lead.get("id")
        created_time = lead.get("created_time")

        if last_known_lead_id and lead_id == last_known_lead_id:
            hit_last_known = True
            break

        if last_known_created_time and created_time:
            try:
                if (
                    datetime.fromisoformat(created_time.replace("Z", "+00:00"))
                    <= datetime.fromisoformat(last_known_created_time.replace("Z", "+00:00"))
                ):
                    hit_last_known = True
                    break
            except Exception:
                pass

        lead["ad_name"] = ad_name
        new_leads.append(lead)

    return new_leads, hit_last_known


def _normalize_leads(
        raw_leads: List[dict],
        user_id: str,
        ad_account_id: str,
        client_group_id: str,
        client_group_name: str,
) -> List[dict]:
    result = []
    for lead in raw_leads:
        field_data = {}
        for field in lead.get("field_data", []):
            values = field.get("values", [])
            if values:
                field_data[field.get("name", "")] = values[0]

        result.append({
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
        })
    return result


# ============================================
# SHARED HELPERS
# ============================================

async def _try_get_user_currency(user_id: str, CurrencyService) -> Optional[str]:
    try:
        return await CurrencyService.get_user_currency(user_id)
    except Exception as e:
        logger.warning(f"Could not get user currency: {e}")
        return None


def _get_user_currency_safe(user_currency: Optional[str], fallback: str) -> str:
    if user_currency:
        return user_currency
    logger.warning(f"Using ad account currency '{fallback}' as fallback")
    return fallback


def _apply_currency_conversion(
        insights: List[Dict],
        ad_account_currency: str,
        user_currency: str,
        CurrencyService,
) -> List[Dict]:
    currency_fields = ["spend", "cpc", "cpm", "cpp"]

    if user_currency == ad_account_currency:
        for insight in insights:
            insight["currency"] = user_currency
            insight["original_currency"] = ad_account_currency
        logger.info(f"Same currency ({user_currency}), no conversion needed")
        return insights

    for insight in insights:
        conversion_ok = True
        for field in currency_fields:
            if field in insight and insight[field] is not None:
                try:
                    original = float(insight[field])
                    insight[field] = CurrencyService.convert(
                        amount=original,
                        from_currency=ad_account_currency,
                        to_currency=user_currency,
                    )
                    insight[f"{field}_original"] = original
                except (ValueError, TypeError) as e:
                    logger.warning(f"Failed to convert {field}: {e}")
                    conversion_ok = False

        insight["currency"] = user_currency if conversion_ok else ad_account_currency
        insight["original_currency"] = ad_account_currency

    logger.info(f"Converted insights {ad_account_currency} -> {user_currency}")
    return insights