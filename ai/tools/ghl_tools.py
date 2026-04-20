from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from core.utils import mongo_to_dict

_PRESET_LIST = (
    "maximum, today, yesterday, this_week_mon_today, last_7d, last_14d, "
    "last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year"
)


def _extract_opportunities(contact_data):
    """Extract opportunity summary from contact data."""
    opportunities = contact_data.get("opportunities") or []
    if not opportunities:
        return []
    result = []
    for opp in opportunities:
        monetary = opp.get("monetaryValue") or opp.get("value") or opp.get("amount")
        result.append({
            "id": opp.get("id", ""),
            "status": opp.get("status", "open"),
            "monetary_value": monetary,
        })
    return result


async def get_ghl_contacts(db, user_id, limit=50, group_ids=None, start_date=None, end_date=None):
    limit = min(int(limit), 200)
    query = {"user_id": user_id}
    if group_ids:
        query["client_group_id"] = {"$in": group_ids}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date
        if end_date:
            date_filter["$lte"] = end_date + "T23:59:59+9999"
        query["contact_data.dateAdded"] = date_filter

    cursor = db["ghl_contacts"].find(query).sort("contact_data.dateAdded", -1).limit(limit)
    docs = await cursor.to_list(length=limit)

    contacts = []
    for doc in docs:
        cd = doc.get("contact_data", {})
        contacts.append({
            "contact_id": cd.get("id", ""),
            "name": f"{cd.get('firstName', '')} {cd.get('lastName', '')}".strip(),
            "email": cd.get("email", ""),
            "phone": cd.get("phone", ""),
            "tags": cd.get("tags", []),
            "date_added": cd.get("dateAdded", ""),
            "source": cd.get("source", ""),
            "group_name": doc.get("client_group_name", ""),
            "opportunities": _extract_opportunities(cd),
        })
    return {"contacts": contacts, "total": len(contacts)}


async def get_ghl_opportunity_stats(db, user_id, preset="maximum", group_ids=None):
    """
    Get GHL opportunity stats (won, lost, open, abandoned counts + won revenue)
    per client group for a given date preset.

    Reads from the pre-cached ghl_opp_cache.<preset> field which is populated
    from the GHL Opportunities Search API (not from embedded contact data).
    """
    from core.constants import PRESET_ALIAS
    resolved = PRESET_ALIAS.get(preset or "maximum", "maximum")

    query = {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1, "ghl_location_id": 1,
            f"ghl_opp_cache.{resolved}": 1,
            "ghl_opp_cache.maximum": 1,
            "gohighlevel_cache.metrics.opportunity_stats": 1,
            "_id": 0,
        },
    ).to_list(None)

    results = []
    totals = {
        "won": 0, "lost": 0, "open": 0, "abandoned": 0,
        "total_opportunities": 0, "won_revenue": 0.0,
    }

    for g in groups:
        opp_cache = g.get("ghl_opp_cache") or {}
        # Prefer the requested preset, fall back to "maximum", then to legacy cache
        stats = opp_cache.get(resolved) or opp_cache.get("maximum") or \
                (g.get("gohighlevel_cache", {}).get("metrics", {}).get("opportunity_stats") or {})

        won = stats.get("won", 0)
        lost = stats.get("lost", 0)
        opened = stats.get("open", 0)
        abandoned = stats.get("abandoned", 0)
        total_opps = stats.get("total_opportunities", won + lost + opened + abandoned)
        won_rev = float(stats.get("won_revenue", 0) or 0)
        conversion = (won / total_opps * 100) if total_opps > 0 else 0

        results.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "won": won,
            "lost": lost,
            "open": opened,
            "abandoned": abandoned,
            "total_opportunities": total_opps,
            "won_revenue": round(won_rev, 2),
            "conversion_rate": round(conversion, 2),
        })

        totals["won"] += won
        totals["lost"] += lost
        totals["open"] += opened
        totals["abandoned"] += abandoned
        totals["total_opportunities"] += total_opps
        totals["won_revenue"] += won_rev

    overall_conv = (totals["won"] / totals["total_opportunities"] * 100) if totals["total_opportunities"] > 0 else 0
    totals["conversion_rate"] = round(overall_conv, 2)
    totals["won_revenue"] = round(totals["won_revenue"], 2)

    return {
        "overall": totals,
        "groups": results[:MAX_RESULT_ITEMS],
        "preset": resolved,
        "total_groups": len(results),
    }


async def get_ghl_tag_breakdown(db, user_id, preset="maximum", group_ids=None, top_n=20):
    """
    Get tag breakdown (tag -> contact count) per client group from the cached
    gohighlevel_cache.metrics.tag_breakdown.
    """
    from core.constants import PRESET_ALIAS
    resolved = PRESET_ALIAS.get(preset or "maximum", "maximum")

    query = {"user_id": user_id, "ghl_location_id": {"$exists": True, "$ne": None}}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {"id": 1, "name": 1, "gohighlevel_cache.metrics.tag_breakdown": 1, "_id": 0},
    ).to_list(None)

    results = []
    global_tag_totals = {}

    for g in groups:
        tag_breakdown = g.get("gohighlevel_cache", {}).get("metrics", {}).get("tag_breakdown", {}) or {}
        sorted_tags = sorted(tag_breakdown.items(), key=lambda x: x[1], reverse=True)[:top_n]

        for tag, count in tag_breakdown.items():
            global_tag_totals[tag] = global_tag_totals.get(tag, 0) + count

        results.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "top_tags": dict(sorted_tags),
            "total_unique_tags": len(tag_breakdown),
        })

    top_global = dict(sorted(global_tag_totals.items(), key=lambda x: x[1], reverse=True)[:top_n])

    return {
        "groups": results[:MAX_RESULT_ITEMS],
        "top_tags_across_all_groups": top_global,
        "preset": resolved,
        "total_groups": len(results),
    }


async def get_tag_rollup_by_campaign(db, user_id, group_ids=None, level="campaign"):
    """
    Get tag counts aggregated by campaign/adset/ad from matched GHL contacts.
    Uses contact_data.attributionSource to link contacts back to Meta campaigns.
    """
    if level not in ("campaign", "adset", "ad"):
        level = "campaign"

    match_stage = {"user_id": user_id}
    if group_ids:
        match_stage["client_group_id"] = {"$in": group_ids}

    id_field_map = {
        "campaign": "$contact_data.attributionSource.campaignId",
        "adset": "$contact_data.attributionSource.utmMedium",
        "ad": "$contact_data.attributionSource.adId",
    }
    id_field = id_field_map[level]

    pipeline = [
        {"$match": match_stage},
        {"$unwind": {"path": "$contact_data.tags", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": {
                "entity_id": id_field,
                "tag": "$contact_data.tags",
            },
            "count": {"$sum": 1},
        }},
        {"$match": {"_id.entity_id": {"$ne": None, "$ne": ""}}},
        {"$group": {
            "_id": "$_id.entity_id",
            "tags": {"$push": {"tag": "$_id.tag", "count": "$count"}},
            "total": {"$sum": "$count"},
        }},
        {"$sort": {"total": -1}},
        {"$limit": MAX_RESULT_ITEMS},
    ]

    docs = await db["ghl_contacts"].aggregate(pipeline).to_list(length=None)
    results = []
    for d in docs:
        # Sort tags within each entity by count desc
        top_tags = sorted(d.get("tags", []), key=lambda x: x["count"], reverse=True)[:10]
        results.append({
            f"{level}_id": d.get("_id"),
            "total_tagged_contacts": d.get("total", 0),
            "tag_counts": {t["tag"]: t["count"] for t in top_tags},
        })

    return {"level": level, "results": results, "total": len(results)}


def register_ghl_tools():
    registry.register(
        name="get_ghl_contacts",
        description="Get GoHighLevel CRM contacts with name, email, phone, tags, source, and opportunity data. Sorted by newest first.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max contacts to return (default 50, max 200)"},
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of client group IDs to filter by. Omit to query all groups.",
                },
                "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
                "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format"},
            },
            "required": [],
        },
        executor=get_ghl_contacts,
    )

    registry.register(
        name="get_ghl_opportunity_stats",
        description=(
            "Get accurate opportunity statistics per client group: won, lost, open, abandoned counts, "
            "won revenue, and conversion rate — sourced directly from the GHL Opportunities API and "
            "pre-cached per date preset. Use this for ANY opportunity/revenue/conversion question. "
            "Supports 13 date presets (maximum, today, last_7d, last_30d, this_month, last_month, etc.)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "description": f"Date range preset. Valid values: {_PRESET_LIST}. Default: maximum (lifetime).",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of client group IDs to filter by. Omit for all groups.",
                },
            },
            "required": [],
        },
        executor=get_ghl_opportunity_stats,
    )

    registry.register(
        name="get_ghl_tag_breakdown",
        description=(
            "Get tag breakdown per client group — shows which GHL tags are applied to contacts "
            "and how many contacts each tag has. Returns per-group top tags plus a global ranking "
            "across all groups. Use this to answer questions about tag distribution, lead qualification tags, etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "preset": {
                    "type": "string",
                    "description": f"Date range preset. Valid values: {_PRESET_LIST}. Default: maximum.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of client group IDs to filter by. Omit for all groups.",
                },
                "top_n": {"type": "integer", "description": "Number of top tags to return per group (default 20)"},
            },
            "required": [],
        },
        executor=get_ghl_tag_breakdown,
    )

    registry.register(
        name="get_tag_rollup_by_campaign",
        description=(
            "Get tag counts rolled up by Meta campaign/adset/ad. Uses GHL contact attribution data "
            "to link contacts back to the Meta campaign they came from. Useful for questions like "
            "'which campaign brings the most hot leads' or 'which ad has the most zombie leads'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "description": "Roll up level: 'campaign', 'adset', or 'ad'. Default: campaign.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of client group IDs to filter by. Omit for all groups.",
                },
            },
            "required": [],
        },
        executor=get_tag_rollup_by_campaign,
    )
