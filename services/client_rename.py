"""
services/client_rename.py
-------------------------
Renaming a client.

`client_groups.name` is the source of truth, but other collections keep their
own copy of it, and the client page shows those copies. This updates them so a
renamed client doesn't keep showing its old name on older records.

Agent reports and Slack messages already sent are left alone: they record what
was said at the time.
"""

import asyncio

from ai.agents.store import CARDS
from ai.suggestions.store import ACTIVITY, SUGGESTIONS
from services.tracked_leads import TRACKED_LEADS

MAX_NAME_LENGTH = 100

_CLIENT_GROUP_NAME_COPIES = (
    "facebook_leads",
    "facebook_ad_insights",
    "facebook_adset_insights",
    "facebook_campaign_insights",
    "ghl_contacts",
    TRACKED_LEADS,
)
_CLIENT_NAME_COPIES = (SUGGESTIONS, CARDS)


def clean_name(raw: str) -> str:
    """The name to store. Raises ValueError with a message safe to show the user."""
    name = raw.strip()
    if not name:
        raise ValueError("Enter a client name.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"Client names can be up to {MAX_NAME_LENGTH} characters.")
    return name


async def _update_alerts(db, user_id: str, group_id: str, name: str) -> None:
    """Alerts keep names in a list that lines up with `target_group_ids`."""
    cursor = db["alerts"].find(
        {"user_id": user_id, "target_group_ids": group_id},
        {"target_group_ids": 1, "target_group_names": 1},
    )
    async for alert in cursor:
        ids = alert.get("target_group_ids") or []
        names = list(alert.get("target_group_names") or [])
        if len(ids) != len(names):
            continue
        names[ids.index(group_id)] = name
        await db["alerts"].update_one({"_id": alert["_id"]}, {"$set": {"target_group_names": names}})


async def update_name_copies(db, group: dict, old_name: str, name: str) -> None:
    """Update every stored copy of this client's name. Safe to run twice."""
    user_id, group_id = group["user_id"], group["id"]
    owned = {"user_id": user_id, "client_group_id": group_id}

    updates = [
        db[collection].update_many(
            {**owned, "client_group_name": {"$ne": name}}, {"$set": {"client_group_name": name}}
        )
        for collection in _CLIENT_GROUP_NAME_COPIES
    ]
    updates += [
        db[collection].update_many(
            {**owned, "client_name": {"$ne": name}}, {"$set": {"client_name": name}}
        )
        for collection in _CLIENT_NAME_COPIES
    ]
    # Some activity rows were logged with a name and no client id.
    updates.append(db[ACTIVITY].update_many(
        {"user_id": user_id, "$or": [
            {"client_group_id": group_id},
            {"client_group_id": None, "client": old_name},
        ]},
        {"$set": {"client": name}},
    ))
    # HotProspector leads are stored per GHL location, not per client.
    if group.get("ghl_location_id"):
        updates.append(db["hotprospector_leads"].update_many(
            {"user_id": user_id, "ghl_location_id": group["ghl_location_id"],
             "lead_data.client_name": old_name},
            {"$set": {"lead_data.client_name": name}},
        ))
    updates.append(_update_alerts(db, user_id, group_id, name))

    results = await asyncio.gather(*updates, return_exceptions=True)
    failures = [r for r in results if isinstance(r, Exception)]
    if failures:
        raise failures[0]
