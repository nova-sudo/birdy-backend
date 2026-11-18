"""
AI tools for unified leads — leads matched across GHL, Meta, and HotProspector
using normalized email/phone match_keys.
"""
from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS


def _meta_field(meta_match, field):
    """A facebook_leads field: the sync stores the campaign and ad names under lead_data."""
    if not meta_match:
        return None
    return meta_match.get(field) or (meta_match.get("lead_data") or {}).get(field)


def _hp_call_count(hp_match):
    if not hp_match:
        return 0
    lead = hp_match.get("lead_data") or {}
    return int(lead.get("call_logs_count") or len(hp_match.get("call_logs") or []))


async def _matched_keys(db, collection, base_query, keys):
    """Which of `keys` appear in a lead collection's match_keys, asked in batches."""
    found = set()
    keys = list(keys)
    for i in range(0, len(keys), _KEY_BATCH):
        batch = keys[i:i + _KEY_BATCH]
        async for doc in db[collection].find({**base_query, "match_keys": {"$in": batch}}, {"match_keys": 1, "_id": 0}):
            found.update(doc.get("match_keys") or [])
    return found


_KEY_BATCH = 2000


async def get_unified_leads(
    db, user_id,
    group_ids=None,
    source=None,
    matched_only=False,
    opportunity_status=None,
    has_tag=None,
    start_date=None,
    end_date=None,
    limit=50,
):
    """
    Fetch leads matched across sources. Supports filtering by source, match status,
    GHL opportunity status, or tag presence.
    """
    limit = min(int(limit), 200)

    # Base: always query ghl_contacts (they have match_keys on them)
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
    if has_tag:
        query["contact_data.tags"] = has_tag
    if opportunity_status:
        query["contact_data.opportunities.status"] = opportunity_status

    cursor = db["ghl_contacts"].find(
        query,
        {
            "contact_id": 1, "contact_data": 1, "match_keys": 1,
            "client_group_name": 1, "_id": 0,
        }
    ).sort("contact_data.dateAdded", -1).limit(limit * 2)  # over-fetch for filtering
    ghl_docs = await cursor.to_list(length=limit * 2)

    leads = []
    for doc in ghl_docs:
        cd = doc.get("contact_data", {}) or {}
        match_keys = doc.get("match_keys", []) or []

        # Check if this contact matches anything in facebook_leads or hotprospector_leads
        meta_match = None
        hp_match = None
        if match_keys:
            meta_match = await db["facebook_leads"].find_one(
                {"user_id": user_id, "match_keys": {"$in": match_keys}},
                {"lead_id": 1, "campaign_name": 1, "ad_name": 1, "lead_data.campaign_name": 1,
                 "lead_data.ad_name": 1, "_id": 0},
            )
            # HotProspector keeps the lead under lead_data. Asking for top-level
            # fields that are not there returned {}, which reads as "no match".
            hp_match = await db["hotprospector_leads"].find_one(
                {"user_id": user_id, "match_keys": {"$in": match_keys}},
                {"lead_data.id": 1, "lead_data.call_logs_count": 1, "call_logs": 1},
            )

        sources = ["ghl"]
        if meta_match is not None:
            sources.append("meta")
        if hp_match is not None:
            sources.append("hp")

        # Filter by source if requested
        if source and source not in sources:
            continue
        if matched_only and len(sources) < 2:
            continue

        opps = cd.get("opportunities", []) or []
        opp_summary = None
        if opps:
            top = opps[0]
            opp_summary = {
                "status": top.get("status", "open"),
                "monetary_value": top.get("monetaryValue") or top.get("value") or 0,
            }

        leads.append({
            "contact_id": doc.get("contact_id"),
            "name": f"{cd.get('firstName', '')} {cd.get('lastName', '')}".strip(),
            "email": cd.get("email"),
            "phone": cd.get("phone"),
            "tags": cd.get("tags", []),
            "group_name": doc.get("client_group_name"),
            "date_added": cd.get("dateAdded"),
            "sources": sources,
            "meta_campaign": _meta_field(meta_match, "campaign_name"),
            "meta_ad": _meta_field(meta_match, "ad_name"),
            "hp_call_count": _hp_call_count(hp_match),
            "opportunity": opp_summary,
        })
        if len(leads) >= limit:
            break

    return {"leads": leads, "total": len(leads)}


async def get_unified_lead_stats(db, user_id, group_ids=None, start_date=None, end_date=None):
    """
    Count leads across sources + match overlap.
    Returns: how many GHL-only, Meta-only, HP-only, and how many are matched across 2 or 3 sources.
    """
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

    ghl_total = await db["ghl_contacts"].count_documents(query)

    # The Meta and HotProspector sides are scoped to the same clients as the GHL
    # side: Meta leads carry the client group, HotProspector leads its GHL location.
    meta_query, hp_query = {"user_id": user_id}, {"user_id": user_id}
    if group_ids:
        meta_query["client_group_id"] = {"$in": group_ids}
        locations = [g.get("ghl_location_id") async for g in db["client_groups"].find(
            {"user_id": user_id, "id": {"$in": group_ids}}, {"ghl_location_id": 1, "_id": 0}) if g.get("ghl_location_id")]
        hp_query["ghl_location_id"] = {"$in": locations}

    sample_limit = 5000
    cursor = db["ghl_contacts"].find(query, {"match_keys": 1, "_id": 0}).limit(sample_limit)
    ghl_sample = await cursor.to_list(length=sample_limit)

    # Two batched lookups instead of two count queries per contact (10,000
    # round trips for a full sample, which ran for minutes).
    all_keys = {k for doc in ghl_sample for k in (doc.get("match_keys") or [])}
    meta_keys = await _matched_keys(db, "facebook_leads", {"user_id": user_id}, all_keys)
    hp_keys = await _matched_keys(db, "hotprospector_leads", {"user_id": user_id}, all_keys)

    ghl_only = 0
    ghl_plus_meta = 0
    ghl_plus_hp = 0
    ghl_plus_both = 0

    for doc in ghl_sample:
        keys = doc.get("match_keys", []) or []
        meta_hit = any(k in meta_keys for k in keys)
        hp_hit = any(k in hp_keys for k in keys)
        if meta_hit and hp_hit:
            ghl_plus_both += 1
        elif meta_hit:
            ghl_plus_meta += 1
        elif hp_hit:
            ghl_plus_hp += 1
        else:
            ghl_only += 1

    meta_total = await db["facebook_leads"].count_documents(meta_query)
    hp_total = await db["hotprospector_leads"].count_documents(hp_query)

    return {
        "totals": {
            "ghl_contacts": ghl_total,
            "meta_leads": meta_total,
            "hotprospector_leads": hp_total,
        },
        "overlap_in_sample": {
            "ghl_only": ghl_only,
            "ghl_and_meta": ghl_plus_meta,
            "ghl_and_hp": ghl_plus_hp,
            "ghl_and_meta_and_hp": ghl_plus_both,
            "sample_size": len(ghl_sample),
        },
        "note": (
            "Overlap counts are based on a sample of up to 5000 GHL contacts "
            "matched via email/phone normalization (match_keys). meta_leads and hotprospector_leads "
            "count the leads Birdy has stored for the chosen clients; a date range narrows GHL contacts only."
        ),
    }


def register_unified_leads_tools():
    registry.register(
        name="get_unified_leads",
        description=(
            "Get leads with cross-source matching info. Each lead shows which sources (GHL/Meta/HP) "
            "have a record of it, plus Meta campaign/ad names and GHL opportunity status/tags. "
            "Supports filters: source, matched_only, opportunity_status, has_tag, date range."
        ),
        parameters={
            "type": "object",
            "properties": {
                "group_ids": {"type": "array", "items": {"type": "string"}},
                "source": {
                    "type": "string",
                    "description": "Filter to leads present in a specific source: 'ghl', 'meta', or 'hp'",
                },
                "matched_only": {
                    "type": "boolean",
                    "description": "If true, only return leads matched across 2+ sources",
                },
                "opportunity_status": {
                    "type": "string",
                    "description": "Filter by GHL opp status: 'won', 'lost', 'open', 'abandoned'",
                },
                "has_tag": {"type": "string", "description": "Filter to leads with this GHL tag"},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "description": "Max leads (default 50, max 200)"},
            },
            "required": [],
        },
        executor=get_unified_leads,
    )

    registry.register(
        name="get_unified_lead_stats",
        description=(
            "Get cross-source lead stats: totals per source (GHL/Meta/HP) and overlap counts "
            "(GHL-only, GHL+Meta matched, GHL+HP matched, all three). Use for questions like "
            "'how many of my Meta leads show up in GHL' or 'what's my lead match rate'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "group_ids": {"type": "array", "items": {"type": "string"}},
                "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                "end_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": [],
        },
        executor=get_unified_lead_stats,
    )
