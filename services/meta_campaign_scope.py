"""
services/meta_campaign_scope.py
-------------------------------
Rules for letting several client groups share one Meta ad account, each
owning chosen campaigns (`client_groups.meta_campaign_ids`; None = all).
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from core.database import DB_NAME
from integrations.facebook_utils._api_helpers import api_get_with_backoff

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v25.0"
# The default /campaigns listing hides archived ones, which would make a
# selected archived campaign look "Removed in Meta".
_LISTED_STATUSES = ["ACTIVE", "PAUSED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES", "CAMPAIGN_PAUSED"]

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


def scope_guard(group_id: str, campaign_ids: Optional[list]) -> dict:
    """
    Filter for writing data fetched under `campaign_ids` back to the group.

    Once the selection changes it matches nothing, so a fetch that was already
    in flight cannot store old-scope numbers or stamp them fresh. On Mongo a
    None value also matches a missing field.
    """
    return {"id": group_id,
            "meta_campaign_ids": None if campaign_ids is None else list(campaign_ids)}


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


async def fetch_account_campaigns(ad_account_id: str, access_token: str) -> Optional[list]:
    """Every campaign on the account (id, name, effective_status), or None on failure."""
    url = f"{_GRAPH}/{ad_account_id}/campaigns"
    params = {
        "access_token": access_token,
        "fields": "id,name,effective_status",
        "effective_status": json.dumps(_LISTED_STATUSES),
        "limit": 500,
    }
    out: list = []
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            next_url = None
            while True:
                # Later pages: Meta embeds token and cursor in `next`; params=None
                # keeps httpx from replacing that query string.
                ok, data = await api_get_with_backoff(
                    client, next_url or url, None if next_url else params
                )
                if not ok or not data:
                    logger.warning("Campaign list fetch failed for %s", ad_account_id)
                    return None
                out.extend(data.get("data", []))
                next_url = (data.get("paging") or {}).get("next")
                if not next_url:
                    return out
    except Exception as e:
        logger.warning("Campaign list fetch errored for %s: %s", ad_account_id, str(e)[:200])
        return None


async def refresh_account_campaigns(db, user_id: str, ad_account_id: str,
                                    access_token: str, max_age_minutes: int = 15) -> None:
    """
    Cache the account's campaign list on every group using the account.
    One Graph call serves all of them; skipped while any group's copy is fresh.
    A failed fetch leaves the previous list in place. Never raises.
    """
    try:
        groups = await db[_COLLECTION].find(
            {"user_id": user_id, "meta_ad_account_id": ad_account_id},
            {"_id": 0, "id": 1, "meta_account_campaigns_at": 1},
        ).to_list(length=None)
        if not groups:
            return
        cutoff = (datetime.utcnow() - timedelta(minutes=max_age_minutes)).isoformat()
        if any((g.get("meta_account_campaigns_at") or "") > cutoff for g in groups):
            return

        campaigns = await fetch_account_campaigns(ad_account_id, access_token)
        if campaigns is None:
            return

        await db[_COLLECTION].update_many(
            {"user_id": user_id, "meta_ad_account_id": ad_account_id},
            {"$set": {
                "meta_account_campaigns": campaigns,
                "meta_account_campaigns_at": datetime.utcnow().isoformat(),
            }},
        )
    except Exception as e:
        logger.warning("refresh_account_campaigns failed for %s: %s", ad_account_id, str(e)[:200])


# ── Writing a selection ────────────────────────────────────────────────────

# Job statuses a refresh can still advance from (see meta_refresh_manager:
# claim_next_jobs only claims these, schedule_stale_groups treats them as busy).
_OPEN_JOB_STATUSES = ["pending", "in_progress", "partial"]
SUPERSEDED = "superseded"
_SCOPED_CACHES = {"facebook_cache": "", "meta_daily_spend": "", "last_meta_refresh": "",
                  "last_meta_static_refresh": ""}

# A lead row whose campaign is not known: field missing, null, or "" (what the
# normalizers store when Meta omits it).
_NO_CAMPAIGN = [None, ""]


async def account_ad_campaigns(db, user_id: str, ad_account_id: str) -> dict:
    """{ad_id: campaign_id} from the cached ad entities of every group on the account."""
    groups = await db[_COLLECTION].find(
        {"user_id": user_id, "meta_ad_account_id": ad_account_id},
        {"_id": 0, "facebook_cache.entities.ads": 1},
    ).to_list(length=None)
    out: dict = {}
    for g in groups:
        ads = (((g.get("facebook_cache") or {}).get("entities") or {}).get("ads")) or []
        for ad in ads:
            if ad.get("id") and ad.get("campaign_id"):
                out[str(ad["id"])] = str(ad["campaign_id"])
    return out


async def _backfill_campaign_ids(leads, base: dict, ad_campaigns: dict) -> None:
    """
    Fill lead_data.campaign_id on rows that lack it, from their ad's campaign.

    Rows written by the incremental lead path before it asked Meta for
    campaign_id carry only ad_id. One update_many per campaign, and only for
    ads that actually have such rows.
    """
    missing = {**base, "lead_data.campaign_id": {"$in": _NO_CAMPAIGN}}
    ad_ids = await leads.distinct("lead_data.ad_id", missing)
    by_campaign: dict = {}
    for ad in ad_ids:
        campaign = ad_campaigns.get(str(ad)) if ad else None
        if campaign:
            by_campaign.setdefault(campaign, []).append(ad)
    for campaign, ads in by_campaign.items():
        await leads.update_many(
            {**missing, "lead_data.ad_id": {"$in": ads}},
            {"$set": {"lead_data.campaign_id": campaign}},
        )


async def reassign_account_leads(db, user_id: str, ad_account_id: str,
                                 ad_campaigns: Optional[dict] = None) -> int:
    """
    Point every cached lead on the account at the group owning its campaign.

    A lone group with a null list owns every lead. Otherwise each list claims
    its campaigns and unclaimed leads become unassigned. One update_many per
    owner plus one for unassigned; rows already correct are not touched.

    Rows without a campaign id are first resolved through their ad
    (`ad_campaigns`, default: the groups' cached ad entities) and backfilled.
    A row that still has no campaign keeps its current owner: not knowing the
    campaign is no evidence that nobody owns it.
    Returns the number of rows modified.
    """
    groups = await db[_COLLECTION].find(
        {"user_id": user_id, "meta_ad_account_id": ad_account_id},
        {"_id": 0, "id": 1, "name": 1, "meta_campaign_ids": 1},
    ).to_list(length=None)
    leads = db["facebook_leads"]
    base = {"user_id": user_id, "ad_account_id": ad_account_id}

    def stale_owner(gid, name):
        return {"$or": [{"client_group_id": {"$ne": gid}}, {"client_group_name": {"$ne": name}}]}

    async def assign(extra: dict, gid, name) -> int:
        res = await leads.update_many(
            {**base, **extra, **stale_owner(gid, name)},
            {"$set": {"client_group_id": gid, "client_group_name": name}},
        )
        return res.modified_count

    if len(groups) == 1 and groups[0].get("meta_campaign_ids") is None:
        g = groups[0]
        return await assign({}, g["id"], g.get("name"))

    if ad_campaigns is None:
        ad_campaigns = await account_ad_campaigns(db, user_id, ad_account_id)
    if ad_campaigns:
        await _backfill_campaign_ids(leads, base, ad_campaigns)

    modified = 0
    claimed: set = set()
    for g in groups:
        ids = g.get("meta_campaign_ids")
        if ids is None:
            continue
        claimed.update(ids)
        if ids:
            modified += await assign(
                {"lead_data.campaign_id": {"$in": list(ids)}}, g["id"], g.get("name"))

    # Legacy accounts linked to several groups with null lists keep the rows
    # those groups already hold; everything else unclaimed is unassigned.
    keep = [g["id"] for g in groups if g.get("meta_campaign_ids") is None]
    unassigned = {"lead_data.campaign_id": {"$nin": list(claimed) + _NO_CAMPAIGN}}
    if keep:
        unassigned["client_group_id"] = {"$nin": keep}
    modified += await assign(unassigned, None, None)
    return modified


def _created_key(group: dict):
    created = group.get("created_at")
    return (created is None, str(created or ""), group.get("id") or "")


async def apply_selection(db, user_id: str, group_id: str, campaign_ids: Optional[list],
                          account_campaigns: list) -> dict:
    """
    Save `campaign_ids` (None = all) for a group and keep its siblings consistent.

    `account_campaigns` is the live [{id, name, effective_status}] list. Raises
    LookupError when the group is not this user's, ScopeConflict on a rule
    violation; both before anything is written. Siblings still on "all" are
    converted to the campaigns nobody else owns (the oldest one gets them,
    any other null sibling gets []). Every changed group loses its cached Meta
    data and open refresh jobs, then the account's leads are reassigned.
    """
    group = await db[_COLLECTION].find_one(
        {"id": group_id, "user_id": user_id},
        {"_id": 0, "id": 1, "name": 1, "meta_ad_account_id": 1},
    )
    if not group:
        raise LookupError(f"Client group {group_id} not found")
    ad_account_id = group.get("meta_ad_account_id")
    if not ad_account_id:
        raise ScopeConflict("Link a Meta ad account to this client group first.")

    if campaign_ids is not None:
        campaign_ids = list(dict.fromkeys(campaign_ids))
    others = await siblings(db, user_id, ad_account_id, exclude_group_id=group_id)
    # Read before the caches below are cleared: they hold the only ad ->
    # campaign map for lead rows stored without a campaign id.
    ad_campaigns = await account_ad_campaigns(db, user_id, ad_account_id)
    names = {c["id"]: c.get("name") for c in account_campaigns if c.get("id")}
    validate_selection(group_id, campaign_ids, others, names)

    updates = [(group_id, campaign_ids)]
    converted = []
    null_others = sorted((o for o in others if o.get("meta_campaign_ids") is None), key=_created_key)
    if null_others:
        taken = set(campaign_ids or [])
        for o in others:
            taken.update(o.get("meta_campaign_ids") or [])
        remainder = list(dict.fromkeys(
            c["id"] for c in account_campaigns if c.get("id") and c["id"] not in taken))
        for i, o in enumerate(null_others):
            updates.append((o["id"], remainder if i == 0 else []))
            converted.append({"id": o["id"], "name": o.get("name")})

    for gid, ids in updates:
        await db[_COLLECTION].update_one(
            {"id": gid, "user_id": user_id},
            {"$set": {"meta_campaign_ids": ids}, "$unset": _SCOPED_CACHES},
        )
    changed = [gid for gid, _ in updates]
    await db["meta_refresh_jobs"].update_many(
        {"group_id": {"$in": changed}, "status": {"$in": _OPEN_JOB_STATUSES}},
        {"$set": {"status": SUPERSEDED, "_claimed_at": None, "updated_at": datetime.utcnow()}},
    )
    await reassign_account_leads(db, user_id, ad_account_id, ad_campaigns)
    return {"changed_group_ids": changed, "converted": converted}


async def on_group_deleted(db, user_id: str, ad_account_id: str,
                           ad_campaigns: Optional[dict] = None) -> None:
    """
    Hand a deleted group's leads to whoever owns their campaigns now.
    Pass `ad_campaigns` read before the delete so the deleted group's cached
    ads still help resolve rows stored without a campaign id.
    """
    await reassign_account_leads(db, user_id, ad_account_id, ad_campaigns)
