"""
AI tools that call the Meta Graph API directly for arbitrary date ranges.

Used when the cached presets don't cover the requested dates (e.g. "January 2025").
These are slower than the cached tools — the LLM should prefer cached data when a
matching preset exists.
"""

import asyncio
import logging

import httpx

from ai.tools.registry import registry
from ai.tools.derived_metrics import enrich
from ai.config import MAX_RESULT_ITEMS
from core.utils import get_result_value
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

META_API = "https://graph.facebook.com/v25.0"


# ── Helpers ────────────────────────────────────────────────────────────────

async def _get_token_and_groups(db, user_id, group_ids=None):
    """Get the user's Meta access token and matching client groups."""
    token_doc = await get_facebook_token(user_id, db.client)
    if not token_doc or not token_doc.get("access_token"):
        return None, [], "No Meta token found. Please connect your Meta account first."

    access_token = token_doc["access_token"]

    query = {"user_id": user_id, "meta_ad_account_id": {"$exists": True, "$ne": None}}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query, {"id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1, "_id": 0}
    ).to_list(None)

    if not groups:
        return access_token, [], "No client groups with Meta ad accounts found."

    return access_token, groups, None


async def _fetch_insights_live(ad_account_id, access_token, start_date, end_date):
    """
    Call Meta Graph API with a time_range for campaigns + nested adsets + ads.
    Returns the same shape as _fetch_meta_campaigns_for_preset but with arbitrary dates.
    """
    time_range = f'{{"since":"{start_date}","until":"{end_date}"}}'

    fields = (
        "name,status,"
        f"insights.time_range({time_range})"
        "{actions,spend,results,reach,impressions,cpm,clicks,cpc,ctr},"
        f"adsets{{name,status,insights.time_range({time_range})"
        "{actions,spend,results,reach,impressions,cpm,clicks,cpc,ctr}},"
        f"ads{{name,adset_id,status,creative{{title,body,image_url}},insights.time_range({time_range})"
        "{actions,results,reach,spend,impressions,cpm,inline_link_clicks,cpc,clicks}}"
    )

    campaigns_list = []
    adsets_list = []
    ads_list = []
    totals = {"spend": 0.0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0}

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            url = f"{META_API}/{ad_account_id}/campaigns"
            params = {"fields": fields, "access_token": access_token, "limit": 100}
            next_url = None
            page = 0

            while True:
                page += 1
                resp = await client.get(url, params=params) if page == 1 else await client.get(next_url)

                if resp.status_code != 200:
                    body = resp.text[:300]
                    if resp.status_code == 429 or '"code":17' in body or '"code":4' in body:
                        return {"error": "Meta API rate limit reached. Try again in a few minutes."}
                    if '"code":190' in body or '"code":200' in body:
                        return {"error": "Meta token expired or missing permissions. Please reconnect Meta."}
                    return {"error": f"Meta API error ({resp.status_code}): {body[:200]}"}

                data = resp.json()

                for campaign in data.get("data", []):
                    c_ins = campaign.get("insights", {}).get("data", [])
                    c_spend = c_imp = c_clicks = c_reach = c_results = 0
                    if c_ins:
                        ins = c_ins[0]
                        c_spend = float(ins.get("spend", 0) or 0)
                        c_imp = int(ins.get("impressions", 0) or 0)
                        c_clicks = int(ins.get("clicks", 0) or 0)
                        c_reach = int(ins.get("reach", 0) or 0)
                        c_results = get_result_value(c_ins, "lead")

                    totals["spend"] += c_spend
                    totals["impressions"] += c_imp
                    totals["clicks"] += c_clicks
                    totals["reach"] += c_reach
                    totals["results"] += c_results

                    campaigns_list.append({
                        "id": campaign.get("id"), "name": campaign.get("name"),
                        "status": (campaign.get("status") or "").title(),
                        "spend": round(c_spend, 2), "impressions": c_imp,
                        "clicks": c_clicks, "reach": c_reach, "results": c_results,
                        "cpm": round(c_spend / c_imp * 1000, 2) if c_imp else 0,
                        "cpc": round(c_spend / c_clicks, 2) if c_clicks else 0,
                        "ctr": round(c_clicks / c_imp * 100, 2) if c_imp else 0,
                    })

                    for adset in campaign.get("adsets", {}).get("data", []):
                        a_ins = adset.get("insights", {}).get("data", [])
                        a_spend = a_imp = a_clicks = a_reach = 0
                        if a_ins:
                            ins = a_ins[0]
                            a_spend = float(ins.get("spend", 0) or 0)
                            a_imp = int(ins.get("impressions", 0) or 0)
                            a_clicks = int(ins.get("clicks", 0) or 0)
                            a_reach = int(ins.get("reach", 0) or 0)
                        adsets_list.append({
                            "id": adset.get("id"), "name": adset.get("name"),
                            "campaign_id": campaign.get("id"),
                            "status": (adset.get("status") or "").title(),
                            "spend": round(a_spend, 2), "impressions": a_imp,
                            "clicks": a_clicks, "reach": a_reach,
                            "cpm": round(a_spend / a_imp * 1000, 2) if a_imp else 0,
                            "cpc": round(a_spend / a_clicks, 2) if a_clicks else 0,
                            "ctr": round(a_clicks / a_imp * 100, 2) if a_imp else 0,
                        })

                    for ad in campaign.get("ads", {}).get("data", []):
                        ad_ins = ad.get("insights", {}).get("data", [])
                        ad_s = ad_i = ad_c = ad_r = ad_res = 0
                        if ad_ins:
                            ins = ad_ins[0]
                            ad_s = float(ins.get("spend", 0) or 0)
                            ad_i = int(ins.get("impressions", 0) or 0)
                            ad_c = int(ins.get("clicks", 0) or 0)
                            ad_r = int(ins.get("reach", 0) or 0)
                            # results is ALWAYS an array of {action_type, value} objects
                            # — use get_result_value to extract the lead-like count
                            ad_res = get_result_value(ad_ins, "lead")
                        ads_list.append({
                            "id": ad.get("id"), "name": ad.get("name"),
                            "campaign_id": campaign.get("id"),
                            "adset_id": ad.get("adset_id"),
                            "status": (ad.get("status") or "").title(),
                            "spend": round(ad_s, 2), "impressions": ad_i,
                            "clicks": ad_c, "reach": ad_r, "results": ad_res,
                            "cpm": round(ad_s / ad_i * 1000, 2) if ad_i else 0,
                            "cpc": round(ad_s / ad_c, 2) if ad_c else 0,
                            "ctr": round(ad_c / ad_i * 100, 2) if ad_i else 0,
                        })

                next_url = data.get("paging", {}).get("next")
                if not next_url:
                    break
                await asyncio.sleep(0.15)

    except httpx.TimeoutException:
        return {"error": "Meta API request timed out. Try a shorter date range."}
    except Exception as e:
        logger.error(f"Meta live fetch error for {ad_account_id}: {e}", exc_info=True)
        return {"error": f"Meta API error: {str(e)[:200]}"}

    t = totals
    return {
        "campaigns": campaigns_list,
        "adsets": adsets_list,
        "ads": ads_list,
        "metrics": {
            "total_campaigns": len(campaigns_list),
            "total_adsets": len(adsets_list),
            "total_ads": len(ads_list),
            "insights": enrich({
                "spend": round(t["spend"], 2),
                "impressions": t["impressions"],
                "clicks": t["clicks"],
                "reach": t["reach"],
                "results": t["results"],
                "cpm": round(t["spend"] / t["impressions"] * 1000, 2) if t["impressions"] else 0,
                "cpc": round(t["spend"] / t["clicks"], 2) if t["clicks"] else 0,
                "ctr": round(t["clicks"] / t["impressions"] * 100, 2) if t["impressions"] else 0,
            }),
        },
    }


async def _fetch_leads_live(ad_account_id, access_token, start_date, end_date):
    """Fetch leads from Meta for a specific date range using filtering."""
    leads = []
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Get all ad IDs first
            url = f"{META_API}/{ad_account_id}/ads"
            params = {"fields": "id,name", "access_token": access_token, "limit": 500}
            ad_ids = []

            while True:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    break
                data = resp.json()
                ad_ids.extend([(a["id"], a.get("name", "")) for a in data.get("data", [])])
                next_url = data.get("paging", {}).get("next")
                if not next_url:
                    break
                url = next_url
                params = None
                await asyncio.sleep(0.1)

            # For each ad, fetch leads in date range
            for ad_id, ad_name in ad_ids:
                lead_url = f"{META_API}/{ad_id}/leads"
                lead_params = {
                    "fields": "id,created_time,field_data,ad_id,ad_name,campaign_id,campaign_name,adset_id,adset_name,platform",
                    "access_token": access_token,
                    "limit": 500,
                    "filtering": f'[{{"field":"time_created","operator":"GREATER_THAN","value":"{start_date}"}},{{"field":"time_created","operator":"LESS_THAN","value":"{end_date}T23:59:59"}}]',
                }

                while True:
                    resp = await client.get(lead_url, params=lead_params)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    for lead in data.get("data", []):
                        fd = {item["name"]: item["values"][0] for item in lead.get("field_data", []) if item.get("values")}
                        leads.append({
                            "lead_id": lead.get("id"),
                            "created_time": lead.get("created_time", ""),
                            "full_name": fd.get("full_name", ""),
                            "email": fd.get("email", ""),
                            "phone_number": fd.get("phone_number", fd.get("phone", "")),
                            "ad_name": lead.get("ad_name", ad_name),
                            "campaign_name": lead.get("campaign_name", ""),
                            "platform": lead.get("platform", ""),
                        })
                    next_url = data.get("paging", {}).get("next")
                    if not next_url:
                        break
                    lead_url = next_url
                    lead_params = None
                    await asyncio.sleep(0.1)

                await asyncio.sleep(0.1)

    except Exception as e:
        logger.error(f"Meta live leads fetch error: {e}", exc_info=True)
        return {"error": str(e)[:200]}

    return {"leads": leads, "total": len(leads)}


# ── Tool Executors ─────────────────────────────────────────────────────────

async def get_meta_insights_live(db, user_id, start_date, end_date, group_ids=None, level="campaign"):
    """
    Fetch campaign/adset/ad insights directly from Meta API for an arbitrary date range.
    Use when the cached presets don't cover the requested dates.
    """
    access_token, groups, err = await _get_token_and_groups(db, user_id, group_ids)
    if err and not groups:
        return {"error": err}

    all_results = []
    for g in groups:
        ad_account_id = g["meta_ad_account_id"]
        data = await _fetch_insights_live(ad_account_id, access_token, start_date, end_date)

        if "error" in data:
            all_results.append({
                "group_id": g["id"], "group_name": g["name"], "error": data["error"]
            })
            continue

        # Pick the requested level
        items = data.get(f"{level}s", data.get("campaigns", []))
        for item in items:
            item["client_group_id"] = g["id"]
            item["client_group_name"] = g["name"]
            enrich(item)

        all_results.append({
            "group_id": g["id"],
            "group_name": g["name"],
            "date_range": f"{start_date} to {end_date}",
            "items": items[:MAX_RESULT_ITEMS],
            "total_items": len(items),
            "aggregated_metrics": data.get("metrics", {}).get("insights", {}),
        })

    return {"groups": all_results, "total_groups": len(all_results)}


async def get_meta_leads_live(db, user_id, start_date, end_date, group_ids=None):
    """Fetch leads directly from Meta API for an arbitrary date range."""
    access_token, groups, err = await _get_token_and_groups(db, user_id, group_ids)
    if err and not groups:
        return {"error": err}

    all_leads = []
    for g in groups:
        ad_account_id = g["meta_ad_account_id"]
        data = await _fetch_leads_live(ad_account_id, access_token, start_date, end_date)

        if "error" in data:
            continue

        for lead in data.get("leads", []):
            lead["group_name"] = g["name"]
            lead["group_id"] = g["id"]
        all_leads.extend(data.get("leads", []))

    return {"leads": all_leads[:MAX_RESULT_ITEMS], "total": len(all_leads)}


# ── Registration ───────────────────────────────────────────────────────────

def register_meta_live_tools():
    registry.register(
        name="get_meta_insights_live",
        description=(
            "Fetch campaign, adset, or ad insights DIRECTLY from Meta's API for any arbitrary date range. "
            "Use this when the user asks about a specific date range that doesn't match a cached preset "
            "(e.g. 'January 2025', 'March 1 to March 15', a specific past month). "
            "This is slower than cached tools but returns real-time data from Meta. "
            "The 'level' parameter controls granularity: 'campaign', 'adset', or 'ad'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (required)."},
                "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format (required)."},
                "group_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter to specific group IDs. Omit for all groups.",
                },
                "level": {
                    "type": "string",
                    "description": "Granularity: 'campaign' (default), 'adset', or 'ad'.",
                },
            },
            "required": ["start_date", "end_date"],
        },
        executor=get_meta_insights_live,
    )

    registry.register(
        name="get_meta_leads_live",
        description=(
            "Fetch leads DIRECTLY from Meta's API for any arbitrary date range. "
            "Use when the user asks about leads for a specific past period not covered by cached presets. "
            "Returns lead contact info (name, email, phone) and which ad/campaign generated them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (required)."},
                "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format (required)."},
                "group_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Filter to specific group IDs. Omit for all groups.",
                },
            },
            "required": ["start_date", "end_date"],
        },
        executor=get_meta_leads_live,
    )
