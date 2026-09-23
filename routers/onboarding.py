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
from services import client_targets as client_targets_service
from services import lead_collection as lead_collection_service
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
    "currency",              # ISO code chosen on the client_currency step. Used both as the
                             # new client group's ad_account_currency and as the account-wide
                             # users.default_currency — the sign-up form no longer asks for it,
                             # so this step is the only place it now gets set.
    "first_client",          # {group_id, name, ghl_location_id, meta_ad_account_id}
    "kpi",                   # {cpa, wins, conv_rate, save_default}
    "slack",                 # {channel_id, channel_name, frequency, time, day, brief_items}
    "slack_opt_out",         # True when the user answered "I don't use Slack" — the wizard
                             # then drops the channel/frequency/brief steps entirely rather
                             # than leaving them as a dead end for a user with no workspace.
    "wants_sync",
    "skipped",               # legacy: step keys the user skipped past, written by the wizard
                             # back when it had a "Skip for now" affordance. Read-only now —
                             # kept so accounts mid-wizard when skip was removed still load.
    "pending_import",        # sub-accounts payload picked in ReviewStep, held here while the
                             # mandatory billing step runs — an in-memory-only ref doesn't
                             # survive a checkout redirect or a mid-wait page reload, so this
                             # is what actually gets imported once the subscription confirms.
}

BRIEF_ITEM_KEYS = {"spend", "leads", "conversion", "top", "alerts", "underperform"}

# Currencies the wizard's picker offers — the same twelve the old sign-up form
# listed. Kept here as the server-side guard for data.currency; the frontend
# list in the wizard is the display copy of it.
SUPPORTED_CURRENCIES = {
    "USD", "EUR", "GBP", "CAD", "AUD", "CHF",
    "CNY", "JPY", "INR", "MXN", "AED", "SAR",
}


class OnboardingStateRequest(BaseModel):
    step: Optional[int] = None
    data: Optional[dict] = None


class TargetsRequest(BaseModel):
    """
    Monthly goals for one client — the six the Client Detail design's Targets
    tab specifies, plus `cpa`, which the onboarding wizard already collects.

    Every field is optional and only the ones sent are written, so the wizard
    saving its three cannot blank the three it never asks about. That matters
    more than it looks: `monthly_wins` drives the health band, and wiping it
    reads as "no target", which resolves to Healthy — an account would quietly
    stop being monitored.
    """
    cpa: Optional[float] = None                 # cost per acquisition
    cpl: Optional[float] = None                 # cost per lead
    monthly_wins: Optional[float] = None        # monthly closes — drives health
    monthly_revenue: Optional[float] = None
    monthly_spend: Optional[float] = None
    conversion_rate: Optional[float] = None     # close rate
    aov: Optional[float] = None                 # average order value
    save_as_default: bool = False


# The goal fields, in the order the design's Targets tab lists them. Defined
# in the service so the wizard, the Targets tab and the default-seeding on a
# new client group all read one list.
TARGET_FIELDS = client_targets_service.TARGET_FIELDS


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
    # How this client collects leads, chosen per row in the review step. Left
    # optional so anything still posting the old shape imports as "unknown"
    # rather than 422-ing a whole batch of clients.
    lead_collection_method: Optional[str] = None
    form_provider: Optional[str] = None


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
            {
                "onboarding": 1,
                "integrations.gohighlevel.agency": 1,
                "name": 1,
                "agency_name": 1,
                "default_currency": 1,
            },
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
            # Echoed so the wizard can pre-select the currency step for a user
            # resuming mid-flow, and so the frontend can seed its localStorage
            # copy without a second round-trip.
            "default_currency": user_doc.get("default_currency"),
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
    where the user left off. ``data.name`` / ``data.agency`` / ``data.currency``
    also update the profile fields the rest of the app reads."""
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
        # The sign-up form used to collect this; it now arrives from the
        # wizard's client_currency step instead. Validated against the list the
        # picker offers rather than accepted verbatim, because it lands in
        # users.default_currency, which every money column in the product
        # formats against — a typo'd code would render "$" everywhere with no
        # obvious cause.
        currency = data.get("currency")
        if isinstance(currency, str) and currency.upper() in SUPPORTED_CURRENCIES:
            updates["default_currency"] = currency.upper()

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
    pre-fills every new client.

    Writes field by field rather than replacing the whole `targets` object: the
    onboarding wizard collects three of these and the settings modal collects
    six, and whichever saved last used to blank the other's fields.
    """
    sent = {f: getattr(body, f) for f in TARGET_FIELDS if getattr(body, f) is not None}
    if not sent:
        raise HTTPException(status_code=400, detail="No targets provided")

    updates = {f"targets.{field}": value for field, value in sent.items()}
    updates["targets.updated_at"] = datetime.utcnow()
    updates["updated_at"] = datetime.utcnow()

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        result = await db["client_groups"].update_one(
            {"id": group_id, "user_id": current_user},
            {"$set": updates},
        )
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Client group not found")

        if body.save_as_default:
            # Merged the same way, so saving one field as the agency default
            # does not drop the others already there.
            await db["users"].update_one(
                {"user_id": current_user},
                {"$set": {f"default_targets.{f}": v for f, v in sent.items()}},
                upsert=True,
            )

        stored = await db["client_groups"].find_one(
            {"id": group_id, "user_id": current_user}, {"targets": 1, "_id": 0}
        )
        return {
            "ok": True,
            "targets": (stored or {}).get("targets", {}),
            "saved_as_default": body.save_as_default,
        }


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
# GHL for its recent contacts, so by the time the user reaches the review step
# the table can apply the real activity rules:
#   - a lead in the last 90 days  -> the sub-account appears in the list
#   - a lead in the last 30 days  -> defaults to Active, else Inactive
#   - how many arrived in those 30 days -> shown per row, the quickest read on
#     whether a client is actually live
# It also resolves Facebook ad-account pairings: name similarity across the
# whole table first, then a single OpenAI call (Birdy's own key — free to the
# user) for the sub-accounts similarity could not confidently place.
# Progress is written incrementally to users.onboarding.review_prep so the
# review endpoint can serve partial results while the job runs.

REVIEW_PREP_STALE_MINUTES = 10
AI_MATCH_MODEL = "gpt-4o"


# How many of a location's newest contacts the activity probe reads. The page
# is only ever used to count how many landed in the last 30 days, and an
# account with more than this many new leads in a month is emphatically live —
# so the count saturates here and is reported as "50+" rather than paging on
# to a precise number nobody needs. One page per location either way.
RECENT_PAGE_LIMIT = 50


async def _latest_contact(location_id: str, access_token: str) -> dict:
    """A location's recent lead activity from one /contacts/search call,
    sorted newest-first:

        last_lead_at      most recent contact's dateAdded (ISO), or None
        contact_count     total contacts on the location, all time
        leads_30d         how many of them arrived in the last 30 days
        leads_30d_capped  True when leads_30d hit RECENT_PAGE_LIMIT and the
                          real number is higher

    The 30-day count is done here, over one newest-first page, rather than
    with a server-side date filter: nothing else in the codebase filters GHL
    search by date (see gohighlevel._in_window, which also counts in-window
    client-side), and a filter this endpoint quietly ignored would return the
    all-time total dressed up as a 30-day one — which is the number that
    decides whether a client is shown as Active, so being wrong is worse than
    being coarse.

    Errors degrade to "no activity known" (last_lead_at None) rather than
    "no activity": the caller distinguishes the two by whether it recorded an
    error for the location.
    """
    empty = {"last_lead_at": None, "contact_count": 0, "leads_30d": 0, "leads_30d_capped": False}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://services.leadconnectorhq.com/contacts/search",
                json={
                    "locationId": location_id,
                    "pageLimit": RECENT_PAGE_LIMIT,
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
                return empty
            data = response.json()
            contacts = data.get("contacts", [])
            total = data.get("total", len(contacts))
            last = contacts[0].get("dateAdded") if contacts else None

            cutoff = datetime.utcnow() - timedelta(days=30)
            leads_30d = 0
            for contact in contacts:
                added = _parse_ghl_date(contact.get("dateAdded"))
                # Newest-first, so the first contact older than the cutoff ends
                # the window — but only trust that ordering to stop early, not
                # to skip an undated row.
                if added is None:
                    continue
                if added < cutoff:
                    break
                leads_30d += 1

            return {
                "last_lead_at": last,
                "contact_count": total,
                "leads_30d": leads_30d,
                "leads_30d_capped": leads_30d >= RECENT_PAGE_LIMIT,
            }
    except Exception as e:
        logger.warning(f"latest-contact search failed for {location_id}: {e}")
        return empty


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
                entry: dict[str, Any] = {
                    "last_lead_at": None, "contact_count": 0,
                    "leads_30d": 0, "leads_30d_capped": False, "error": None,
                }
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
                    entry.update(await _latest_contact(location_id, token))
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
            # The AI pass exists to place sub-accounts similarity could not, so
            # it is asked about the ones with no *confident* similarity match —
            # a 0.6-0.8 near-miss is still an open question, and letting the
            # model weigh in on it is the cheaper half of this call.
            similarity = _assign_fb_matches(targets, free_fb)
            unmatched = [
                t for t in targets
                if not (similarity.get(t["location_id"]) or {}).get("confident")
            ]
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


# A pair below the floor is not a candidate at all; a candidate below
# MATCH_CONFIDENT is offered as a suggestion but never pre-filled.
#
# Pre-filling a wrong ad account is the expensive mistake: the row looks
# answered, so nobody reads it, and the client is imported reporting another
# client's spend. An empty dropdown is visibly unanswered and costs one click.
# So the bar for filling the box in is higher than the bar for having an
# opinion. Containment ("Aura" in "Aura — Primary") clears it; a shared
# distinctive word on its own does not.
MATCH_FLOOR = 0.6
MATCH_CONFIDENT = 0.8


def _match_word_counts(fb_accounts: list[dict]) -> dict[str, int]:
    """How many ad accounts lead with each distinctive word. Computed once per
    assignment rather than per location — it is a property of the ad-account
    list, and it was previously rebuilt for every sub-account."""
    counts: dict[str, int] = {}
    for account in fb_accounts:
        word = _first_distinctive_word(account.get("name", ""))
        if word:
            counts[word] = counts.get(word, 0) + 1
    return counts


def _score_fb_match(location_name: str, account: dict, word_counts: dict[str, int]) -> float:
    """Name-similarity score in 0..1 between one GHL sub-account and one Meta
    ad account."""
    loc = _normalise(location_name)
    acc = _normalise(account.get("name", ""))
    if not loc or not acc:
        return 0.0

    score = difflib.SequenceMatcher(None, loc, acc).ratio()
    # Containment either way is a strong signal fuzzy ratio underrates
    # ("Aura" vs "Aura — Primary").
    if loc in acc or acc in loc:
        score = max(score, 0.85)
    # A shared distinctive leading word ("Aura" in "Aura Aesthetics" /
    # "Aura — Primary") only counts if exactly one ad account carries it —
    # two accounts starting with the same word is ambiguity, not a match.
    loc_word = _first_distinctive_word(location_name)
    acc_word = _first_distinctive_word(account.get("name", ""))
    if loc_word and acc_word == loc_word and word_counts.get(loc_word, 0) == 1:
        score = max(score, 0.68)
    return score


def _assign_fb_matches(
    locations: list[dict],
    fb_accounts: list[dict],
    active_location_ids: Optional[set] = None,
) -> dict[str, dict]:
    """Match sub-accounts to ad accounts as one assignment over the whole
    list, rather than per sub-account independently.

    Scoring each sub-account against the full ad-account list on its own —
    which is what this used to do — lets the same ad account be handed to
    several sub-accounts at once. Every one of those rows but one is wrong by
    construction, and the duplicates showed up on exactly the agencies this
    matters to: "Aura — Primary" is the best fuzzy match for "Aura Aesthetics"
    and for "Aura Body", so both got it, and whichever the user didn't notice
    was imported pointing at another client's spend.

    So candidate pairs are ranked globally and consumed one-to-one: the
    strongest pair in the whole grid is settled first, and both sides drop
    out. Scores are bucketed to two decimals before ranking so that a
    genuinely live sub-account wins a contested ad account over a dormant one
    at effectively the same score — the dormant row is the one nobody is
    waiting on, and it stays available in the dropdown either way.

    @param locations            [{location_id, name}]
    @param fb_accounts          ad accounts still free to be assigned
    @param active_location_ids  sub-accounts with recent leads, used only to
                                break ties; None treats them all as equal
    @returns {location_id: {"account": <ad account>, "score": float,
                            "confident": bool}}
    """
    if not locations or not fb_accounts:
        return {}
    active = active_location_ids or set()
    word_counts = _match_word_counts(fb_accounts)

    candidates = []
    for target in locations:
        location_id = target.get("location_id")
        if not location_id:
            continue
        for account in fb_accounts:
            account_id = account.get("id")
            if not account_id:
                continue
            score = _score_fb_match(target.get("name", ""), account, word_counts)
            if score >= MATCH_FLOOR:
                candidates.append((score, location_id, account_id, account))

    candidates.sort(
        key=lambda c: (
            -round(c[0], 2),
            0 if c[1] in active else 1,
            c[1],  # stable across runs for equal pairs, so the table doesn't
                   # reshuffle between two polls of the same data
        )
    )

    matches: dict[str, dict] = {}
    used_accounts: set = set()
    for score, location_id, account_id, account in candidates:
        if location_id in matches or account_id in used_accounts:
            continue
        matches[location_id] = {
            "account": account,
            "score": score,
            "confident": score >= MATCH_CONFIDENT,
        }
        used_accounts.add(account_id)
    return matches


def _fb_payload(account: dict, score: Optional[float]) -> dict:
    return {
        "id": account.get("id"),
        "name": account.get("name", ""),
        "currency": account.get("currency"),
        "score": round(score, 3) if score is not None else None,
    }


def _resolve_pairings(
    matches: dict[str, dict],
    ai_matches: dict[str, str],
    fb_by_id: dict[str, dict],
) -> dict[str, tuple[Optional[dict], Optional[dict]]]:
    """Fold the two sources of a pairing — name similarity and the prep job's
    AI pass — into one (fb_match, fb_suggestion) per sub-account.

    Confident similarity settles first and holds its ad account; the AI pass
    then fills what is left. They are resolved against one shared `taken` set
    rather than independently, because they were computed at different moments
    against different free-account lists: the prep job asked the model about
    the leftovers *it* could not place, and by the time the review endpoint
    runs, similarity may have placed one of them. Letting both write would put
    the same ad account on two rows, which is the thing the assignment exists
    to prevent.

    A near-miss becomes a suggestion, and is withheld once its ad account has
    gone to a row that was sure of it — accepting it would only duplicate.
    """
    resolved: dict[str, dict] = {}
    taken: set = set()

    for location_id, match in matches.items():
        if match["confident"]:
            resolved[location_id] = _fb_payload(match["account"], match["score"])
            taken.add(match["account"].get("id"))

    for location_id, account_id in ai_matches.items():
        account = fb_by_id.get(account_id)
        if not account or location_id in resolved or account_id in taken:
            continue
        resolved[location_id] = _fb_payload(account, None)
        taken.add(account_id)

    pairings: dict[str, tuple[Optional[dict], Optional[dict]]] = {}
    for location_id in set(matches) | set(resolved):
        fb_match = resolved.get(location_id)
        suggestion = None
        match = matches.get(location_id)
        if not fb_match and match and match["account"].get("id") not in taken:
            suggestion = _fb_payload(match["account"], match["score"])
        pairings[location_id] = (fb_match, suggestion)
    return pairings


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
    already imported, a Meta ad-account pairing per sub-account, and — once
    the prep job has run — real activity.

    Pairing comes back in two fields, and the difference between them is the
    point: ``fb_match`` is confident enough to pre-fill the row's dropdown,
    ``fb_suggestion`` is a near-miss offered to the user without being chosen
    for them. Both carry the similarity ``score`` that decided which is which
    (AI matches have no score). Each ad account appears in at most one row —
    the pairing is one assignment over the whole table, not a per-row lookup.

    Activity, per sub-account: ``leads_30d`` (with ``leads_30d_capped`` when
    the real number is higher than the probe counts), ``leads_recent_90``
    (whether the sub-account belongs in the list at all), ``leads_recent_7``
    (pre-ticked for import) and ``status_default`` (Active on a lead in the
    last 30 days; Inactive once the prep job has looked and found none;
    Active while it has not looked yet).

    ``prep`` reports the job's progress so the wizard can poll while it
    finishes."""
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

        # Pass 1 — what the prep job learned about each sub-account. Activity is
        # resolved before matching because it decides which sub-accounts are
        # even in the running for an ad account.
        activity: dict[str, dict] = {}
        for loc in locations:
            location_id = loc.get("id") or loc.get("_id")
            info = prep_accounts.get(location_id) or {}
            answered = bool(info) and not info.get("error")
            last_lead_at = info.get("last_lead_at")
            last_lead = _parse_ghl_date(last_lead_at)
            # None = prep hasn't answered for this location; True/False = real.
            leads_recent_90 = (now - last_lead <= timedelta(days=90)) if last_lead else (
                False if answered else None
            )
            # A sub-account the prep job successfully read and found no recent
            # lead in is Inactive, not Active. It defaulted to Active whenever
            # `last_lead` was missing, which covered both "we have not looked
            # yet" and "we looked and there is nothing" — so a dormant client
            # arrived pre-labelled Active and pre-ticked for import, which is
            # the opposite of what the status column is for. Unknown still
            # defaults to Active: guessing Inactive over a failed lookup would
            # quietly drop a live client out of the import.
            if last_lead is not None:
                status_default = "active" if now - last_lead <= timedelta(days=30) else "inactive"
            else:
                status_default = "inactive" if answered else "active"
            activity[location_id] = {
                "last_lead_at": last_lead_at,
                "contact_count": info.get("contact_count"),
                "leads_30d": info.get("leads_30d"),
                "leads_30d_capped": bool(info.get("leads_30d_capped")),
                "leads_recent_90": leads_recent_90,
                # Drives ReviewStep's default checkbox state — only sub-accounts
                # with a lead this recent are pre-selected for import.
                "leads_recent_7": bool(last_lead and now - last_lead <= timedelta(days=7)),
                "status_default": status_default,
            }

        # Pass 2 — one assignment across every sub-account still up for import.
        # Already-imported ones are left out on both sides: their ad account is
        # already in `used_ad_accounts`, and letting the row itself compete
        # would consume a free ad account on behalf of a client nobody is
        # looking at on this screen.
        pending = [
            {"location_id": loc.get("id") or loc.get("_id"), "name": loc.get("name", "Unknown")}
            for loc in locations
            if (loc.get("id") or loc.get("_id")) not in imported_locations
        ]
        active_ids = {
            location_id for location_id, a in activity.items()
            if a["status_default"] == "active"
        }
        matches = _assign_fb_matches(pending, free_fb_accounts, active_ids)

        pairings = _resolve_pairings(matches, ai_matches, fb_by_id)

        # Pass 3 — the rows themselves.
        accounts = []
        for loc in locations:
            location_id = loc.get("id") or loc.get("_id")
            name = loc.get("name", "Unknown")
            info = activity[location_id]
            fb_match, fb_suggestion = pairings.get(location_id, (None, None))

            accounts.append({
                "location_id": location_id,
                "name": name,
                "already_imported": location_id in imported_locations,
                **{k: info[k] for k in (
                    "last_lead_at", "contact_count", "leads_30d", "leads_30d_capped",
                    "leads_recent_90", "leads_recent_7", "status_default",
                )},
                "fb_match": fb_match,
                "fb_suggestion": fb_suggestion,
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
                "suggested": len([a for a in accounts if a["fb_suggestion"]]),
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

        # Bulk-imported clients inherit the sales-stack answer the user gave in
        # the wizard, the same way the first client does. Hardcoding "ghl" here
        # meant an agency that said "I don't currently call my leads" got one
        # client honestly marked "none" and up to twenty-four claiming a GHL
        # dialler they don't use — so the Sales Hub would show "not available"
        # for the first client and a confident 0 for all the others.
        user_doc = await db["users"].find_one(
            {"user_id": current_user}, {"onboarding.data.sales_tool": 1, "_id": 0}
        )
        sales_tool = (
            ((user_doc or {}).get("onboarding") or {}).get("data") or {}
        ).get("sales_tool")
        provider = {"hp": "hotprospector", "none": "none"}.get(sales_tool, "ghl")

        existing = await groups.find(
            {"user_id": current_user, "ghl_location_id": {"$ne": None}},
            {"ghl_location_id": 1, "_id": 0},
        ).to_list(length=None)
        already = {g["ghl_location_id"] for g in existing}

        # Read once for the batch, not per client. This is the path that made
        # the unread defaults expensive: the wizard collects targets for the
        # first client, offers to save them as the agency default, and then
        # imports up to twenty-four more clients here — none of which used to
        # get them.
        default_targets = await client_targets_service.defaults_for(current_user, db)

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
                "call_log_provider": provider,
                # Declared per client in the review step. An agency's clients do
                # not all collect leads the same way, and guessing a default
                # would silently mis-configure every client nobody looked at.
                "lead_collection": {
                    **lead_collection_service.DEFAULT,
                    "method": lead_collection_service.normalize_method(
                        account.lead_collection_method
                    ),
                    "form_provider": lead_collection_service.normalize_provider(
                        account.form_provider
                    ),
                },
                "notes": "",
                "targets": dict(default_targets),
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
            # A client whose form we can't read from the page needs a webhook to
            # post submissions to, and that needs a secret. Minted here so the
            # portal has one to show the moment the import finishes, rather than
            # making someone come back and press a button to generate it.
            if lead_collection_service.normalize_method(
                account.lead_collection_method
            ) in lead_collection_service.NEEDS_WEBHOOK:
                await groups.update_one(
                    {"id": group_id},
                    {"$set": {
                        "lead_collection.webhook_secret":
                            lead_collection_service.new_webhook_secret(),
                    }},
                )

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
