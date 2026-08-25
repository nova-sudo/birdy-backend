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
import json
import logging
import os
import re
from datetime import datetime, timedelta
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
    get_subaccount_tokens,
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
    "skipped",               # step keys the user skipped past (un-set if later completed)
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
# Review preparation — the "working in the background" job
# ---------------------------------------------------------------------------
#
# Kicked off when the user accepts background sync ("Yes, add all my clients").
# For every sub-account not yet imported it mints a GHL location token and asks
# GHL for the most-recent contact, so by the time the user reaches the review
# step the table can apply the real activity rules:
#   - a lead in the last 90 days  -> the sub-account appears in the list
#   - a lead in the last 30 days  -> defaults to Active, else Inactive
# It also resolves Facebook ad-account matches: name similarity first, then a
# single Anthropic call (Birdy's own key — free to the user) for the leftovers.
# Progress is written incrementally to users.onboarding.review_prep so the
# review endpoint can serve partial results while the job runs.

REVIEW_PREP_STALE_MINUTES = 10
AI_MATCH_MODEL = "gpt-4o"


async def _latest_contact(location_id: str, access_token: str) -> tuple[Optional[str], int]:
    """(most recent contact's dateAdded ISO string, total contacts) for a
    location — one /contacts/search call sorted newest-first, pageLimit 1."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://services.leadconnectorhq.com/contacts/search",
                json={
                    "locationId": location_id,
                    "pageLimit": 1,
                    "page": 1,
                    "sort": [{"field": "dateAdded", "direction": "desc"}],
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Version": "2021-07-28",
                    "Accept": "application/json",
                },
            )
            if response.status_code != 200:
                logger.warning(f"latest-contact search failed for {location_id}: {response.status_code}")
                return None, 0
            data = response.json()
            contacts = data.get("contacts", [])
            total = data.get("total", len(contacts))
            last = contacts[0].get("dateAdded") if contacts else None
            return last, total
    except Exception as e:
        logger.warning(f"latest-contact search failed for {location_id}: {e}")
        return None, 0


async def _ai_match_accounts(unmatched: list[dict], fb_accounts: list[dict]) -> dict[str, str]:
    """Match remaining GHL sub-accounts to Meta ad accounts with one call to
    Birdy's own OpenAI account (OPENAI_API_KEY — not the users' BYOK keys).
    Returns {location_id: ad_account_id}; degrades to {} on any failure —
    similarity matches already cover the obvious cases, so this only ever
    adds."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not unmatched or not fb_accounts:
        return {}
    if not api_key:
        logger.warning("AI account matching skipped: OPENAI_API_KEY is not set")
        return {}
    try:
        from openai import AsyncOpenAI

        subaccount_lines = "\n".join(f"- {u['location_id']}: {u['name']}" for u in unmatched[:100])
        account_lines = "\n".join(f"- {a['id']}: {a.get('name', '')}" for a in fb_accounts[:200])
        prompt = (
            "You match a marketing agency's client businesses (GoHighLevel sub-accounts) "
            "to their Facebook ad accounts by name. Names rarely match exactly — expect "
            "abbreviations, extra words like 'Primary'/'Retargeting'/'Ltd', and casing noise.\n\n"
            f"Sub-accounts (id: name):\n{subaccount_lines}\n\n"
            f"Ad accounts (id: name):\n{account_lines}\n\n"
            "Reply with ONLY a JSON object mapping sub-account id to ad account id, for "
            "confident matches only. Never map two sub-accounts to the same ad account. "
            "Omit sub-accounts with no plausible match. No prose, no code fences."
        )
        client = AsyncOpenAI(api_key=api_key)
        completion = await client.chat.completions.create(
            model=AI_MATCH_MODEL,
            max_tokens=2000,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = (completion.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        raw = json.loads(text)
        valid_locations = {u["location_id"] for u in unmatched}
        valid_accounts = {a["id"] for a in fb_accounts}
        matches, used = {}, set()
        for loc, acc in raw.items():
            if loc in valid_locations and acc in valid_accounts and acc not in used:
                matches[loc] = acc
                used.add(acc)
        logger.info(f"AI account matching resolved {len(matches)}/{len(unmatched)} leftovers")
        return matches
    except Exception as e:
        logger.warning(f"AI account matching skipped: {e}")
        return {}


async def _prepare_review_job(user_id: str):
    """Background task behind POST /api/onboarding/prepare-review."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        users = db["users"]
        prep_key = "onboarding.review_prep"

        try:
            agency_token = await get_agency_token(user_id, mongo_client)
            if not agency_token:
                raise RuntimeError("no agency token")
            company_id = agency_token.get("company_id")
            access_token = agency_token.get("access_token")

            success, locations = await ghl_integration.fetch_locations(company_id, access_token)
            if not success:
                raise RuntimeError(f"fetch_locations failed: {locations.get('error')}")

            existing = await db["client_groups"].find(
                {"user_id": user_id}, {"ghl_location_id": 1, "_id": 0}
            ).to_list(length=None)
            imported = {g.get("ghl_location_id") for g in existing}
            targets = [
                {"location_id": loc.get("id") or loc.get("_id"), "name": loc.get("name", "Unknown")}
                for loc in locations
                if (loc.get("id") or loc.get("_id")) not in imported
            ]

            await users.update_one(
                {"user_id": user_id},
                {"$set": {prep_key: {
                    "status": "running",
                    "started_at": datetime.utcnow(),
                    "done": 0,
                    "total": len(targets),
                    "accounts": {},
                    "ai_matches": {},
                }}},
            )

            existing_tokens = await get_subaccount_tokens(user_id, mongo_client) or {}

            for i, target in enumerate(targets):
                location_id = target["location_id"]
                entry: dict[str, Any] = {"last_lead_at": None, "contact_count": 0, "error": None}
                try:
                    token = (existing_tokens.get(location_id) or {}).get("access_token")
                    if not token:
                        ok, loc_tokens = await ghl_integration.generate_location_token(
                            company_id, location_id, access_token
                        )
                        if not ok:
                            raise RuntimeError(loc_tokens.get("error", "token generation failed"))
                        token = loc_tokens.get("access_token")
                        location_details = await fetch_location_details(location_id, token)
                        await save_subaccount_token(
                            user_id, location_id, loc_tokens, mongo_client, location_details,
                        )
                    last_lead_at, contact_count = await _latest_contact(location_id, token)
                    entry["last_lead_at"] = last_lead_at
                    entry["contact_count"] = contact_count
                except Exception as e:
                    entry["error"] = str(e)
                    logger.warning(f"review prep failed for location {location_id}: {e}")

                await users.update_one(
                    {"user_id": user_id},
                    {"$set": {
                        f"{prep_key}.accounts.{location_id}": entry,
                        f"{prep_key}.done": i + 1,
                    }},
                )

            # Facebook matching: similarity first, AI (house key) for leftovers.
            fb_accounts = await _fetch_fb_accounts(user_id, mongo_client)
            used_ad_accounts = {g.get("meta_ad_account_id") for g in await db["client_groups"].find(
                {"user_id": user_id}, {"meta_ad_account_id": 1, "_id": 0}
            ).to_list(length=None) if g.get("meta_ad_account_id")}
            free_fb = [a for a in fb_accounts if a.get("id") not in used_ad_accounts]
            unmatched = [t for t in targets if not _best_fb_match(t["name"], free_fb)]
            ai_matches = await _ai_match_accounts(unmatched, free_fb)

            await users.update_one(
                {"user_id": user_id},
                {"$set": {
                    f"{prep_key}.ai_matches": ai_matches,
                    f"{prep_key}.status": "complete",
                    f"{prep_key}.completed_at": datetime.utcnow(),
                }},
            )
            logger.info(f"review prep complete for {user_id}: {len(targets)} locations, {len(ai_matches)} AI matches")
        except Exception as e:
            logger.error(f"review prep failed for {user_id}: {e}", exc_info=True)
            await users.update_one(
                {"user_id": user_id},
                {"$set": {f"{prep_key}.status": "error", f"{prep_key}.error": str(e)}},
            )


@router.post("/api/onboarding/prepare-review")
async def prepare_review(
    background_tasks: BackgroundTasks,
    current_user: str = Depends(get_current_user),
):
    """Start (or no-op if already running) the background review-prep job."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        user_doc = await db["users"].find_one(
            {"user_id": current_user}, {"onboarding.review_prep": 1}
        )
        prep = ((user_doc or {}).get("onboarding") or {}).get("review_prep") or {}
        if prep.get("status") == "running":
            started = prep.get("started_at")
            if started and datetime.utcnow() - started < timedelta(minutes=REVIEW_PREP_STALE_MINUTES):
                return {"started": False, "status": "running"}
        background_tasks.add_task(_prepare_review_job, current_user)
        return {"started": True, "status": "running"}


# ---------------------------------------------------------------------------
# GET /api/onboarding/subaccounts-review
# ---------------------------------------------------------------------------

def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


# Generic words that can't identify a business on their own.
_MATCH_STOPWORDS = {
    "the", "and", "ltd", "limited", "llc", "inc", "co", "uk", "primary",
    "main", "retargeting", "retarget", "broad", "lookalike", "clinic",
    "studio", "aesthetics", "beauty", "body",
}


def _first_distinctive_word(name: str) -> Optional[str]:
    for word in _normalise(name).split():
        if len(word) >= 3 and word not in _MATCH_STOPWORDS:
            return word
    return None


def _best_fb_match(location_name: str, fb_accounts: list[dict]) -> Optional[dict]:
    """Best name-similarity match between a GHL sub-account and a Meta ad
    account. Conservative threshold — a wrong match is worse than no match,
    since the user can pick from the dropdown (and the AI pass catches the
    rest)."""
    loc = _normalise(location_name)
    if not loc:
        return None
    loc_word = _first_distinctive_word(location_name)
    # A shared distinctive leading word ("Aura" in "Aura Aesthetics" /
    # "Aura — Primary") only counts if exactly one ad account carries it —
    # two accounts starting with the same word is ambiguity, not a match.
    word_counts: dict[str, int] = {}
    for account in fb_accounts:
        word = _first_distinctive_word(account.get("name", ""))
        if word:
            word_counts[word] = word_counts.get(word, 0) + 1

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
        acc_word = _first_distinctive_word(account.get("name", ""))
        if loc_word and acc_word == loc_word and word_counts.get(loc_word, 0) == 1:
            score = max(score, 0.68)
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


def _parse_ghl_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


@router.get("/api/onboarding/subaccounts-review")
async def subaccounts_review(current_user: str = Depends(get_current_user)):
    """Everything the review table needs: all GHL sub-accounts, which are
    already imported, a Meta ad-account match per sub-account (name similarity
    + the prep job's AI matches), and — once the prep job has run — real
    activity flags: ``leads_recent_90`` (whether the sub-account belongs in
    the list at all) and ``status_default`` (Active on a lead in the last 30
    days, Inactive otherwise). ``prep`` reports the job's progress so the
    wizard can poll while it finishes."""
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

        user_doc = await db["users"].find_one(
            {"user_id": current_user}, {"onboarding.review_prep": 1}
        )
        prep = ((user_doc or {}).get("onboarding") or {}).get("review_prep") or {}
        prep_accounts = prep.get("accounts") or {}
        ai_matches = prep.get("ai_matches") or {}

        fb_accounts = await _fetch_fb_accounts(current_user, mongo_client)
        free_fb_accounts = [a for a in fb_accounts if a.get("id") not in used_ad_accounts]
        fb_by_id = {a.get("id"): a for a in fb_accounts}

        now = datetime.utcnow()
        accounts = []
        for loc in locations:
            location_id = loc.get("id") or loc.get("_id")
            name = loc.get("name", "Unknown")

            match = _best_fb_match(name, free_fb_accounts)
            if not match and location_id in ai_matches:
                match = fb_by_id.get(ai_matches[location_id])

            info = prep_accounts.get(location_id)
            last_lead_at = (info or {}).get("last_lead_at")
            last_lead = _parse_ghl_date(last_lead_at)
            # None = prep hasn't answered for this location; True/False = real.
            leads_recent_90 = (now - last_lead <= timedelta(days=90)) if last_lead else (
                False if info and not (info or {}).get("error") else None
            )
            status_default = "active"
            if last_lead is not None:
                status_default = "active" if now - last_lead <= timedelta(days=30) else "inactive"

            accounts.append({
                "location_id": location_id,
                "name": name,
                "already_imported": location_id in imported_locations,
                "last_lead_at": last_lead_at,
                "contact_count": (info or {}).get("contact_count"),
                "leads_recent_90": leads_recent_90,
                "status_default": status_default,
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
            "prep": {
                "status": prep.get("status") or "not_started",
                "done": prep.get("done", 0),
                "total": prep.get("total", 0),
            },
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
        existing_tokens = await get_subaccount_tokens(user_id, mongo_client) or {}

        for item in imports:
            group_id, location_id = item["group_id"], item["location_id"]
            try:
                # The review-prep job usually minted this token already.
                existing = existing_tokens.get(location_id) or {}
                expires_at = existing.get("expires_at")
                token_fresh = bool(existing.get("access_token")) and (
                    expires_at is None or expires_at > datetime.now()
                )
                if not token_fresh:
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
