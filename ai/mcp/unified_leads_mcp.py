"""
ai/mcp/unified_leads_mcp.py
----------------------------
The unified leads tools (get_unified_leads, get_unified_lead_stats), registered
onto the shared FastMCP server (ai/mcp/server.py) served over the Model
Context Protocol, mounted into the main FastAPI app at /mcp (see main.py).

Business logic here is a straight port of ai/tools/unified_leads_tools.py — see
that module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.

Leads matched across GHL, Meta, and HotProspector using normalized
email/phone match_keys.
"""

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.server import mcp, current_user_id as _current_user_id


_GET_UNIFIED_LEADS_DESCRIPTION = (
    "Get leads with cross-source matching info. Each lead shows which sources (GHL/Meta/HP) "
    "have a record of it, plus Meta campaign/ad names and GHL opportunity status/tags. "
    "Supports filters: source, matched_only, opportunity_status, has_tag, date range."
)


@mcp.tool(description=_GET_UNIFIED_LEADS_DESCRIPTION)
async def get_unified_leads(
    group_ids: list[str] | None = None,
    source: str | None = None,
    matched_only: bool = False,
    opportunity_status: str | None = None,
    has_tag: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> dict:
    """
    Args:
        group_ids: Filter to specific client group IDs. Omit for all groups.
        source: Filter to leads present in a specific source: 'ghl', 'meta', or 'hp'.
        matched_only: If true, only return leads matched across 2+ sources.
        opportunity_status: Filter by GHL opp status: 'won', 'lost', 'open', 'abandoned'.
        has_tag: Filter to leads with this GHL tag.
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
        limit: Max leads (default 50, max 200)
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

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
                {"lead_id": 1, "campaign_name": 1, "ad_name": 1, "adset_name": 1, "platform": 1, "_id": 0},
            )
            hp_match = await db["hotprospector_leads"].find_one(
                {"user_id": user_id, "match_keys": {"$in": match_keys}},
                {"id": 1, "call_logs": 1, "_id": 0},
            )

        sources = ["ghl"]
        if meta_match:
            sources.append("meta")
        if hp_match:
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
            "meta_campaign": meta_match.get("campaign_name") if meta_match else None,
            "meta_ad": meta_match.get("ad_name") if meta_match else None,
            "hp_call_count": len(hp_match.get("call_logs", [])) if hp_match else 0,
            "opportunity": opp_summary,
        })
        if len(leads) >= limit:
            break

    return {"leads": leads, "total": len(leads)}


_GET_UNIFIED_LEAD_STATS_DESCRIPTION = (
    "Get cross-source lead stats: totals per source (GHL/Meta/HP) and overlap counts "
    "(GHL-only, GHL+Meta matched, GHL+HP matched, all three). Use for questions like "
    "'how many of my Meta leads show up in GHL' or 'what's my lead match rate'."
)


@mcp.tool(description=_GET_UNIFIED_LEAD_STATS_DESCRIPTION)
async def get_unified_lead_stats(
    group_ids: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Args:
        group_ids: Filter to specific client group IDs. Omit for all groups.
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

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

    # Simple overlap: iterate a sample and check matches
    sample_limit = 5000
    cursor = db["ghl_contacts"].find(query, {"match_keys": 1, "_id": 0}).limit(sample_limit)
    ghl_sample = await cursor.to_list(length=sample_limit)

    ghl_only = 0
    ghl_plus_meta = 0
    ghl_plus_hp = 0
    ghl_plus_both = 0

    for doc in ghl_sample:
        keys = doc.get("match_keys", []) or []
        if not keys:
            ghl_only += 1
            continue
        meta_hit = await db["facebook_leads"].count_documents(
            {"user_id": user_id, "match_keys": {"$in": keys}}
        )
        hp_hit = await db["hotprospector_leads"].count_documents(
            {"user_id": user_id, "match_keys": {"$in": keys}}
        )
        if meta_hit and hp_hit:
            ghl_plus_both += 1
        elif meta_hit:
            ghl_plus_meta += 1
        elif hp_hit:
            ghl_plus_hp += 1
        else:
            ghl_only += 1

    meta_total = await db["facebook_leads"].count_documents({"user_id": user_id})
    hp_total = await db["hotprospector_leads"].count_documents({"user_id": user_id})

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
            "matched via email/phone normalization (match_keys)."
        ),
    }
