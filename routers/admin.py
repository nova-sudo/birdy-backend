import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from core.constants import META_CACHE_PRESETS
from core.database import DB_NAME
from core.utils import preset_date_bounds
from dependencies import get_mongo_client, get_current_user
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

router = APIRouter()


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
                            "onNull":  {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                        }},
                        {"$dateFromString": {"dateString": f"{start_iso}T00:00:00Z"}},
                    ]},
                    {"$lte": [
                        {"$dateFromString": {
                            "dateString": "$lead_data.created_time",
                            "onError": {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
                            "onNull":  {"$dateFromString": {"dateString": "1970-01-01T00:00:00Z"}},
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
        {"facebook_cache": 1}
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

    # Also patch the flat legacy path so get_client_groups reads correct values
    maximum_leads = facet_doc.get("maximum", [{}])
    maximum_leads_count = maximum_leads[0]["n"] if maximum_leads else total_leads_count
    maximum_spend = (
        facebook_cache.get("maximum", {}).get("metrics", {}).get("insights", {}).get("spend", 0) or 0
    )
    maximum_cpl = round(maximum_spend / maximum_leads_count, 2) if maximum_leads_count > 0 else 0

    update_fields["facebook_cache.metrics.insights.total_leads"] = maximum_leads_count
    update_fields["facebook_cache.metrics.insights.cost_per_result"] = maximum_cpl
    update_fields["facebook_cache.metrics.insights.results"] = maximum_leads_count  # so results field also shows leads

    await client_groups_collection.update_one(
        {"id": group_id},
        {"$set": update_fields}
    )

    logger.info(f"Updated lead counts for {len(presets)} presets on group {group_id} (lifetime: {total_leads_count})")


@router.post("/api/admin/backfill-lead-counts")
async def backfill_lead_counts(current_user: str = Depends(get_current_user)):
    """One-time backfill: recalculate lead counts and CPL for all presets."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        client_groups_collection = db["client_groups"]

        groups = await client_groups_collection.find({
            "user_id": current_user,
            "meta_ad_account_id": {"$exists": True, "$ne": None}
        }).to_list(None)

        results = []
        for group in groups:
            try:
                await update_preset_lead_counts(
                    group["id"],
                    current_user,
                    mongo_client,
                )
                results.append({"group": group["name"], "status": "ok"})
            except Exception as e:
                results.append({"group": group["name"], "status": f"error: {e}"})

        return {"backfilled": len(results), "results": results}


@router.post("/api/admin/refresh-meta")
async def refresh_meta_data(current_user: str = Depends(get_current_user)):
    """
    Force re-fetch all Meta data for every client group.
    Clears the 5-minute account_data cache first, then re-fetches from Meta API
    with the latest fields (adset_id on ads, actions in insights).
    """
    from services.meta_service import fetch_meta_data_for_group, fetch_meta_all_presets_for_group

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        users_collection = db["users"]
        client_groups_collection = db["client_groups"]

        # Clear the in-DB account_data cache so fetch_meta_data_for_group hits Meta fresh
        await users_collection.update_one(
            {"user_id": current_user},
            {"$unset": {f"integrations.facebook.accounts": ""}}
        )
        logger.info(f"Cleared Meta account_data cache for user [{current_user}]")

        groups = await client_groups_collection.find(
            {
                "user_id": current_user,
                "meta_ad_account_id": {"$exists": True, "$ne": None}
            }
        ).to_list(None)

        if not groups:
            return {"status": "nothing_to_do", "message": "No groups with Meta ad accounts found."}

        token = await get_facebook_token(current_user, mongo_client)
        if not token or not token.get("access_token"):
            raise HTTPException(status_code=401, detail="No valid Facebook token found.")

        # Get ad accounts list for the fetch function
        import httpx
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"https://graph.facebook.com/v23.0/me/adaccounts",
                params={"fields": "id,name,currency,created_time", "access_token": token["access_token"]},
            )
            facebook_ad_accounts = resp.json().get("data", []) if resp.status_code == 200 else []

        results = []
        for group in groups:
            group_name = group.get("name", "Unknown")
            meta_ad_account_id = group["meta_ad_account_id"]

            try:
                # Re-fetch the base account_data (campaigns/adsets/ads with new fields)
                result = await fetch_meta_data_for_group(
                    meta_ad_account_id, current_user, mongo_client, facebook_ad_accounts
                )

                # Also refresh all preset caches
                ad_account_currency = group.get("ad_account_currency")
                await fetch_meta_all_presets_for_group(
                    group["id"], meta_ad_account_id, current_user, mongo_client, ad_account_currency
                )

                results.append({"group": group_name, "status": "ok"})
                logger.info(f"Refreshed Meta data for group '{group_name}'")
            except Exception as e:
                results.append({"group": group_name, "status": "error", "detail": str(e)})
                logger.error(f"Refresh failed for group '{group_name}': {e}")

            await asyncio.sleep(2)

        ok_count = sum(1 for r in results if r["status"] == "ok")
        err_count = sum(1 for r in results if r["status"] == "error")

        return {
            "status": "complete",
            "total": len(groups),
            "succeeded": ok_count,
            "failed": err_count,
            "details": results,
        }
