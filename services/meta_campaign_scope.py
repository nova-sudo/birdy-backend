"""
services/meta_campaign_scope.py
-------------------------------
Rules for letting several client groups share one Meta ad account, each
owning chosen campaigns (`client_groups.meta_campaign_ids`; None = all).
"""

import json
from typing import Optional

from core.database import DB_NAME

_COLLECTION = "client_groups"
_UNASSIGNED_STATUSES = ("ACTIVE", "PAUSED")


class ScopeConflict(Exception):
    """A campaign selection breaks the sharing rules for the account."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def campaign_filter(campaign_ids: Optional[list], field: str = "campaign.id") -> Optional[str]:
    """JSON for the Graph `filtering` param, or None when the group sees all campaigns."""
    if campaign_ids is None:
        return None
    return json.dumps([{"field": field, "operator": "IN", "value": list(campaign_ids)}])


async def siblings(db, user_id: str, ad_account_id: str, exclude_group_id: Optional[str] = None) -> list:
    """Other groups of this user on the same Meta ad account."""
    query = {"user_id": user_id, "meta_ad_account_id": ad_account_id}
    if exclude_group_id is not None:
        query["id"] = {"$ne": exclude_group_id}
    projection = {
        "_id": 0, "id": 1, "name": 1, "meta_campaign_ids": 1,
        "meta_account_campaigns": 1, "created_at": 1,
    }
    return await db[_COLLECTION].find(query, projection).to_list(length=None)


def validate_selection(group_id: str, campaign_ids: Optional[list], others: list,
                       campaign_names: dict) -> None:
    """
    Raise ScopeConflict if saving `campaign_ids` for `group_id` is not allowed.

    With siblings present the group must pick an explicit list, and may not
    claim a campaign a sibling already owns. Siblings with a null list are not
    violations (they are split off automatically elsewhere).
    """
    if not others:
        return
    if campaign_ids is None:
        raise ScopeConflict(
            "This ad account is shared with other client groups, so choose the "
            "campaigns this group owns instead of all campaigns."
        )
    for other in others:
        if other.get("id") == group_id:
            continue
        owned = other.get("meta_campaign_ids")
        if owned is None:
            continue
        clash = [c for c in campaign_ids if c in set(owned)]
        if clash:
            who = other.get("name") or other.get("id")
            names = ", ".join(campaign_names.get(c) or c for c in clash)
            raise ScopeConflict(f"{who} already owns {names}.")


def scope_summary(group: dict, others: list) -> Optional[dict]:
    """Scope state for UI: mode, counts, and unclaimed ACTIVE/PAUSED campaigns."""
    if not group.get("meta_ad_account_id"):
        return None
    campaigns = group.get("meta_account_campaigns") or []
    ids = group.get("meta_campaign_ids")
    total = len(campaigns)
    mode = "all" if ids is None else "selected"
    shared = bool(others)

    unassigned = []
    if shared:
        claimed = set(ids or [])
        for o in others:
            claimed.update(o.get("meta_campaign_ids") or [])
        unassigned = [
            {"id": c["id"], "name": c.get("name")}
            for c in campaigns
            if c.get("effective_status") in _UNASSIGNED_STATUSES and c["id"] not in claimed
        ]
    return {
        "mode": mode,
        "selected": total if ids is None else len(ids),
        "total": total,
        "shared": shared,
        "unassigned": unassigned,
    }


async def create_client_groups_scope_indexes(mongo_client):
    """Idempotent index creation for sibling lookups on client_groups."""
    coll = mongo_client[DB_NAME][_COLLECTION]
    await coll.create_index(
        [("user_id", 1), ("meta_ad_account_id", 1)], name="user_meta_ad_account"
    )
