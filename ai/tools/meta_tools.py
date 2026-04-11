from ai.tools.registry import registry
from ai.tools.derived_metrics import enrich
from ai.config import MAX_RESULT_ITEMS
from core.utils import mongo_to_dict


def _build_insights_query(user_id, start_date=None, end_date=None, group_ids=None):
    query = {"user_id": user_id}
    if group_ids:
        query["client_group_id"] = {"$in": group_ids}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date
        if end_date:
            date_filter["$lte"] = end_date
        query["date_start"] = date_filter
    return query


def _flatten_insight(doc):
    """Extract key metrics from insight_data into a flat dict for the AI."""
    data = doc.get("insight_data", {})
    flat = {
        "campaign_id": doc.get("campaign_id") or data.get("campaign_id"),
        "campaign_name": doc.get("campaign_name") or data.get("campaign_name"),
        "adset_id": doc.get("adset_id") or data.get("adset_id"),
        "adset_name": doc.get("adset_name") or data.get("adset_name"),
        "ad_id": doc.get("ad_id") or data.get("ad_id"),
        "ad_name": doc.get("ad_name") or data.get("ad_name"),
        "date_start": doc.get("date_start"),
        "client_group_id": doc.get("client_group_id"),
        "client_group_name": doc.get("client_group_name"),
        "spend": data.get("spend"),
        "impressions": data.get("impressions"),
        "clicks": data.get("clicks"),
        "ctr": data.get("ctr"),
        "cpc": data.get("cpc"),
        "cpm": data.get("cpm"),
        "reach": data.get("reach"),
        "results": data.get("results"),
    }
    return enrich(flat)


# Only fetch the fields we need from MongoDB
_PROJECTION = {
    "_id": 0, "campaign_id": 1, "campaign_name": 1, "adset_id": 1,
    "adset_name": 1, "ad_id": 1, "ad_name": 1, "date_start": 1,
    "client_group_id": 1, "client_group_name": 1, "insight_data": 1,
}


# ---------------------------------------------------------------------------
# Helpers: read from facebook_cache on client_groups (where the data lives)
# ---------------------------------------------------------------------------

def _pick_preset(start_date, end_date):
    """Map a date range to the best matching facebook_cache preset key."""
    from datetime import date as _date, timedelta

    if not start_date and not end_date:
        return "maximum"

    today = _date.today()
    try:
        s = _date.fromisoformat(start_date) if start_date else None
        e = _date.fromisoformat(end_date) if end_date else today
    except ValueError:
        return "maximum"

    if not s:
        return "maximum"

    delta = (e - s).days

    # Exact matches
    if s == e == today:
        return "today"
    if s == e == today - timedelta(days=1):
        return "yesterday"
    if delta <= 7:
        return "last_7d"
    if delta <= 14:
        return "last_14d"
    if delta <= 30:
        return "last_30d"
    if delta <= 90:
        return "this_quarter"
    if delta <= 365:
        return "this_year"
    return "maximum"


async def _get_cached_data(db, user_id, level, group_ids=None, start_date=None, end_date=None):
    """
    Read campaign/adset/ad data from facebook_cache on client_groups.
    This is where the actual data lives (populated by the preset refresh).
    Falls back to the per-day insights collections if cache has no data.
    """
    preset = _pick_preset(start_date, end_date)

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            f"facebook_cache.{preset}": 1,
            "facebook_cache.campaigns": 1,
            "facebook_cache.adsets": 1,
            "facebook_cache.ads": 1,
        },
    ).to_list(None)

    results = []
    for g in groups:
        cache = g.get("facebook_cache", {})
        # Try preset-specific data first, fall back to top-level (backward compat)
        preset_data = cache.get(preset, {})
        items = preset_data.get(level, []) or cache.get(level, [])

        for item in items:
            item["client_group_id"] = g.get("id")
            item["client_group_name"] = g.get("name")
            enrich(item)
            results.append(item)

    return results


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

async def get_campaign_insights(db, user_id, start_date=None, end_date=None, group_ids=None):
    # Try cache first (where the data actually is)
    cached = await _get_cached_data(db, user_id, "campaigns", group_ids, start_date, end_date)
    if cached:
        return {"insights": cached[:MAX_RESULT_ITEMS], "total": len(cached)}

    # Fallback to per-day insights collection
    query = _build_insights_query(user_id, start_date, end_date, group_ids)
    cursor = db["facebook_campaign_insights"].find(query, _PROJECTION).sort("date_start", -1).limit(MAX_RESULT_ITEMS)
    docs = await cursor.to_list(length=MAX_RESULT_ITEMS)
    return {"insights": [_flatten_insight(d) for d in docs], "total": len(docs)}


async def get_adset_insights(db, user_id, start_date=None, end_date=None, group_ids=None):
    cached = await _get_cached_data(db, user_id, "adsets", group_ids, start_date, end_date)
    if cached:
        return {"insights": cached[:MAX_RESULT_ITEMS], "total": len(cached)}

    query = _build_insights_query(user_id, start_date, end_date, group_ids)
    cursor = db["facebook_adset_insights"].find(query, _PROJECTION).sort("date_start", -1).limit(MAX_RESULT_ITEMS)
    docs = await cursor.to_list(length=MAX_RESULT_ITEMS)
    return {"insights": [_flatten_insight(d) for d in docs], "total": len(docs)}


async def get_ad_insights(db, user_id, start_date=None, end_date=None, group_ids=None):
    cached = await _get_cached_data(db, user_id, "ads", group_ids, start_date, end_date)
    if cached:
        return {"insights": cached[:MAX_RESULT_ITEMS], "total": len(cached)}

    query = _build_insights_query(user_id, start_date, end_date, group_ids)
    cursor = db["facebook_ad_insights"].find(query, _PROJECTION).sort("date_start", -1).limit(MAX_RESULT_ITEMS)
    docs = await cursor.to_list(length=MAX_RESULT_ITEMS)
    return {"insights": [_flatten_insight(d) for d in docs], "total": len(docs)}


async def get_facebook_leads(db, user_id, start_date=None, end_date=None, limit=100, group_ids=None):
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
        query["lead_data.created_time"] = date_filter

    cursor = db["facebook_leads"].find(
        query,
        {"lead_data": 1, "client_group_name": 1, "client_group_id": 1},
    ).sort("lead_data.created_time", -1).limit(limit)

    docs = await cursor.to_list(length=limit)
    leads = []
    for doc in docs:
        ld = doc.get("lead_data", {})
        leads.append({
            "lead_id": ld.get("id"),
            "full_name": ld.get("full_name", ""),
            "email": ld.get("email", ""),
            "phone_number": ld.get("phone_number", ""),
            "ad_name": ld.get("ad_name", ""),
            "campaign_name": ld.get("campaign_name", ""),
            "created_time": ld.get("created_time", ""),
            "group_name": doc.get("client_group_name", ""),
        })
    return {"leads": leads, "total": len(leads)}


# ── Schema definitions ──────────────────────────────────────────────────────

_INSIGHTS_PARAMS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
        "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format"},
        "group_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of client group IDs to filter by. Omit to query all groups.",
        },
    },
    "required": [],
}

_LEADS_PARAMS = {
    "type": "object",
    "properties": {
        "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
        "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format"},
        "limit": {"type": "integer", "description": "Max leads to return (default 100, max 200)"},
        "group_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of client group IDs to filter by. Omit to query all groups.",
        },
    },
    "required": [],
}


def register_meta_tools():
    registry.register(
        name="get_campaign_insights",
        description="Get Facebook campaign-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, results/leads). Returns data from the cached preset that best matches the date range.",
        parameters=_INSIGHTS_PARAMS,
        executor=get_campaign_insights,
    )
    registry.register(
        name="get_adset_insights",
        description="Get Facebook ad set-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM). Returns data from the cached preset that best matches the date range.",
        parameters=_INSIGHTS_PARAMS,
        executor=get_adset_insights,
    )
    registry.register(
        name="get_ad_insights",
        description="Get Facebook individual ad-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, quality rankings). Returns data from the cached preset that best matches the date range.",
        parameters=_INSIGHTS_PARAMS,
        executor=get_ad_insights,
    )
    registry.register(
        name="get_facebook_leads",
        description="Get Facebook leads with contact info (name, email, phone) and which ad/campaign generated them. Sorted by newest first.",
        parameters=_LEADS_PARAMS,
        executor=get_facebook_leads,
    )
