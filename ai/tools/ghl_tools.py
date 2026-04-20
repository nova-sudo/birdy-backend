from calendar import monthrange

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


# ─────────────────────────────────────────────────────────────────────────────
# Arbitrary-window opp stats (live from GHL API)
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_and_compute_window(db, user_id, group_id, start_date, end_date):
    """
    Pull every opportunity for a group's GHL location and compute stats for the
    given window. Returns {stats, ok, error}. `start_date`/`end_date` are
    yyyy-mm-dd ISO strings; both None = lifetime.
    """
    from integrations.gohighlevel import (
        ghl_integration,
        get_subaccount_tokens,
        compute_opp_stats,
    )

    grp = await db["client_groups"].find_one(
        {"user_id": user_id, "id": group_id},
        {"id": 1, "name": 1, "ghl_location_id": 1},
    )
    if not grp:
        return {"ok": False, "error": f"Group '{group_id}' not found"}
    location_id = grp.get("ghl_location_id")
    if not location_id:
        return {"ok": False, "error": f"Group '{grp.get('name')}' has no GHL location linked"}

    tokens = await get_subaccount_tokens(user_id, db.client)
    access_token = (tokens or {}).get(location_id, {}).get("access_token")
    if not access_token:
        return {"ok": False, "error": f"No GHL access token for location {location_id}"}

    ok, opps = await ghl_integration.fetch_all_opportunities(location_id, access_token)
    if not ok:
        return {"ok": False, "error": "GHL API fetch failed — the tool could not load opportunities"}

    stats = compute_opp_stats(opps, start_date, end_date)
    return {
        "ok": True,
        "group_id": grp["id"],
        "group_name": grp.get("name"),
        "total_opps_in_account": len(opps),
        "window": {"start": start_date, "end": end_date} if start_date else {"start": None, "end": None, "label": "lifetime"},
        "stats": stats,
    }


async def get_ghl_opp_stats_windowed(db, user_id, group_id, start_date=None, end_date=None):
    """
    Compute opp stats for an arbitrary date window — NOT restricted to the
    13 cached presets. Use this when the user asks for a specific month,
    quarter, or custom range that isn't in the standard preset list.
    Example: 'March 2025', 'Q2 2024', 'Jan 15 to Feb 15 2025'.
    """
    if (start_date and not end_date) or (end_date and not start_date):
        return {"error": "Provide both start_date and end_date, or neither for lifetime"}
    result = await _fetch_and_compute_window(db, user_id, group_id, start_date, end_date)
    return result


async def get_ghl_opp_stats_monthly(db, user_id, group_id, year):
    """
    Return month-by-month opp stats for a given year. Fetches opps once
    and derives 12 monthly stat objects in-memory (plus a yearly total).
    Ideal for 'show me each month's revenue' charts.
    """
    from integrations.gohighlevel import (
        ghl_integration,
        get_subaccount_tokens,
        compute_opp_stats,
    )

    try:
        year = int(year)
    except (TypeError, ValueError):
        return {"error": "`year` must be an integer like 2025"}

    grp = await db["client_groups"].find_one(
        {"user_id": user_id, "id": group_id},
        {"id": 1, "name": 1, "ghl_location_id": 1},
    )
    if not grp:
        return {"error": f"Group '{group_id}' not found"}
    location_id = grp.get("ghl_location_id")
    if not location_id:
        return {"error": f"Group '{grp.get('name')}' has no GHL location linked"}

    tokens = await get_subaccount_tokens(user_id, db.client)
    access_token = (tokens or {}).get(location_id, {}).get("access_token")
    if not access_token:
        return {"error": f"No GHL access token for location {location_id}"}

    ok, opps = await ghl_integration.fetch_all_opportunities(location_id, access_token)
    if not ok:
        return {"error": "GHL API fetch failed — the tool could not load opportunities"}

    # Compute 12 monthly windows
    months = []
    MONTH_NAMES = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    for m in range(1, 13):
        last_day = monthrange(year, m)[1]
        start = f"{year:04d}-{m:02d}-01"
        end = f"{year:04d}-{m:02d}-{last_day:02d}"
        stats = compute_opp_stats(opps, start, end)
        months.append({
            "month": f"{MONTH_NAMES[m - 1]} {year}",
            "start": start,
            "end": end,
            **stats,
        })

    # Yearly total (same data, window-free over the year)
    yearly = compute_opp_stats(opps, f"{year:04d}-01-01", f"{year:04d}-12-31")

    return {
        "group_id": grp["id"],
        "group_name": grp.get("name"),
        "year": year,
        "total_opps_in_account": len(opps),
        "months": months,
        "yearly_total": yearly,
    }


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
        name="get_ghl_opp_stats_windowed",
        description=(
            "Compute GHL opportunity stats (won/lost/open/abandoned, won_revenue, "
            "total_opportunities) for an ARBITRARY date window — not restricted to "
            "the 13 cached presets. Use this whenever the user asks for a specific "
            "month, quarter, or custom range that isn't in the preset list (e.g. "
            "'March 2025', 'Q2 2024', 'Jan 15 to Feb 15 2025'). Works by fetching "
            "all opps once via the GHL API and filtering by lastStatusChangeAt / "
            "createdAt in-memory. Slower than cached presets (~3-10s) but accurate. "
            "REQUIRES a single group_id. Omit both dates for lifetime stats."
        ),
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "The client group ID (from get_client_groups)."},
                "start_date": {"type": "string", "description": "Window start in YYYY-MM-DD format."},
                "end_date": {"type": "string", "description": "Window end in YYYY-MM-DD format."},
            },
            "required": ["group_id"],
        },
        executor=get_ghl_opp_stats_windowed,
    )

    registry.register(
        name="get_ghl_opp_stats_monthly",
        description=(
            "Return month-by-month GHL opp stats for a given year. Fetches all opps "
            "once and derives 12 monthly stat objects (won/lost/open/abandoned counts, "
            "won_revenue, total_opportunities) plus a yearly total. Use this for "
            "'monthly revenue in 2025' or 'show me each month's won opps for Aura in 2024' "
            "style questions — it's the right tool for time-series charts spanning a year. "
            "Stats are counted by lastStatusChangeAt for closed statuses and createdAt "
            "for newly-open opps (so 'won in May' = opps closed won in May)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "The client group ID (from get_client_groups)."},
                "year": {"type": "integer", "description": "The calendar year, e.g. 2025."},
            },
            "required": ["group_id", "year"],
        },
        executor=get_ghl_opp_stats_monthly,
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
