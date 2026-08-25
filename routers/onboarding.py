"""
routers/onboarding.py
---------------------
First-run onboarding wizard endpoints.

The wizard itself lives in the frontend (`/onboarding`); this router owns the
durable state it needs:

- ``users.onboarding``  — {completed, step, data, completed_at}. ``data`` is a
  whitelisted scratch dict so an OAuth round-trip (GHL/Meta/Slack) can leave
  the page and resume where the user left off.
- ``users.agency_name`` — collected on the second step.
- ``client_groups.targets`` / ``users.default_targets`` — KPI targets
  (cost-per-acquisition, monthly wins, conversion rate).
- ``users.integrations.slack_bot.brief`` — brief frequency/time/day and which
  sections the morning brief should contain. (The suggestion crons do not read
  this yet — storing it here is the contract for when they do.)

Existing users never see the wizard: a user with no ``onboarding`` field who
already has client groups or a GHL connection is grandfathered as completed on
first read.

Bulk sub-account import inserts the client_group documents synchronously and
mints GHL location tokens in a background task; the ghl/meta/hp cron ticks
then backfill data because ``last_*_refresh`` is None — the same contract the
single-client path relies on for HP.
"""

import difflib
import logging
import re
from datetime import datetime
from typing import Any, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from billing_middleware import check_client_limit
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.gohighlevel import (
    ghl_integration,
    get_agency_token,
    fetch_location_details,
    get_contact_count_from_ghl,
    save_subaccount_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Keys the wizard may persist in users.onboarding.data. Anything else in the
# payload is dropped so the scratch dict can't grow unbounded or be abused.
ALLOWED_DATA_KEYS = {
    "name", "agency", "sales_tool",
    "first_client",          # {group_id, name, ghl_location_id, meta_ad_account_id}
    "kpi",                   # {cpa, wins, conv_rate, save_default}
    "slack",                 # {channel_id, channel_name, frequency, time, day, brief_items}
    "wants_sync",
}

BRIEF_ITEM_KEYS = {"spend", "leads", "conversion", "top", "alerts", "underperform"}


class OnboardingStateRequest(BaseModel):
    step: Optional[int] = None
    data: Optional[dict] = None


class TargetsRequest(BaseModel):
    cpa: Optional[float] = None
    monthly_wins: Optional[float] = None
    conversion_rate: Optional[float] = None
    save_as_default: bool = False


class BriefConfigRequest(BaseModel):
    frequency: str                      # "daily" | "weekly"
    time: Optional[str] = None          # e.g. "9:00 AM"
    day: Optional[str] = None           # e.g. "Monday" (weekly only)
    items: Optional[dict] = None        # {spend: bool, leads: bool, ...}


class ImportAccount(BaseModel):
    location_id: str
    name: str
    meta_ad_account_id: Optional[str] = None
    ad_account_currency: Optional[str] = None
    client_status: Optional[str] = "Active"


class ImportSubaccountsRequest(BaseModel):
    accounts: list[ImportAccount]


# ---------------------------------------------------------------------------
# GET /api/onboarding/status
# ---------------------------------------------------------------------------

@router.get("/api/onboarding/status")
async def onboarding_status(current_user: str = Depends(get_current_user)):
    """The wizard gate. Users created before onboarding existed are
    grandfathered as completed the first time this is read."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        user_doc = await db["users"].find_one(
            {"user_id": current_user},
            {"onboarding": 1, "integrations.gohighlevel.agency": 1, "name": 1, "agency_name": 1},
        )
        if not user_doc:
            raise HTTPException(status_code=404, detail="User not found")

        onboarding = user_doc.get("onboarding")
        if onboarding is None:
            has_groups = await db["client_groups"].count_documents(
                {"user_id": current_user}, limit=1
            )
            has_ghl = bool(
                ((user_doc.get("integrations") or {}).get("gohighlevel") or {}).get("agency")
            )
            if has_groups or has_ghl:
                onboarding = {
                    "completed": True,
                    "grandfathered": True,
                    "completed_at": datetime.utcnow(),
                }
                await db["users"].update_one(
                    {"user_id": current_user}, {"$set": {"onboarding": onboarding}}
                )
            else:
                onboarding = {"completed": False, "step": 0, "data": {}}

        return {
            "completed": bool(onboarding.get("completed")),
            "step": onboarding.get("step", 0),
            "data": onboarding.get("data", {}),
            "name": user_doc.get("name"),
            "agency_name": user_doc.get("agency_name"),
        }


# ---------------------------------------------------------------------------
# PUT /api/onboarding/state
# ---------------------------------------------------------------------------

@router.put("/api/onboarding/state")
async def save_onboarding_state(
    body: OnboardingStateRequest,
    current_user: str = Depends(get_current_user),
):
    """Persist wizard progress so an OAuth redirect (or a closed tab) resumes
    where the user left off. ``data.name`` / ``data.agency`` also update the
    profile fields the rest of the app reads."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        updates: dict[str, Any] = {"updated_at": datetime.utcnow()}

        if body.step is not None:
            updates["onboarding.step"] = max(0, int(body.step))

        data = body.data or {}
        for key, value in data.items():
            if key in ALLOWED_DATA_KEYS:
                updates[f"onboarding.data.{key}"] = value

        if isinstance(data.get("name"), str) and data["name"].strip():
            updates["name"] = data["name"].strip()
        if isinstance(data.get("agency"), str) and data["agency"].strip():
            updates["agency_name"] = data["agency"].strip()

        result = await db["users"].update_one(
            {"user_id": current_user}, {"$set": updates}
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
        return {"ok": True}


# ---------------------------------------------------------------------------
# POST /api/onboarding/complete
# ---------------------------------------------------------------------------

@router.post("/api/onboarding/complete")
async def complete_onboarding(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        result = await db["users"].update_one(
            {"user_id": current_user},
            {
                "$set": {
                    "onboarding.completed": True,
                    "onboarding.completed_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
        logger.info(f"Onboarding completed for {current_user}")
        return {"ok": True}


# ---------------------------------------------------------------------------
# PUT /api/client-groups/{group_id}/targets
# ---------------------------------------------------------------------------

@router.put("/api/client-groups/{group_id}/targets")
async def set_client_targets(
    group_id: str,
    body: TargetsRequest,
    current_user: str = Depends(get_current_user),
):
    """KPI targets for one client, optionally saved as the agency default that
    pre-fills every new client."""
    targets = {
        "cpa": body.cpa,
        "monthly_wins": body.monthly_wins,
        "conversion_rate": body.conversion_rate,
        "updated_at": datetime.utcnow(),
    }
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        result = await db["client_groups"].update_one(
            {"id": group_id, "user_id": current_user},
            {"$set": {"targets": targets, "updated_at": datetime.utcnow()}},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Client group not found")

        if body.save_as_default:
            await db["users"].update_one(
                {"user_id": current_user},
                {"$set": {"default_targets": targets}},
            )
        return {"ok": True, "targets": targets, "saved_as_default": body.save_as_default}


# ---------------------------------------------------------------------------
# PUT /api/integrations/slack/brief
# ---------------------------------------------------------------------------

@router.put("/api/integrations/slack/brief")
async def set_slack_brief(
    body: BriefConfigRequest,
    current_user: str = Depends(get_current_user),
):
    if body.frequency not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="frequency must be 'daily' or 'weekly'")

    items = {
        key: bool((body.items or {}).get(key, key != "underperform"))
        for key in BRIEF_ITEM_KEYS
    }
    brief = {
        "frequency": body.frequency,
        "time": body.time or "9:00 AM",
        "day": (body.day or "Monday") if body.frequency == "weekly" else None,
        "items": items,
        "updated_at": datetime.utcnow(),
    }
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        user_doc = await db["users"].find_one(
            {"user_id": current_user}, {"integrations.slack_bot": 1}
        )
        if not user_doc or not ((user_doc.get("integrations") or {}).get("slack_bot")):
            raise HTTPException(status_code=404, detail="Slack bot not connected")

        await db["users"].update_one(
            {"user_id": current_user},
            {"$set": {"integrations.slack_bot.brief": brief}},
        )
        return {"ok": True, "brief": brief}


# ---------------------------------------------------------------------------
# GET /api/onboarding/subaccounts-review
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _best_fb_match(location_name: str, fb_accounts: list[dict]) -> Optional[dict]:
    """Best name-similarity match between a GHL sub-account and a Meta ad
    account. Conservative threshold — a wrong match is worse than no match,
    since the user can pick from the dropdown."""
    loc = _normalise(location_name)
    if not loc:
        return None
    best, best_score = None, 0.0
    for account in fb_accounts:
        acc = _normalise(account.get("name", ""))
        if not acc:
            continue
        score = difflib.SequenceMatcher(None, loc, acc).ratio()
        # Containment either way is a strong signal fuzzy ratio underrates
        # ("Aura" vs "Aura — Primary").
        if loc in acc or acc in loc:
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = account, score
    return best if best_score >= 0.6 else None


async def _fetch_fb_accounts(current_user: str, mongo_client) -> list[dict]:
    """Live ad-account list; degrades to [] if Meta isn't connected."""
    token = await get_facebook_token(current_user, mongo_client)
    if not token or not token.get("access_token"):
        return []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://graph.facebook.com/v25.0/me/adaccounts",
                params={
                    "fields": "name,currency",
                    "access_token": token["access_token"],
                    "limit": 1000,
                },
            )
            if response.status_code != 200:
                logger.warning(f"adaccounts fetch failed during onboarding review: {response.status_code}")
                return []
            return response.json().get("data", [])
    except Exception as e:
        logger.warning(f"adaccounts fetch failed during onboarding review: {e}")
        return []


@router.get("/api/onboarding/subaccounts-review")
async def subaccounts_review(current_user: str = Depends(get_current_user)):
    """Everything the review table needs: all GHL sub-accounts, which are
    already imported, and a suggested Meta ad-account match per sub-account.

    Lead-recency (the 90/30-day activity rule) needs synced contact data we
    don't have before import, so status defaults to Active and the user
    adjusts; the rule can move server-side once a post-import job exists.
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        agency_token = await get_agency_token(current_user, mongo_client)
        if not agency_token:
            raise HTTPException(status_code=400, detail="No agency token available. Connect GoHighLevel first.")
        company_id = agency_token.get("company_id")
        access_token = agency_token.get("access_token")
        if not company_id or not access_token:
            raise HTTPException(status_code=400, detail="Invalid agency token")

        success, locations = await ghl_integration.fetch_locations(company_id, access_token)
        if not success:
            raise HTTPException(
                status_code=locations.get("status_code", 400),
                detail=f"Failed to fetch locations: {locations.get('error', 'Unknown error')}",
            )

        existing = await db["client_groups"].find(
            {"user_id": current_user},
            {"ghl_location_id": 1, "meta_ad_account_id": 1, "name": 1, "_id": 0},
        ).to_list(length=None)
        imported_locations = {g.get("ghl_location_id") for g in existing if g.get("ghl_location_id")}
        used_ad_accounts = {g.get("meta_ad_account_id") for g in existing if g.get("meta_ad_account_id")}

        fb_accounts = await _fetch_fb_accounts(current_user, mongo_client)
        free_fb_accounts = [a for a in fb_accounts if a.get("id") not in used_ad_accounts]

        accounts = []
        for loc in locations:
            location_id = loc.get("id") or loc.get("_id")
            name = loc.get("name", "Unknown")
            match = _best_fb_match(name, free_fb_accounts)
            accounts.append({
                "location_id": location_id,
                "name": name,
                "already_imported": location_id in imported_locations,
                "fb_match": {
                    "id": match["id"],
                    "name": match.get("name", ""),
                    "currency": match.get("currency"),
                } if match else None,
            })

        return {
            "accounts": accounts,
            "fb_accounts": [
                {"id": a.get("id"), "name": a.get("name", ""), "currency": a.get("currency")}
                for a in fb_accounts
            ],
            "stats": {
                "accounts_found": len(accounts),
                "already_imported": len([a for a in accounts if a["already_imported"]]),
                "matched": len([a for a in accounts if a["fb_match"]]),
            },
        }


# ---------------------------------------------------------------------------
# POST /api/onboarding/import-subaccounts
# ---------------------------------------------------------------------------

async def _mint_location_tokens(user_id: str, imports: list[dict]):
    """Background: mint + save a GHL location token per imported group so the
    ghl-tick cron can fetch its data. Failures mark the group so the UI can
    surface them instead of a silent forever-pending row."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agency_token = await get_agency_token(user_id, mongo_client)
        if not agency_token:
            logger.error(f"bulk import token minting: no agency token for {user_id}")
            return
        company_id = agency_token.get("company_id")
        access_token = agency_token.get("access_token")

        for item in imports:
            group_id, location_id = item["group_id"], item["location_id"]
            try:
                success, loc_tokens = await ghl_integration.generate_location_token(
                    company_id, location_id, access_token
                )
                if not success:
                    raise RuntimeError(loc_tokens.get("error", "token generation failed"))
                location_details = await fetch_location_details(
                    location_id, loc_tokens.get("access_token")
                )
                contact_count = await get_contact_count_from_ghl(
                    location_id, loc_tokens.get("access_token")
                )
                await save_subaccount_token(
                    user_id, location_id, loc_tokens, mongo_client,
                    location_details, contact_count=contact_count,
                )
                await db["client_groups"].update_one(
                    {"id": group_id},
                    {"$set": {
                        "status": "complete",
                        "status_message": "Imported — historical data syncing in the background",
                    }},
                )
            except Exception as e:
                logger.error(f"bulk import failed for location {location_id}: {e}")
                await db["client_groups"].update_one(
                    {"id": group_id},
                    {"$set": {
                        "status": "complete",
                        "status_message": f"Imported, but GHL access failed: {e}",
                    }},
                )


@router.post("/api/onboarding/import-subaccounts")
async def import_subaccounts(
    body: ImportSubaccountsRequest,
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
):
    """Bulk-create client groups from GHL sub-accounts. Documents are inserted
    immediately (so the Clients page shows them); location tokens are minted in
    a background task; historical GHL/Meta/HP data arrives via the cron ticks,
    which treat last_*_refresh=None as maximally stale."""
    if not body.accounts:
        raise HTTPException(status_code=400, detail="No accounts to import")

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        groups = db["client_groups"]

        existing = await groups.find(
            {"user_id": current_user, "ghl_location_id": {"$ne": None}},
            {"ghl_location_id": 1, "_id": 0},
        ).to_list(length=None)
        already = {g["ghl_location_id"] for g in existing}

        imported, skipped_existing, skipped_limit = [], [], []
        seen_this_batch = set()
        for account in body.accounts:
            if account.location_id in already or account.location_id in seen_this_batch:
                skipped_existing.append(account.location_id)
                continue
            try:
                await check_client_limit(current_user, mongo_client)
            except HTTPException:
                skipped_limit.append(account.location_id)
                continue

            group_id = f"{current_user}_{int(datetime.now().timestamp() * 1000)}_{len(imported)}"
            client_status = "Inactive" if (account.client_status or "").lower() == "inactive" else "Active"
            await groups.insert_one({
                "id": group_id,
                "user_id": current_user,
                "name": account.name,
                "ad_account_currency": account.ad_account_currency,
                "ghl_location_id": account.location_id,
                "meta_ad_account_id": account.meta_ad_account_id,
                "hotprospector_group_id": None,
                "call_log_provider": "ghl",
                "notes": "",
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "status": "creating",
                "status_message": "Queued for import...",
                "gohighlevel_cache": {},
                "facebook_cache": {},
                "hotprospector_cache": {},
                "hotprospector_call_cache": {},
                "last_ghl_refresh": None,
                "last_meta_refresh": None,
                "last_hp_refresh": None,
                "client_status": client_status,
            })
            seen_this_batch.add(account.location_id)
            imported.append({"group_id": group_id, "location_id": account.location_id})

        if imported:
            background_tasks.add_task(_mint_location_tokens, current_user, imported)

        return {
            "imported": imported,
            "skipped_existing": skipped_existing,
            "skipped_limit": skipped_limit,
            "message": f"Importing {len(imported)} sub-accounts",
        }
