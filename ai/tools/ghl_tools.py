from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from core.utils import mongo_to_dict


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


async def get_ghl_opportunity_stats(db, user_id, group_ids=None, start_date=None, end_date=None):
    """Aggregate opportunity stats (won/lost/open counts and values) per client group."""
    match_stage = {"user_id": user_id, "contact_data.opportunities": {"$exists": True, "$ne": []}}
    if group_ids:
        match_stage["client_group_id"] = {"$in": group_ids}
    if start_date or end_date:
        date_filter = {}
        if start_date:
            date_filter["$gte"] = start_date
        if end_date:
            date_filter["$lte"] = end_date + "T23:59:59+9999"
        match_stage["contact_data.dateAdded"] = date_filter

    pipeline = [
        {"$match": match_stage},
        {"$unwind": "$contact_data.opportunities"},
        {"$group": {
            "_id": {
                "group_id": "$client_group_id",
                "group_name": "$client_group_name",
                "status": {"$ifNull": ["$contact_data.opportunities.status", "open"]},
            },
            "count": {"$sum": 1},
            "total_value": {
                "$sum": {
                    "$ifNull": [
                        "$contact_data.opportunities.monetaryValue",
                        {"$ifNull": [
                            "$contact_data.opportunities.value",
                            {"$ifNull": ["$contact_data.opportunities.amount", 0]}
                        ]}
                    ]
                }
            },
        }},
        {"$group": {
            "_id": {
                "group_id": "$_id.group_id",
                "group_name": "$_id.group_name",
            },
            "statuses": {
                "$push": {
                    "status": "$_id.status",
                    "count": "$count",
                    "total_value": "$total_value",
                }
            },
            "total_opportunities": {"$sum": "$count"},
        }},
        {"$project": {
            "_id": 0,
            "group_id": "$_id.group_id",
            "group_name": "$_id.group_name",
            "total_opportunities": 1,
            "statuses": 1,
        }},
    ]

    results = await db["ghl_contacts"].aggregate(pipeline).to_list(length=None)
    return {"groups": results, "total_groups": len(results)}


def register_ghl_tools():
    registry.register(
        name="get_ghl_contacts",
        description="Get GoHighLevel CRM contacts with name, email, phone, tags, source, and opportunity data (status: won/lost/open, monetary value). Sorted by newest first.",
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
        description="Get aggregated opportunity statistics per client group: counts and total monetary values broken down by status (won/lost/open/abandoned). Use this for conversion rate comparisons between groups.",
        parameters={
            "type": "object",
            "properties": {
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of client group IDs to compare. Omit to get stats for all groups.",
                },
                "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format"},
                "end_date": {"type": "string", "description": "End date in YYYY-MM-DD format"},
            },
            "required": [],
        },
        executor=get_ghl_opportunity_stats,
    )
