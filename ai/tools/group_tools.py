from ai.tools.registry import registry
from core.utils import mongo_to_dict


async def get_client_groups(db, user_id, **kwargs):
    cursor = db["client_groups"].find(
        {"user_id": user_id},
        {
            "id": 1,
            "name": 1,
            "meta_ad_account_id": 1,
            "ghl_location_id": 1,
            "hotprospector_group_id": 1,
            "ad_account_currency": 1,
            "notes": 1,
            "status": 1,
        },
    )
    docs = await cursor.to_list(length=None)
    groups = []
    for doc in docs:
        groups.append({
            "id": doc.get("id"),
            "name": doc.get("name"),
            "meta_ad_account_id": doc.get("meta_ad_account_id"),
            "ghl_location_id": doc.get("ghl_location_id"),
            "hotprospector_group_id": doc.get("hotprospector_group_id"),
            "currency": doc.get("ad_account_currency"),
            "notes": doc.get("notes", ""),
            "status": doc.get("status"),
        })
    return {"client_groups": groups, "total": len(groups)}


def register_group_tools():
    registry.register(
        name="get_client_groups",
        description="List all client groups (business clients) for the user. Returns group IDs, names, and which integrations are linked. Call this first to discover available groups before filtering other queries.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        executor=get_client_groups,
    )
