"""
services/meta_service.py
------------------------
Meta (Facebook) helper functions extracted from main.py.
Handles fetching, caching, and currency conversion of Meta ad data.
"""

import asyncio
import json
import logging
import os
import time

import httpx
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import HTTPException

from core.database import DB_NAME
from core.constants import META_CACHE_PRESETS, PRESET_ALIAS, GHL_PRESET_DATE_RANGE
from core.utils import get_result_value, preset_date_bounds
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)


class MetaAuthError(Exception):
    """
    Raised when Meta's Graph API returns a code-190 (invalid/expired token)
    or code-200 (missing ads_read/ads_management scope) error.

    A distinct exception type — not `Exception` — because both this file
    and services/meta_refresh_manager.py wrap their fetch call sites in
    catch-all `except Exception:` blocks that would otherwise swallow the
    auth signal and let the refresh march on returning empty "success"
    results. Callers should catch this explicitly (or re-raise it past
    generic handlers) so downstream circuit-breaker logic in the refresh
    manager can flag the group with meta_token_error=True and stop
    re-enqueueing it on every 5h cron cycle.
    """
    pass


# ---------------------------------------------------------------------------
# Low-level cache helpers
# ---------------------------------------------------------------------------

async def save_facebook_data(
    user_id: str,
    account_id: str,
    data: dict,
    data_type: str,
    mongo_client: AsyncIOMotorClient,
):
    try:
        data_doc = {
            "data": data,
            "updated_at": datetime.now(),
            "created_at": datetime.now(),
        }
        db = mongo_client[DB_NAME]
        await db["users"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    f"integrations.facebook.accounts.{account_id}.{data_type}": data_doc,
                    "updated_at": datetime.now(),
                }
            },
            upsert=True,
        )
        logger.info(f"Saved Meta {data_type} data for account {account_id} for user: {user_id}")
    except Exception as e:
        logger.error(f"Failed to save Meta {data_type} data for account {account_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save {data_type} data: {str(e)}")


async def get_facebook_data(
    user_id: str,
    account_id: str,
    data_type: str,
    mongo_client: AsyncIOMotorClient,
):
    db = mongo_client[DB_NAME]
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


# ---------------------------------------------------------------------------
# fetch_meta_data_for_group  (legacy single-preset fetcher)
# ---------------------------------------------------------------------------

async def fetch_meta_data_for_group(
    meta_ad_account_id: str,
    current_user: str,
    mongo_client,
    facebook_ad_accounts: list,
):
    """
    Fetch Meta data for a single group in parallel.
    NOW SAVES: campaigns, adsets, ads, leads, AND summary metrics
    """
    try:
        account = next(
            (acc for acc in facebook_ad_accounts if acc["id"] == meta_ad_account_id),
            {},
        )

        # Try cache first (5 minutes)
        account_data = await get_facebook_data(
            current_user, meta_ad_account_id, "account_data", mongo_client
        )

        # If no cache or cache expired, fetch fresh data
        if not account_data:
            token = await get_facebook_token(current_user, mongo_client)
            if token and token.get("access_token"):
                async with httpx.AsyncClient(timeout=300.0) as client:
                    response = await client.get(
                        f"https://graph.facebook.com/v25.0/{meta_ad_account_id}/campaigns",
                        params={
                            "fields": "name,status,insights.date_preset(maximum){actions,attribution_setting,spend,results,reach,frequency,cost_per_result,impressions,cpm,clicks,cpc,ctr},adsets{name,status,insights.date_preset(maximum){actions,attribution_setting,spend,results,reach,frequency,impressions,cpm,clicks,cpc,ctr}},ads{name,adset_id,status,creative{title,body,image_url},insights.date_preset(maximum){actions,attribution_setting,results,reach,frequency,spend,quality_ranking,engagement_rate_ranking,conversion_rate_ranking,impressions,cpm,inline_link_clicks,cpc,clicks}}",
                            "access_token": token["access_token"],
                        },
                    )
                    if response.status_code == 200:
                        account_data = response.json()
                        await save_facebook_data(
                            current_user,
                            meta_ad_account_id,
                            account_data,
                            "account_data",
                            mongo_client,
                        )
                    else:
                        logger.warning(f"Meta API error for {meta_ad_account_id}: {response.status_code}")
                        return None

        # Get leads count
        leads = await get_facebook_data(
            current_user, meta_ad_account_id, "leads", mongo_client
        )

        leads_count = 0
        if not leads:
            token = await get_facebook_token(current_user, mongo_client)
            if token and token.get("access_token"):
                async with httpx.AsyncClient(timeout=300.0) as client:
                    response = await client.get(
                        f"https://graph.facebook.com/v25.0/{meta_ad_account_id}/campaigns",
                        params={
                            "fields": "ads{leads.date_preset(maximum){id}}",
                            "access_token": token["access_token"],
                        },
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

        campaigns_list = []
        adsets_list = []
        ads_list = []

        total_spend = 0.0
        total_impressions = 0
        total_clicks = 0
        total_reach = 0
        total_results = 0

        for campaign in campaigns:
            # PROCESS CAMPAIGN
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
                "cpm": round((campaign_spend / campaign_impressions * 1000), 2) if campaign_impressions > 0 else 0,
                "cpc": round((campaign_spend / campaign_clicks), 2) if campaign_clicks > 0 else 0,
                "ctr": round((campaign_clicks / campaign_impressions * 100), 2) if campaign_impressions > 0 else 0,
                "leads": campaign_results,
            })

            # PROCESS ADSETS
            for adset in campaign.get("adsets", {}).get("data", []):
                adset_insights = adset.get("insights", {}).get("data", [])
                adset_spend = 0
                adset_impressions = 0
                adset_clicks = 0
                adset_reach = 0
                adset_results = 0

                if adset_insights:
                    insight = adset_insights[0]
                    try:
                        adset_spend = float(insight.get("spend", 0) or 0)
                        adset_impressions = int(insight.get("impressions", 0) or 0)
                        adset_clicks = int(insight.get("clicks", 0) or 0)
                        adset_reach = int(insight.get("reach", 0) or 0)
                        adset_results = get_result_value(adset_insights, "lead")
                    except (ValueError, TypeError):
                        pass

                adsets_list.append({
                    "id": adset.get("id"),
                    "name": adset.get("name"),
                    "campaign_id": campaign.get("id"),
                    "status": adset.get("status", "").title(),
                    "spend": round(adset_spend, 2),
                    "impressions": adset_impressions,
                    "clicks": adset_clicks,
                    "reach": adset_reach,
                    "results": adset_results,
                    "cpm": round((adset_spend / adset_impressions * 1000), 2) if adset_impressions > 0 else 0,
                    "cpc": round((adset_spend / adset_clicks), 2) if adset_clicks > 0 else 0,
                    "ctr": round((adset_clicks / adset_impressions * 100), 2) if adset_impressions > 0 else 0,
                })

            # PROCESS ADS
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
                        ad_results = get_result_value(ad_insights, "lead")
                    except (ValueError, TypeError):
                        pass

                creative = ad.get("creative", {})
                ads_list.append({
                    "id": ad.get("id"),
                    "name": ad.get("name"),
                    "campaign_id": campaign.get("id"),
                    "adset_id": ad.get("adset_id"),
                    "status": ad.get("status", "").title(),
                    "spend": round(ad_spend, 2),
                    "impressions": ad_impressions,
                    "clicks": ad_clicks,
                    "reach": ad_reach,
                    "results": ad_results,
                    "cpm": round((ad_spend / ad_impressions * 1000), 2) if ad_impressions > 0 else 0,
                    "cpc": round((ad_spend / ad_clicks), 2) if ad_clicks > 0 else 0,
                    "ctr": round((ad_clicks / ad_impressions * 100), 2) if ad_impressions > 0 else 0,
                    "creative_title": creative.get("title", ""),
                    "creative_body": creative.get("body", ""),
                    "creative_image": creative.get("image_url", ""),
                })

        # Calculate derived metrics
        avg_cpm = (total_spend / total_impressions * 1000) if total_impressions > 0 else 0
        avg_cpc = (total_spend / total_clicks) if total_clicks > 0 else 0
        avg_ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0
        cost_per_lead = (total_spend / leads_count) if leads_count > 0 else 0

        logger.info(
            f"Meta data for {meta_ad_account_id}: "
            f"{len(campaigns_list)} campaigns, {len(adsets_list)} adsets, {len(ads_list)} ads, "
            f"${total_spend:.2f} spend, {leads_count} leads"
        )

        return {
            "ad_account_id": meta_ad_account_id,
            "name": account.get("name", "Unknown Ad Account"),
            "currency": account.get("currency"),
            "created_time": account.get("created_time"),
            "campaigns": campaigns_list,
            "adsets": adsets_list,
            "ads": ads_list,
            "leads": leads if leads else [],
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
                    "cost_per_result": round(cost_per_lead, 2),
                },
            },
        }

    except Exception as e:
        logger.error(f"Error fetching Meta data for {meta_ad_account_id}: {str(e)}", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# fetch_and_cache_meta_data  (legacy full-cache with currency conversion)
# ---------------------------------------------------------------------------

async def fetch_and_cache_meta_data(
    group_id: str,
    meta_ad_account_id: str,
    user_id: str,
    mongo_client,
    ad_account_currency: str | None = None,
):
    """
    Fetch Meta data and save FULL details to cache with currency conversion.
    Converts all monetary values from ad account currency to user's default currency.
    """
    try:
        from utils.currency_exchange import CurrencyService

        db = mongo_client[DB_NAME]
        client_groups_collection = db["client_groups"]

        # Get user's default currency with fallback
        user_currency = None
        try:
            user_currency = await CurrencyService.get_user_currency(user_id)
            logger.info(
                f"User currency: {user_currency}, Ad account currency: {ad_account_currency}"
            )
        except (ValueError, RuntimeError) as e:
            logger.warning(
                f"Could not get user currency: {e}. "
                f"Using ad account currency {ad_account_currency} as fallback."
            )
            user_currency = ad_account_currency

        # Get Facebook ad accounts
        facebook_ad_accounts_data = await get_facebook_data(
            user_id, "global", "adaccounts", mongo_client
        )
        facebook_ad_accounts = facebook_ad_accounts_data.get("data", []) if facebook_ad_accounts_data else []

        # Fetch Meta data (now includes campaigns, adsets, ads)
        meta_data = await fetch_meta_data_for_group(
            meta_ad_account_id, user_id, mongo_client, facebook_ad_accounts
        )

        if not meta_data:
            logger.warning(f"Failed to fetch Meta data for {meta_ad_account_id}")
            return
        if not ad_account_currency:
            ad_account_currency = meta_data.get("currency")

        # ---- CONVERT CURRENCY in meta_data ----

        def convert_spend_fields(item):
            """Convert spend-related fields in a single item (campaign/adset/ad)"""
            if not item:
                return item

            converted_item = item.copy()
            spend_fields = [
                "spend", "daily_budget", "lifetime_budget",
                "budget_remaining", "cost_per_result", "cpm", "cpc", "cpp",
            ]

            for field in spend_fields:
                if field in converted_item and converted_item[field] is not None:
                    try:
                        original_amount = float(converted_item[field])
                        if user_currency != ad_account_currency:
                            converted_amount = CurrencyService.convert(
                                amount=original_amount,
                                from_currency=ad_account_currency,
                                to_currency=user_currency,
                            )
                            converted_item[field] = converted_amount
                            converted_item[f"{field}_original"] = original_amount
                            logger.debug(
                                f"Converted {field}: {original_amount:.2f} {ad_account_currency} "
                                f"-> {converted_amount:.2f} {user_currency}"
                            )
                        else:
                            converted_item[field] = original_amount
                            logger.debug(f"No conversion needed for {field} (same currency)")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Failed to convert {field} value {converted_item[field]}: {e}")

            return converted_item

        # Track totals for logging
        total_campaigns = 0
        total_adsets = 0
        total_ads = 0
        total_spend_before = 0.0
        total_spend_after = 0.0

        # Convert campaigns
        if "campaigns" in meta_data and meta_data["campaigns"]:
            converted_campaigns = []
            for campaign in meta_data["campaigns"]:
                if "spend" in campaign and campaign["spend"] is not None:
                    try:
                        total_spend_before += float(campaign["spend"])
                    except (ValueError, TypeError):
                        pass

                converted_campaign = convert_spend_fields(campaign)
                converted_campaigns.append(converted_campaign)

                if "spend" in converted_campaign and converted_campaign["spend"] is not None:
                    try:
                        total_spend_after += float(converted_campaign["spend"])
                    except (ValueError, TypeError):
                        pass

            meta_data["campaigns"] = converted_campaigns
            total_campaigns = len(converted_campaigns)
            logger.info(
                f"Converted currency for {total_campaigns} campaigns. "
                f"Total spend: {total_spend_before:.2f} {ad_account_currency} -> "
                f"{total_spend_after:.2f} {user_currency}"
            )

        # Convert adsets
        if "adsets" in meta_data and meta_data["adsets"]:
            converted_adsets = []
            adset_spend_before = 0.0
            adset_spend_after = 0.0

            for adset in meta_data["adsets"]:
                if "spend" in adset and adset["spend"] is not None:
                    try:
                        adset_spend_before += float(adset["spend"])
                    except (ValueError, TypeError):
                        pass

                converted_adset = convert_spend_fields(adset)
                converted_adsets.append(converted_adset)

                if "spend" in converted_adset and converted_adset["spend"] is not None:
                    try:
                        adset_spend_after += float(converted_adset["spend"])
                    except (ValueError, TypeError):
                        pass

            meta_data["adsets"] = converted_adsets
            total_adsets = len(converted_adsets)
            logger.info(
                f"Converted currency for {total_adsets} adsets. "
                f"Total spend: {adset_spend_before:.2f} {ad_account_currency} -> "
                f"{adset_spend_after:.2f} {user_currency}"
            )

        # Convert ads
        if "ads" in meta_data and meta_data["ads"]:
            converted_ads = []
            ad_spend_before = 0.0
            ad_spend_after = 0.0

            for ad in meta_data["ads"]:
                if "spend" in ad and ad["spend"] is not None:
                    try:
                        ad_spend_before += float(ad["spend"])
                    except (ValueError, TypeError):
                        pass

                converted_ad = convert_spend_fields(ad)
                converted_ads.append(converted_ad)

                if "spend" in converted_ad and converted_ad["spend"] is not None:
                    try:
                        ad_spend_after += float(converted_ad["spend"])
                    except (ValueError, TypeError):
                        pass

            meta_data["ads"] = converted_ads
            total_ads = len(converted_ads)
            logger.info(
                f"Converted currency for {total_ads} ads. "
                f"Total spend: {ad_spend_before:.2f} {ad_account_currency} -> "
                f"{ad_spend_after:.2f} {user_currency}"
            )

        # Convert nested metrics.insights monetary fields
        if meta_data.get("metrics"):
            converted_metrics = meta_data["metrics"].copy()
            insights = converted_metrics.get("insights")
        if isinstance(insights, dict):
            converted_metrics["insights"] = convert_spend_fields(insights)
            meta_data["metrics"] = converted_metrics
            logger.info("Converted currency for metrics.insights")

        # Add currency metadata to meta_data
        meta_data["currency"] = user_currency
        meta_data["original_currency"] = ad_account_currency

        # SAVE FULL DATA
        update_result = await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "facebook_cache": meta_data,
                    "last_meta_refresh": datetime.utcnow(),
                }
            },
        )

        logger.info(
            f"Cached Meta data for group {group_id} in {user_currency} "
            f"(matched: {update_result.matched_count}, modified: {update_result.modified_count}): "
            f"{total_campaigns} campaigns, {total_adsets} adsets, {total_ads} ads"
        )

    except Exception as e:
        logger.error(f"Error caching Meta data: {e}", exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Preset fetching (single + batched)
# ---------------------------------------------------------------------------

def _campaigns_fields_for_preset(date_preset: str) -> str:
    """
    Meta Graph API `fields` param for the /act/campaigns endpoint that
    _fetch_meta_campaigns_for_preset and _fetch_meta_presets_batch both
    use. Nested `insights.date_preset(...)` gives aggregated per-preset
    metrics at campaign/adset/ad levels in one request.
    """
    return (
        "name,status,"
        f"insights.date_preset({date_preset})"
        "{actions,spend,results,reach,impressions,cpm,clicks,cpc,ctr},"
        "adsets{name,status,"
        f"insights.date_preset({date_preset})"
        "{actions,spend,results,reach,impressions,cpm,clicks,cpc,ctr}},"
        "ads{name,adset_id,status,creative{title,body,image_url},"
        f"insights.date_preset({date_preset})"
        "{actions,results,reach,spend,impressions,cpm,inline_link_clicks,cpc,clicks}}"
    )


def _classify_meta_error(status_code: int, body: str) -> str | None:
    """
    Return "auth", "rate_limit", or None. Shared error classification for
    single and batched Meta responses so both paths interpret the same
    codes identically.
    """
    is_auth = (
        (status_code == 400
         and ('"code":190' in body or "not a confirmed user" in body
              or "not allowed" in body))
        or (status_code == 403
            and ('"code":200' in body or "ads_management" in body
                 or "ads_read" in body))
    )
    if is_auth:
        return "auth"

    is_rate_limit = (
        status_code == 429
        or (status_code == 400 and '"code":17' in body)
        or (status_code == 403 and '"code":4' in body)
        or "Application request limit reached" in body
    )
    if is_rate_limit:
        return "rate_limit"

    return None


def _rate_limited_result(date_preset: str) -> dict:
    return {
        "_rate_limited": True,
        "campaigns": [],
        "adsets": [],
        "ads": [],
        "date_preset": date_preset,
        "metrics": {
            "total_campaigns": 0,
            "total_adsets": 0,
            "total_ads": 0,
            "insights": {},
        },
    }


def _accumulate_campaigns_page(
    campaigns: list[dict],
    campaigns_list: list[dict],
    adsets_list: list[dict],
    ads_list: list[dict],
    totals: dict,
) -> None:
    """
    Parse one page of Meta `/campaigns?fields=name,status,insights,adsets,ads`
    into the flat campaigns/adsets/ads lists and running totals. Shared by
    the single-preset fetcher and the batch subresponse parser.
    """
    for campaign in campaigns:
        c_insights = campaign.get("insights", {}).get("data", [])
        c_spend = c_imp = c_clicks = c_reach = c_results = 0

        if c_insights:
            ins = c_insights[0]
            try:
                c_spend = float(ins.get("spend", 0) or 0)
                c_imp = int(ins.get("impressions", 0) or 0)
                c_clicks = int(ins.get("clicks", 0) or 0)
                c_reach = int(ins.get("reach", 0) or 0)
                c_results = get_result_value(c_insights, "lead")
            except (ValueError, TypeError):
                pass

        totals["spend"] += c_spend
        totals["impressions"] += c_imp
        totals["clicks"] += c_clicks
        totals["reach"] += c_reach
        totals["results"] += c_results

        campaigns_list.append({
            "id": campaign.get("id"),
            "name": campaign.get("name"),
            "status": campaign.get("status", "").title(),
            "spend": round(c_spend, 2),
            "impressions": c_imp,
            "clicks": c_clicks,
            "reach": c_reach,
            "results": c_results,
            "cpm": round(c_spend / c_imp * 1000, 2) if c_imp > 0 else 0,
            "cpc": round(c_spend / c_clicks, 2) if c_clicks > 0 else 0,
            "ctr": round(c_clicks / c_imp * 100, 2) if c_imp > 0 else 0,
        })

        for adset in campaign.get("adsets", {}).get("data", []):
            a_ins = adset.get("insights", {}).get("data", [])
            a_spend = a_imp = a_clicks = a_reach = a_results = 0
            if a_ins:
                ins = a_ins[0]
                try:
                    a_spend = float(ins.get("spend", 0) or 0)
                    a_imp = int(ins.get("impressions", 0) or 0)
                    a_clicks = int(ins.get("clicks", 0) or 0)
                    a_reach = int(ins.get("reach", 0) or 0)
                    a_results = get_result_value(a_ins, "lead")
                except (ValueError, TypeError):
                    pass
            adsets_list.append({
                "id": adset.get("id"),
                "name": adset.get("name"),
                "campaign_id": campaign.get("id"),
                "status": adset.get("status", "").title(),
                "spend": round(a_spend, 2),
                "impressions": a_imp,
                "clicks": a_clicks,
                "reach": a_reach,
                "results": a_results,
                "cpm": round(a_spend / a_imp * 1000, 2) if a_imp > 0 else 0,
                "cpc": round(a_spend / a_clicks, 2) if a_clicks > 0 else 0,
                "ctr": round(a_clicks / a_imp * 100, 2) if a_imp > 0 else 0,
            })

        for ad in campaign.get("ads", {}).get("data", []):
            ad_ins = ad.get("insights", {}).get("data", [])
            ad_spend = ad_imp = ad_clicks = ad_reach = ad_results = 0
            if ad_ins:
                ins = ad_ins[0]
                try:
                    ad_spend = float(ins.get("spend", 0) or 0)
                    ad_imp = int(ins.get("impressions", 0) or 0)
                    ad_clicks = int(ins.get("clicks", 0) or 0)
                    ad_reach = int(ins.get("reach", 0) or 0)
                    ad_results = get_result_value(ad_ins, "lead")
                except (ValueError, TypeError):
                    pass
            creative = ad.get("creative", {})
            ads_list.append({
                "id": ad.get("id"),
                "name": ad.get("name"),
                "campaign_id": campaign.get("id"),
                "adset_id": ad.get("adset_id"),
                "status": ad.get("status", "").title(),
                "spend": round(ad_spend, 2),
                "impressions": ad_imp,
                "clicks": ad_clicks,
                "reach": ad_reach,
                "results": ad_results,
                "cpm": round(ad_spend / ad_imp * 1000, 2) if ad_imp > 0 else 0,
                "cpc": round(ad_spend / ad_clicks, 2) if ad_clicks > 0 else 0,
                "ctr": round(ad_clicks / ad_imp * 100, 2) if ad_imp > 0 else 0,
                "creative_title": creative.get("title", ""),
                "creative_body": creative.get("body", ""),
                "creative_image": creative.get("image_url", ""),
            })


def _finalize_preset_result(
    date_preset: str,
    campaigns_list: list[dict],
    adsets_list: list[dict],
    ads_list: list[dict],
    totals: dict,
) -> dict:
    """
    Package the accumulated lists into the same dict shape callers of
    _fetch_meta_campaigns_for_preset expect.
    """
    total_spend = totals["spend"]
    total_impressions = totals["impressions"]
    total_clicks = totals["clicks"]
    total_reach = totals["reach"]
    total_results = totals["results"]

    avg_cpm = round(total_spend / total_impressions * 1000, 2) if total_impressions > 0 else 0
    avg_cpc = round(total_spend / total_clicks, 2) if total_clicks > 0 else 0
    avg_ctr = round(total_clicks / total_impressions * 100, 2) if total_impressions > 0 else 0

    return {
        "campaigns": campaigns_list,
        "adsets": adsets_list,
        "ads": ads_list,
        "date_preset": date_preset,
        "metrics": {
            "total_campaigns": len(campaigns_list),
            "total_adsets": len(adsets_list),
            "total_ads": len(ads_list),
            "insights": {
                "spend": round(total_spend, 2),
                "impressions": total_impressions,
                "clicks": total_clicks,
                "reach": total_reach,
                "results": total_results,
                "cpm": avg_cpm,
                "cpc": avg_cpc,
                "ctr": avg_ctr,
                "cost_per_result": round(total_spend / total_results, 2) if total_results > 0 else 0,
            },
        },
    }


async def _fetch_meta_campaigns_for_preset(
    ad_account_id: str,
    access_token: str,
    date_preset: str,
) -> dict:
    """
    Fetch campaigns + aggregated metrics from Meta for one date_preset.
    Kept as a fallback path when the batched fetcher is unavailable or
    a specific preset needs a one-off fetch.
    """

    fields = _campaigns_fields_for_preset(date_preset)

    campaigns_list: list[dict] = []
    adsets_list: list[dict] = []
    ads_list: list[dict] = []
    totals = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            url = f"https://graph.facebook.com/v25.0/{ad_account_id}/campaigns"
            params = {"fields": fields, "access_token": access_token, "limit": 100}
            next_url = None
            page = 0

            while True:
                page += 1
                if page == 1:
                    resp = await client.get(url, params=params)
                else:
                    resp = await client.get(next_url)

                if resp.status_code != 200:
                    body = resp.text[:300]
                    kind = _classify_meta_error(resp.status_code, body)
                    if kind == "auth":
                        logger.error(
                            f"Meta API auth/permission error for preset={date_preset} "
                            f"account={ad_account_id}: {body}"
                        )
                        raise MetaAuthError(
                            f"Facebook auth/permission error for account {ad_account_id}: {body[:200]}"
                        )

                    logger.warning(
                        f"Meta API {resp.status_code} for preset={date_preset} "
                        f"account={ad_account_id}: {body}"
                    )
                    if kind == "rate_limit":
                        return _rate_limited_result(date_preset)
                    break

                data = resp.json()
                _accumulate_campaigns_page(
                    data.get("data", []) or [],
                    campaigns_list, adsets_list, ads_list, totals,
                )

                next_url = data.get("paging", {}).get("next")
                if not next_url:
                    break
                await asyncio.sleep(0.15)

    except MetaAuthError:
        # Propagate — refresh manager's execute_refresh handles this,
        # sets meta_token_error=True on the group, and stops the retry loop.
        raise
    except Exception as e:
        logger.error(
            f"Error fetching Meta preset={date_preset} for {ad_account_id}: {e}",
            exc_info=True,
        )

    return _finalize_preset_result(date_preset, campaigns_list, adsets_list, ads_list, totals)


# ---------------------------------------------------------------------------
# _fetch_meta_presets_batch  (all presets in ONE HTTP call via Meta Batch API)
# ---------------------------------------------------------------------------

async def _fetch_meta_presets_batch(
    ad_account_id: str,
    access_token: str,
    presets: list[str],
) -> dict[str, dict]:
    """
    Fetch all `presets` in ONE HTTP request via Meta's Batch API. Returns
    {preset: preset_result_dict} — each value has the same shape as
    _fetch_meta_campaigns_for_preset would return.

    Rationale (fix #3 of the Meta reliability audit): the previous path
    called _fetch_meta_campaigns_for_preset in a serial loop, 13× per
    group per refresh. Meta Batch API accepts up to 50 subrequests in one
    POST and executes them server-side, cutting wall-clock from ~13× the
    per-preset latency to ~2× (spike-measured ~5.3× win on a real account).

    Rate-limit budget is unchanged — each subrequest still counts toward
    the ad-account quota — but connection overhead and coordination drop.

    A preset that comes back rate-limited is returned as
    {"_rate_limited": True, ...} the same way the single-preset path
    reports it, so the refresh manager's partial-job reclaim still works.

    Auth errors (code 190/200) raise MetaAuthError, matching the
    single-preset contract and letting the circuit-breaker trigger.

    Pagination: each subrequest uses limit=500 for the /campaigns edge.
    Groups with >500 campaigns per ad account fall back to the single-preset
    paginating path (caller responsibility — this function does NOT follow
    `paging.next` links inside batch subresponses).
    """
    if not presets:
        return {}

    # Meta batch cap is 50; we send 13 so this is theoretical, but guard anyway.
    if len(presets) > 50:
        raise ValueError(f"Meta batch cap is 50 subrequests, got {len(presets)}")

    batch = [
        {
            "method": "GET",
            "relative_url": (
                f"{ad_account_id}/campaigns"
                f"?fields={_campaigns_fields_for_preset(p)}"
                f"&limit=500"
            ),
        }
        for p in presets
    ]

    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            "https://graph.facebook.com/v25.0/",
            data={
                "access_token": access_token,
                "batch": json.dumps(batch),
            },
        )

    if resp.status_code != 200:
        body = resp.text[:400]
        kind = _classify_meta_error(resp.status_code, body)
        if kind == "auth":
            logger.error(
                f"Meta batch API auth/permission error for account={ad_account_id}: {body}"
            )
            raise MetaAuthError(
                f"Facebook auth/permission error for account {ad_account_id}: {body[:200]}"
            )
        # Any other non-200 on the outer batch envelope is a hard failure —
        # the caller falls back to the serial per-preset path.
        raise RuntimeError(
            f"Meta batch API HTTP {resp.status_code} for {ad_account_id}: {body}"
        )

    subresponses = resp.json()
    if not isinstance(subresponses, list) or len(subresponses) != len(presets):
        raise RuntimeError(
            f"Meta batch API returned {len(subresponses) if isinstance(subresponses, list) else '<not-a-list>'} "
            f"subresponses, expected {len(presets)}"
        )

    out: dict[str, dict] = {}
    for preset, sr in zip(presets, subresponses):
        # `None` subresponse can happen when a batch item times out server-side
        if sr is None:
            logger.warning(
                f"Meta batch subresponse for preset={preset} account={ad_account_id} "
                f"was null (server-side timeout) — marking rate-limited so the "
                f"refresh manager retries just this preset."
            )
            out[preset] = _rate_limited_result(preset)
            continue

        sub_code = sr.get("code", 0)
        sub_body_str = sr.get("body", "") or ""

        if sub_code != 200:
            kind = _classify_meta_error(sub_code, sub_body_str)
            if kind == "auth":
                logger.error(
                    f"Meta batch subresponse auth error for preset={preset} "
                    f"account={ad_account_id}: {sub_body_str[:300]}"
                )
                raise MetaAuthError(
                    f"Facebook auth/permission error for account {ad_account_id}: {sub_body_str[:200]}"
                )
            logger.warning(
                f"Meta batch subresponse HTTP {sub_code} for preset={preset} "
                f"account={ad_account_id}: {sub_body_str[:300]}"
            )
            if kind == "rate_limit":
                out[preset] = _rate_limited_result(preset)
            else:
                # Unknown non-200 (e.g. code=1 "reduce data"): mark as
                # rate-limited so partial-job reclaim retries this preset
                # only, without failing the whole group.
                out[preset] = _rate_limited_result(preset)
            continue

        try:
            body = json.loads(sub_body_str) if sub_body_str else {}
        except json.JSONDecodeError as e:
            logger.error(
                f"Meta batch subresponse body not JSON for preset={preset}: {e} "
                f"(first 200 chars: {sub_body_str[:200]})"
            )
            out[preset] = _rate_limited_result(preset)
            continue

        campaigns_list: list[dict] = []
        adsets_list: list[dict] = []
        ads_list: list[dict] = []
        totals = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0}
        _accumulate_campaigns_page(
            body.get("data", []) or [],
            campaigns_list, adsets_list, ads_list, totals,
        )

        # If the ad account has >500 campaigns this preset has a paging.next.
        # Fall back to the paginating single-preset fetcher for just this
        # preset so we don't silently truncate data.
        if body.get("paging", {}).get("next"):
            logger.info(
                f"Meta batch subresponse for preset={preset} has paging.next "
                f"(>500 campaigns) — refetching via the paginating single-preset "
                f"path to avoid truncation."
            )
            out[preset] = await _fetch_meta_campaigns_for_preset(
                ad_account_id, access_token, preset,
            )
            continue

        out[preset] = _finalize_preset_result(
            preset, campaigns_list, adsets_list, ads_list, totals,
        )

    return out


# ---------------------------------------------------------------------------
# fetch_meta_all_presets_for_group
# ---------------------------------------------------------------------------

async def fetch_meta_all_presets_for_group(
    group_id: str,
    meta_ad_account_id: str,
    user_id: str,
    mongo_client,
    ad_account_currency: str | None = None,
    presets: list | None = None,
):
    """
    Fetch ALL Meta date presets for a client group and store each one as
    facebook_cache.<preset_key> in MongoDB.

    Structure saved to DB:
        facebook_cache: {
            "data_maximum": { campaigns, adsets, ads, metrics, ... },
            "today":        { campaigns, adsets, ads, metrics, ... },
            ...
            "name":         "Ad Account Name",
            "currency":     "USD",
            "ad_account_id": "act_xxx",
            "total_leads":  123,
        }
    """
    from utils.currency_exchange import CurrencyService

    if presets is None:
        presets = META_CACHE_PRESETS

    db = mongo_client[DB_NAME]
    client_groups_collection = db["client_groups"]

    # -- get currencies --
    try:
        user_currency = await CurrencyService.get_user_currency(user_id)
    except Exception:
        user_currency = ad_account_currency

    if not ad_account_currency:
        grp = await client_groups_collection.find_one({"id": group_id}, {"ad_account_currency": 1})
        ad_account_currency = (grp or {}).get("ad_account_currency")

    # -- token --
    token = await get_facebook_token(user_id, mongo_client)
    if not token or not token.get("access_token"):
        logger.warning(f"No Facebook token for user {user_id}, skipping Meta preset fetch")
        return
    access_token = token["access_token"]

    # -- currency converter helper --
    def _convert(item: dict) -> dict:
        if not item or not ad_account_currency or not user_currency:
            return item
        if ad_account_currency == user_currency:
            return item
        spend_fields = ["spend", "cpm", "cpc", "cpp", "cost_per_result"]
        out = item.copy()
        for f in spend_fields:
            if f in out and out[f] is not None:
                try:
                    out[f] = CurrencyService.convert(
                        float(out[f]),
                        from_currency=ad_account_currency,
                        to_currency=user_currency,
                    )
                except Exception:
                    pass
        return out

    # -- fetch all presets via Meta Batch API (one HTTP call) --
    #
    # Previous implementation looped serially over `presets`, making 13
    # HTTP round-trips per group per refresh (each ~3s → ~40s wall-clock
    # per group). Meta's Batch API accepts up to 50 subrequests in one
    # POST, executes them server-side, and returns an array of subresponses.
    # Same rate-limit consumption, ~5-6× wall-clock win measured in the
    # spike (see scripts/spike_meta_flat_insights.py).
    #
    # If the batch envelope itself fails (non-auth 5xx, network glitch,
    # etc.), fall back to the serial per-preset loop with retries so a
    # transient batch failure doesn't zero out a refresh cycle.
    logger.info(
        f"Fetching {len(presets)} Meta presets for group {group_id} "
        f"(account {meta_ad_account_id}) via batch API"
    )

    preset_data: dict[str, dict] = {}

    try:
        batch_results = await _fetch_meta_presets_batch(
            meta_ad_account_id, access_token, list(presets)
        )
    except MetaAuthError:
        # Circuit breaker — refresh manager sets meta_token_error=True
        # and stops re-enqueueing. Don't retry against a dead token.
        raise
    except Exception as e:
        logger.warning(
            f"Meta batch API failed for group {group_id} ({e}); "
            f"falling back to serial per-preset loop"
        )
        batch_results = {}

    presets_needing_retry = [p for p in presets if p not in batch_results]

    # Ingest batch results, currency-convert, log
    for preset, result in batch_results.items():
        if result.get("_rate_limited"):
            # The refresh manager's partial-job reclaim picks this up and
            # retries just this preset without re-running the whole group.
            presets_needing_retry.append(preset)
            continue

        result["campaigns"] = [_convert(c) for c in result.get("campaigns", [])]
        result["adsets"] = [_convert(a) for a in result.get("adsets", [])]
        result["ads"] = [_convert(a) for a in result.get("ads", [])]
        if result.get("metrics", {}).get("insights"):
            result["metrics"]["insights"] = _convert(result["metrics"]["insights"])

        preset_data[preset] = result
        logger.info(
            f"  Preset '{preset}': "
            f"{result['metrics']['insights'].get('spend', 0):.2f} "
            f"{user_currency or ad_account_currency} spend, "
            f"{result['metrics']['total_campaigns']} campaigns"
        )

    # Fallback / rate-limited-preset retry: serial per-preset with backoff
    for preset in presets_needing_retry:
        for attempt in range(3):
            try:
                result = await _fetch_meta_campaigns_for_preset(
                    meta_ad_account_id, access_token, preset
                )
                if result.get("_rate_limited"):
                    wait = 30 * (2 ** attempt)
                    logger.warning(
                        f"  Rate limited on preset '{preset}', "
                        f"waiting {wait}s (attempt {attempt + 1}/3)"
                    )
                    await asyncio.sleep(wait)
                    continue

                result["campaigns"] = [_convert(c) for c in result.get("campaigns", [])]
                result["adsets"] = [_convert(a) for a in result.get("adsets", [])]
                result["ads"] = [_convert(a) for a in result.get("ads", [])]
                if result.get("metrics", {}).get("insights"):
                    result["metrics"]["insights"] = _convert(result["metrics"]["insights"])

                preset_data[preset] = result
                logger.info(
                    f"  Preset '{preset}' (retry): "
                    f"{result['metrics']['insights'].get('spend', 0):.2f} "
                    f"{user_currency or ad_account_currency} spend, "
                    f"{result['metrics']['total_campaigns']} campaigns"
                )
                break

            except MetaAuthError:
                raise
            except Exception as e:
                logger.error(f"  Error fetching preset '{preset}' attempt {attempt + 1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(5 * (attempt + 1))

        await asyncio.sleep(2.0)

    # -- get ad account meta info --
    facebook_ad_accounts_data = await get_facebook_data(user_id, "global", "adaccounts", mongo_client)
    facebook_ad_accounts = (facebook_ad_accounts_data or {}).get("data", [])
    account_info = next(
        (acc for acc in facebook_ad_accounts if acc["id"] == meta_ad_account_id), {}
    )

    # -- count leads per preset window from facebook_leads collection --
    leads_collection = db["facebook_leads"]

    # Lifetime count (preset-agnostic)
    total_leads_count = await leads_collection.count_documents({
        "user_id": user_id,
        "client_group_id": group_id,
    })
    logger.info(f"Lifetime lead count for group {group_id}: {total_leads_count}")

    facet_stages = {}
    for preset_key in preset_data.keys():
        start_iso, end_iso = preset_date_bounds(preset_key)
        if start_iso is None:
            facet_stages[preset_key] = [{"$count": "n"}]
        else:
            facet_stages[preset_key] = [
                {"$match": {"$expr": {"$and": [
                    {"$gte": [
                        {"$dateFromString": {
                            "dateString": "$lead_data.created_time",
                            "onError": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                            "onNull": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                        }},
                        {"$dateFromString": {"dateString": f"{start_iso}T00:00:00Z"}},
                    ]},
                    {"$lte": [
                        {"$dateFromString": {
                            "dateString": "$lead_data.created_time",
                            "onError": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                            "onNull": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                        }},
                        {"$dateFromString": {"dateString": f"{end_iso}T23:59:59Z"}},
                    ]},
                ]}}},
                {"$count": "n"},
            ]

    if facet_stages:
        base_match = {"user_id": user_id, "client_group_id": group_id}
        pipeline = [{"$match": base_match}, {"$facet": facet_stages}]
        facet_result = await leads_collection.aggregate(pipeline).to_list(1)
        facet_doc = facet_result[0] if facet_result else {}
    else:
        facet_doc = {}

    # -- patch every preset's metrics with its windowed lead count + CPL --
    for preset_key, data in preset_data.items():
        insights = data.get("metrics", {}).get("insights", {})
        spend = insights.get("spend", 0) or 0

        bucket = facet_doc.get(preset_key, [])
        preset_leads = bucket[0]["n"] if bucket else 0

        insights["total_leads"] = preset_leads
        insights["cost_per_result"] = (
            round(spend / preset_leads, 2) if preset_leads > 0 else 0
        )
        logger.info(
            f"  Preset '{preset_key}': {preset_leads} leads, "
            f"spend={spend}, CPL={insights['cost_per_result']}"
        )

    # -- build the facebook_cache document --
    facebook_cache_update = {
        "facebook_cache.ad_account_id": meta_ad_account_id,
        "facebook_cache.name": account_info.get("name", "Unknown Ad Account"),
        "facebook_cache.currency": user_currency,
        "facebook_cache.original_currency": ad_account_currency,
        "facebook_cache.total_leads": total_leads_count,
        "last_meta_refresh": datetime.utcnow(),
        "last_meta_refresh_mode": "full_presets" if len(preset_data) >= 10 else "frequent_only",
    }

    for preset_key, data in preset_data.items():
        facebook_cache_update[f"facebook_cache.{preset_key}"] = data

    # Keep old flat structure populated from maximum for backward compat
    primary = preset_data.get("maximum") or {}
    if primary:
        facebook_cache_update["facebook_cache.metrics"] = primary.get("metrics", {})
        facebook_cache_update["facebook_cache.campaigns"] = primary.get("campaigns", [])
        facebook_cache_update["facebook_cache.adsets"] = primary.get("adsets", [])
        facebook_cache_update["facebook_cache.ads"] = primary.get("ads", [])

    await client_groups_collection.update_one(
        {"id": group_id},
        {"$set": facebook_cache_update},
    )

    logger.info(
        f"Saved {len(preset_data)} Meta preset buckets for group {group_id}"
    )


# ---------------------------------------------------------------------------
# update_preset_lead_counts
# ---------------------------------------------------------------------------

async def update_preset_lead_counts(
    group_id: str,
    user_id: str,
    mongo_client,
    presets: list | None = None,
):
    if presets is None:
        presets = META_CACHE_PRESETS

    db = mongo_client[DB_NAME]
    client_groups_collection = db["client_groups"]
    leads_collection = db["facebook_leads"]

    total_leads_count = await leads_collection.count_documents({
        "user_id": user_id,
        "client_group_id": group_id,
    })

    facet_stages = {}
    for preset_key in presets:
        start_iso, end_iso = preset_date_bounds(preset_key)
        if start_iso is None:
            facet_stages[preset_key] = [{"$count": "n"}]
        else:
            facet_stages[preset_key] = [
                {"$match": {"$expr": {"$and": [
                    {"$gte": [
                        {"$dateFromString": {
                            "dateString": "$lead_data.created_time",
                            "onError": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                            "onNull": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                        }},
                        {"$dateFromString": {"dateString": f"{start_iso}T00:00:00Z"}},
                    ]},
                    {"$lte": [
                        {"$dateFromString": {
                            "dateString": "$lead_data.created_time",
                            "onError": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                            "onNull": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                        }},
                        {"$dateFromString": {"dateString": f"{end_iso}T23:59:59Z"}},
                    ]},
                ]}}},
                {"$count": "n"},
            ]

    base_match = {"user_id": user_id, "client_group_id": group_id}
    pipeline = [{"$match": base_match}, {"$facet": facet_stages}]
    facet_result = await leads_collection.aggregate(pipeline).to_list(1)
    facet_doc = facet_result[0] if facet_result else {}

    # Fetch all current spend values in one DB read
    group_doc = await client_groups_collection.find_one(
        {"id": group_id},
        {"facebook_cache": 1},
    )
    facebook_cache = (group_doc or {}).get("facebook_cache", {})

    update_fields = {
        "facebook_cache.total_leads": total_leads_count,
        "updated_at": datetime.utcnow(),
    }

    for preset_key in presets:
        bucket = facet_doc.get(preset_key, [])
        preset_leads = bucket[0]["n"] if bucket else 0

        spend = (
            facebook_cache
            .get(preset_key, {})
            .get("metrics", {})
            .get("insights", {})
            .get("spend", 0) or 0
        )

        cpl = round(spend / preset_leads, 2) if preset_leads > 0 else 0

        update_fields[f"facebook_cache.{preset_key}.metrics.insights.total_leads"] = preset_leads
        update_fields[f"facebook_cache.{preset_key}.metrics.insights.cost_per_result"] = cpl

        logger.info(f"  Updated preset '{preset_key}': {preset_leads} leads, CPL={cpl}")

    # Also patch the flat legacy path
    maximum_leads = facet_doc.get("maximum", [{}])
    maximum_leads_count = maximum_leads[0]["n"] if maximum_leads else total_leads_count
    maximum_spend = (
        facebook_cache.get("maximum", {}).get("metrics", {}).get("insights", {}).get("spend", 0) or 0
    )
    maximum_cpl = round(maximum_spend / maximum_leads_count, 2) if maximum_leads_count > 0 else 0

    update_fields["facebook_cache.metrics.insights.total_leads"] = maximum_leads_count
    update_fields["facebook_cache.metrics.insights.cost_per_result"] = maximum_cpl
    update_fields["facebook_cache.metrics.insights.results"] = maximum_leads_count

    await client_groups_collection.update_one(
        {"id": group_id},
        {"$set": update_fields},
    )

    logger.info(f"Updated lead counts for {len(presets)} presets on group {group_id} (lifetime: {total_leads_count})")
