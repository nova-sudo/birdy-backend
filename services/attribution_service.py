"""
services/attribution_service.py
-------------------------------
Ad → click → lead attribution.

Birdy already knows what happened on Meta (facebook_ad_insights) and what
happened in the CRM (ghl_contacts, opportunities). What it has never had is
the join between them: *which ad produced this contact*. Meta's own reporting
can't answer that once the lead lands anywhere other than a Lead Ad, and the
form provider in the middle usually can't either.

This service owns the missing middle. Two collections:

    attribution_visitors   one row per anonymous browser, holding the Meta
                           identifiers that were on the URL when they landed,
                           plus the email/phone once they identify themselves.

    attribution_matches    one row per (client group, GHL contact) — the
                           durable statement "this contact came from ad X".

The join is deliberately *not* the form's responsibility. Two paths reach the
same row, and either alone is enough:

    deterministic   the form carried our visitor_id through (hidden field or
                    URL parameter) — nothing to guess.
    identity        the visitor typed an email/phone on our page, and the CRM
                    contact carries the same one. Reuses `match_keys`, the
                    normalized email/phone keys already stamped on every
                    ghl_contacts row for cross-source joins.

There is no probabilistic/fingerprint matching here on purpose. An attribution
product that quietly guesses is worse than one that says "unattributed": these
numbers get shown to the agency's own client.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import datetime, timedelta

from core.database import DB_NAME
from utils.phone_normalize import compute_match_keys, normalize_email, normalize_phone

logger = logging.getLogger(__name__)


VISITORS = "attribution_visitors"
MATCHES = "attribution_matches"

# Identifiers we keep off the landing URL. `ad_id` is the one that matters —
# the ad/adset/campaign *names* are looked up from Meta at read time, so a
# customer mistyping an ad name into a UTM can't corrupt a report.
TOUCH_FIELDS = (
    "ad_id", "adset_id", "campaign_id",
    "fbclid", "gclid", "ttclid",
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "landing_page", "referrer",
)

# Any single value longer than this is truncated before storage — these
# endpoints are public, so nothing from the wire is trusted for length.
MAX_VALUE_LEN = 500
MAX_ID_LEN = 100

# Anonymous browsing history is not the product; the match is. Visitors that
# stop being seen age out, while attribution_matches rows are permanent.
VISITOR_TTL_DAYS = 180

# A visitor can identify before the hourly GHL sync has pulled their contact
# in, so an unmatched-but-identified visitor is retried rather than dropped.
# 24 attempts at 30-minute ticks ≈ 12 hours of grace.
MAX_MATCH_ATTEMPTS = 24

# A contact created *before* the click can't have been produced by it. We keep
# the match (it's the same person, and the touch is still real) but flag it, so
# "leads from this ad" can exclude re-engaged existing contacts.
CLICK_SLACK = timedelta(hours=1)

_SITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_VISITOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ---------------------------------------------------------------------------
# Site keys — the public per-client-group identifier baked into the snippet
# ---------------------------------------------------------------------------

# site_id → client_group summary. The collect endpoint is a public firehose;
# without this every pageview on every client's site costs a Mongo round trip.
_site_cache: dict[str, tuple[float, dict | None]] = {}
_SITE_CACHE_TTL_SECONDS = 300


def _new_site_id() -> str:
    return secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24]


async def ensure_site_id(group: dict, mongo_client) -> str:
    """Return this client group's tracking site id, minting one on first use."""
    existing = group.get("attribution_site_id")
    if existing:
        return existing

    site_id = _new_site_id()
    await mongo_client[DB_NAME]["client_groups"].update_one(
        {"id": group["id"]},
        {"$set": {"attribution_site_id": site_id, "updated_at": datetime.utcnow()}},
    )
    logger.info("Minted attribution site_id for group %s", group.get("id"))
    return site_id


async def resolve_site(site_id: str | None, mongo_client) -> dict | None:
    """
    Map a site_id from the wire to {site_id, client_group_id, user_id, location_id}.

    Returns None for anything unknown or malformed — the caller answers 204
    either way, so a scraped snippet pointed at a dead account is silently
    inert rather than an error a stranger can probe.
    """
    if not site_id or not _SITE_ID_RE.match(site_id):
        return None

    cached = _site_cache.get(site_id)
    now = time.monotonic()
    if cached and now - cached[0] < _SITE_CACHE_TTL_SECONDS:
        return cached[1]

    group = await mongo_client[DB_NAME]["client_groups"].find_one(
        {"attribution_site_id": site_id},
        projection={"id": 1, "user_id": 1, "ghl_location_id": 1, "name": 1},
    )
    site = None
    if group:
        site = {
            "site_id": site_id,
            "client_group_id": group.get("id"),
            "client_group_name": group.get("name"),
            "user_id": group.get("user_id"),
            "location_id": group.get("ghl_location_id"),
        }
    _site_cache[site_id] = (now, site)
    return site


def _forget_site(site_id: str) -> None:
    """Drop a cached site mapping (used by tests and site-key rotation)."""
    _site_cache.pop(site_id, None)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _clip(value, limit: int = MAX_VALUE_LEN) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] if value else None


def valid_visitor_id(visitor_id: str | None) -> bool:
    return bool(visitor_id and _VISITOR_ID_RE.match(visitor_id))


def clean_touch(raw: dict | None) -> dict:
    """Keep only the identifiers we report on, clipped and stripped of blanks."""
    raw = raw if isinstance(raw, dict) else {}
    touch = {}
    for field in TOUCH_FIELDS:
        value = _clip(raw.get(field))
        if value:
            touch[field] = value
    return touch


def is_paid_touch(touch: dict) -> bool:
    """A touch worth attributing to: it carries an ad click, not just a visit."""
    return bool(touch.get("ad_id") or touch.get("fbclid") or touch.get("utm_source"))


def attributed_touch(visitor: dict) -> dict | None:
    """
    The touch a lead is credited to: the most recent *paid* one, falling back
    to the first touch we ever saw.

    Last-paid-click rather than first-click because the question the dashboard
    answers is "which ad should I spend more on" — credit belongs to the ad
    that brought them back the time they converted. First touch is kept on the
    row regardless, so the other view stays available later.
    """
    return visitor.get("last_paid_touch") or visitor.get("first_touch") or None


async def record_touch(site: dict, visitor_id: str, touch: dict, mongo_client) -> None:
    """Upsert a visitor and fold in this landing."""
    now = datetime.utcnow()
    update = {
        "$setOnInsert": {
            "site_id": site["site_id"],
            "client_group_id": site["client_group_id"],
            "user_id": site["user_id"],
            "location_id": site.get("location_id"),
            "first_touch": {**touch, "ts": now},
            "match_status": "anonymous",
            "match_attempts": 0,
            "created_at": now,
        },
        "$set": {"last_seen_at": now},
        "$inc": {"touch_count": 1},
    }
    if is_paid_touch(touch):
        update["$set"]["last_paid_touch"] = {**touch, "ts": now}

    await mongo_client[DB_NAME][VISITORS].update_one({"_id": visitor_id}, update, upsert=True)


async def record_identity(
    site: dict,
    visitor_id: str,
    email: str | None,
    phone: str | None,
    mongo_client,
) -> dict:
    """
    Attach an email/phone to a visitor and try to match them straight away.

    Returns {"matched": bool, "status": str}. Nothing is echoed back to the
    browser — this is for logs and tests.
    """
    keys = compute_match_keys(_clip(email, MAX_ID_LEN), _clip(phone, MAX_ID_LEN))
    if not keys:
        return {"matched": False, "status": "no_keys"}

    now = datetime.utcnow()
    db = mongo_client[DB_NAME]
    existing = await db[VISITORS].find_one({"_id": visitor_id}, projection={"match_status": 1})
    already_matched = bool(existing and existing.get("match_status") == "matched")

    changes = {
        "identity": {
            "email": normalize_email(email),
            "phone": normalize_phone(phone),
        },
        "identified_at": now,
        "last_seen_at": now,
    }
    if not already_matched:
        # A fresh identity is a fresh chance, even for a visitor we gave up on
        # — but a contact that is already attributed stays attributed.
        changes["match_status"] = "pending"

    await db[VISITORS].update_one(
        {"_id": visitor_id},
        {
            "$setOnInsert": {
                "site_id": site["site_id"],
                "client_group_id": site["client_group_id"],
                "user_id": site["user_id"],
                "location_id": site.get("location_id"),
                "match_attempts": 0,
                "touch_count": 0,
                "created_at": now,
            },
            "$set": changes,
            "$addToSet": {"match_keys": {"$each": keys}},
        },
        upsert=True,
    )

    if already_matched:
        return {"matched": True, "status": "already_matched"}

    visitor = await db[VISITORS].find_one({"_id": visitor_id})
    matched = await try_match_visitor(visitor, mongo_client) if visitor else None
    return {"matched": bool(matched), "status": "matched" if matched else "pending"}


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _parse_ghl_date(raw) -> datetime | None:
    """GHL's dateAdded is an ISO string ('2026-09-01T12:00:00.000Z')."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def match_confidence(visitor_keys: list[str], contact_keys: list[str]) -> tuple[str, int]:
    """
    Score an identity match by which keys actually overlapped.

    Email and phone are both deterministic — neither is a guess — so the gap
    between them is small and reflects only how often a person shares one:
    shared/typo'd phone numbers are more common than shared inboxes.
    """
    shared = set(visitor_keys) & set(contact_keys)
    has_email = any(k.startswith("email:") for k in shared)
    has_phone = any(k.startswith("phone:") for k in shared)
    if has_email and has_phone:
        return "email+phone", 99
    if has_email:
        return "email", 95
    if has_phone:
        return "phone", 90
    return "none", 0


def carries_visitor_id(contact_data: dict, visitor_id: str) -> bool:
    """
    True when the GHL contact itself holds our visitor id.

    The tracker stamps a hidden `birdy_visitor_id` input into on-page forms; if
    the client mapped it to a GHL custom field, it lands here and the match
    stops being an inference. GHL has spelled this array `customFields` and
    `customField` across API versions, and each entry may put the value under
    `value`, `field_value` or `fieldValue` — hence the wide net.
    """
    if not visitor_id:
        return False
    fields = contact_data.get("customFields") or contact_data.get("customField") or []
    if not isinstance(fields, list):
        return False
    for field in fields:
        if not isinstance(field, dict):
            continue
        for key in ("value", "field_value", "fieldValue"):
            if field.get(key) == visitor_id:
                return True
    return False


async def try_match_visitor(visitor: dict, mongo_client) -> dict | None:
    """
    Find the GHL contact for an identified visitor and write the match row.

    Returns the match document on success, None if no contact carries these
    keys yet (the usual case for the first few minutes after a submission —
    the CRM sync runs hourly).
    """
    keys = visitor.get("match_keys") or []
    group_id = visitor.get("client_group_id")
    if not keys or not group_id:
        return None

    db = mongo_client[DB_NAME]
    contact = await db["ghl_contacts"].find_one(
        {"client_group_id": group_id, "match_keys": {"$in": keys}},
        projection={
            "contact_id": 1, "location_id": 1, "user_id": 1,
            "match_keys": 1, "contact_data.dateAdded": 1,
            "contact_data.customFields": 1, "contact_data.customField": 1,
        },
        sort=[("contact_data.dateAdded", -1)],
    )
    if not contact:
        return None

    touch = attributed_touch(visitor) or {}
    method, confidence = match_confidence(keys, contact.get("match_keys") or [])
    # If the hidden field survived the form, the CRM record is carrying our own
    # visitor id and there is nothing left to infer — the identity match only
    # confirmed what the id already said.
    if carries_visitor_id(contact.get("contact_data") or {}, visitor["_id"]):
        method, confidence = "visitor_id", 100

    contact_created = _parse_ghl_date(
        (contact.get("contact_data") or {}).get("dateAdded")
    )
    touch_at = touch.get("ts") or visitor.get("created_at")
    predates = bool(
        contact_created and touch_at and contact_created < touch_at - CLICK_SLACK
    )

    now = datetime.utcnow()
    doc = {
        "client_group_id": group_id,
        "user_id": visitor.get("user_id"),
        "location_id": contact.get("location_id") or visitor.get("location_id"),
        "ghl_contact_id": contact.get("contact_id"),
        "visitor_id": visitor["_id"],
        "method": method,
        "confidence": confidence,
        "ad_id": touch.get("ad_id"),
        "adset_id": touch.get("adset_id"),
        "campaign_id": touch.get("campaign_id"),
        "fbclid": touch.get("fbclid"),
        "utm_source": touch.get("utm_source"),
        "utm_medium": touch.get("utm_medium"),
        "utm_campaign": touch.get("utm_campaign"),
        "utm_content": touch.get("utm_content"),
        "landing_page": touch.get("landing_page"),
        "touch_at": touch_at,
        "first_touch_at": (visitor.get("first_touch") or {}).get("ts"),
        "contact_created_at": contact_created,
        "contact_predates_click": predates,
        "created_at": now,
    }

    # First match wins. A contact is claimed once; a later visitor presenting
    # the same email is the same person coming back, not a second lead.
    await db[MATCHES].update_one(
        {"client_group_id": group_id, "ghl_contact_id": contact.get("contact_id")},
        {"$setOnInsert": doc},
        upsert=True,
    )
    await db[VISITORS].update_one(
        {"_id": visitor["_id"]},
        {
            "$set": {
                "match_status": "matched",
                "matched_at": now,
                "ghl_contact_id": contact.get("contact_id"),
            }
        },
    )
    logger.info(
        "Attributed contact %s to ad %s via %s (visitor %s)",
        contact.get("contact_id"), touch.get("ad_id"), method, visitor["_id"],
    )
    return doc


async def run_match_tick(mongo_client, limit: int = 200) -> dict:
    """
    Retry every identified-but-unmatched visitor.

    Driven from the visitor side rather than by scanning ghl_contacts: the
    pending queue is tiny and indexed, so the tick costs the same whether the
    account has a thousand contacts or a million.
    """
    db = mongo_client[DB_NAME]
    cursor = db[VISITORS].find(
        {"match_status": "pending"},
        sort=[("identified_at", 1)],
        limit=limit,
    )
    pending = await cursor.to_list(length=limit)

    matched = 0
    gave_up = 0
    for visitor in pending:
        try:
            if await try_match_visitor(visitor, mongo_client):
                matched += 1
                continue
        except Exception as e:  # one bad row must not stall the queue
            logger.error("Match failed for visitor %s: %s", visitor.get("_id"), e)

        attempts = (visitor.get("match_attempts") or 0) + 1
        update = {"$set": {"match_attempts": attempts, "last_match_attempt_at": datetime.utcnow()}}
        if attempts >= MAX_MATCH_ATTEMPTS:
            # Not a failure worth alerting on: plenty of people fill in a form
            # and never reach the CRM (bounced submit, spam filter, a form the
            # client forgot to wire up).
            update["$set"]["match_status"] = "unmatched"
            gave_up += 1
        await db[VISITORS].update_one({"_id": visitor["_id"]}, update)

    return {"scanned": len(pending), "matched": matched, "gave_up": gave_up}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

async def leads_by_ad(
    user_id: str,
    client_group_id: str,
    start: datetime | None,
    end: datetime | None,
    mongo_client,
    include_predated: bool = False,
) -> list[dict]:
    """
    Attributed lead counts per Meta ad, newest ad names resolved from Meta.

    Counts contacts by when the *contact* was created, not when we matched
    them, so the figure lines up with every other lead count in Birdy.
    """
    db = mongo_client[DB_NAME]
    match: dict = {"user_id": user_id, "client_group_id": client_group_id, "ad_id": {"$ne": None}}
    if not include_predated:
        match["contact_predates_click"] = {"$ne": True}
    if start or end:
        window = {}
        if start:
            window["$gte"] = start
        if end:
            window["$lte"] = end
        match["contact_created_at"] = window

    rows = await db[MATCHES].aggregate([
        {"$match": match},
        {"$group": {
            "_id": "$ad_id",
            "leads": {"$sum": 1},
            "adset_id": {"$first": "$adset_id"},
            "campaign_id": {"$first": "$campaign_id"},
            "last_lead_at": {"$max": "$contact_created_at"},
            "deterministic": {"$sum": {"$cond": [{"$gte": ["$confidence", 100]}, 1, 0]}},
        }},
        {"$sort": {"leads": -1}},
    ]).to_list(length=None)

    ad_ids = [r["_id"] for r in rows if r.get("_id")]
    names: dict[str, str] = {}
    if ad_ids:
        insights = await db["facebook_ad_insights"].find(
            {"user_id": user_id, "ad_id": {"$in": ad_ids}},
            projection={"ad_id": 1, "ad_name": 1, "_id": 0},
        ).to_list(length=None)
        for row in insights:
            if row.get("ad_name"):
                names[row["ad_id"]] = row["ad_name"]

    return [
        {
            "ad_id": r["_id"],
            "ad_name": names.get(r["_id"]),
            "adset_id": r.get("adset_id"),
            "campaign_id": r.get("campaign_id"),
            "leads": r["leads"],
            "deterministic_leads": r.get("deterministic", 0),
            "last_lead_at": r.get("last_lead_at"),
        }
        for r in rows
    ]


async def install_status(client_group_id: str, mongo_client) -> dict:
    """
    Whether the snippet is live and doing its job — what the onboarding step's
    "✓ Tracking detected" tick reads.
    """
    db = mongo_client[DB_NAME]
    latest = await db[VISITORS].find_one(
        {"client_group_id": client_group_id},
        projection={"last_seen_at": 1, "last_paid_touch": 1},
        sort=[("last_seen_at", -1)],
    )
    paid = await db[VISITORS].find_one(
        {"client_group_id": client_group_id, "last_paid_touch": {"$exists": True}},
        projection={"last_paid_touch": 1},
        sort=[("last_seen_at", -1)],
    )
    matches = await db[MATCHES].count_documents({"client_group_id": client_group_id})
    return {
        "installed": bool(latest),
        "last_seen_at": latest.get("last_seen_at") if latest else None,
        "first_ad_click_seen": bool(paid),
        "last_ad_id": (paid or {}).get("last_paid_touch", {}).get("ad_id"),
        "attributed_leads": matches,
    }


# ---------------------------------------------------------------------------
# Indexes (called from main.py lifespan startup)
# ---------------------------------------------------------------------------

async def create_attribution_indexes(mongo_client):
    """Idempotent index creation for the attribution collections."""
    db = mongo_client[DB_NAME]

    visitors = db[VISITORS]
    # The match tick's only query. Tiny and fully covered by this index — the
    # pending queue never grows past what a tick drains.
    await visitors.create_index(
        [("match_status", 1), ("identified_at", 1)], name="match_queue"
    )
    await visitors.create_index(
        [("client_group_id", 1), ("last_seen_at", -1)], name="group_last_seen"
    )
    await visitors.create_index("match_keys", name="idx_match_keys", sparse=True)
    # Anonymous browsing history ages out; attribution_matches is the record
    # that has to survive, and it has no TTL.
    await visitors.create_index(
        "last_seen_at", expireAfterSeconds=VISITOR_TTL_DAYS * 86400, name="visitor_ttl"
    )

    matches = db[MATCHES]
    await matches.create_index(
        [("client_group_id", 1), ("ghl_contact_id", 1)],
        unique=True,
        name="group_contact_unique",
    )
    await matches.create_index(
        [("user_id", 1), ("client_group_id", 1), ("contact_created_at", -1)],
        name="group_contact_created",
    )
    await matches.create_index([("visitor_id", 1)], name="visitor_id")

    # The site_id lookup on every pageview, and the contact lookup every match
    # performs. Both are on existing collections.
    await db["client_groups"].create_index(
        "attribution_site_id", unique=True, sparse=True, name="attribution_site_unique"
    )
    # Multikey, and ghl_contacts is the largest collection in the system —
    # built in the background so a cold start never blocks on it.
    await db["ghl_contacts"].create_index(
        [("client_group_id", 1), ("match_keys", 1)],
        name="group_match_keys",
        background=True,
    )

    logger.info("✅ Created attribution indexes")
