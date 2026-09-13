"""
services/tracked_leads.py
-------------------------
Leads captured off a landing page we don't own.

The attribution tracker could already tell that an anonymous visitor clicked ad
839 and later typed an email into a form. What it could not do was call that
person a *lead*: they only became reportable once a matching contact turned up in
GoHighLevel on the next sync. For a client whose form feeds GHL that is merely a
delay. For a client whose form feeds a spreadsheet, a Zap, or their own backend,
the lead existed nowhere at all.

This is that missing record. One row per person who submitted a form, durable,
with whatever ad identifiers we can attach.

Two ways in, because the form is usually not ours to read:

    tracker   our script is on the page and read the submitted fields directly.
    webhook   the form lives in an iframe on someone else's domain, so the form
              tool posts the submission to us server-side instead.

**Not stored on the visitor document, deliberately.** A webhook lead may have no
visitor at all — no script on the page, just a POST — and `attribution_visitors`
is operational state with a TTL. A lead is a business record and has to outlive
the browsing session that produced it.

Everything here is idempotent on `(client_group_id, dedupe_key)`. Forms get
double-submitted, webhooks get retried, and a person who fills in the same form
twice in a week is one lead, not two.
"""

from __future__ import annotations

import logging
from datetime import datetime

from core.database import DB_NAME
from services.attribution_service import attributed_touch, clean_touch
from utils.phone_normalize import compute_match_keys, normalize_email, normalize_phone

logger = logging.getLogger(__name__)


TRACKED_LEADS = "tracked_leads"

SOURCE_TRACKER = "tracker"
SOURCE_WEBHOOK = "webhook"

# Anything longer than this from the wire is truncated. These endpoints are
# public, so nothing arriving over them is trusted for length.
MAX_FIELD_LEN = 200


def _clip(value, limit: int = MAX_FIELD_LEN) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit] if value else None


def dedupe_key(email: str | None, phone: str | None) -> str | None:
    """
    The one key that identifies this person for idempotency.

    Email first, phone second — an email is far likelier to be unique to one
    human than a phone number, which gets shared across a household or typed
    wrong. Returns None when neither is usable, which is the caller's signal
    that this submission is not a lead.
    """
    normalized_email = normalize_email(email)
    if normalized_email:
        return f"email:{normalized_email}"
    normalized_phone = normalize_phone(phone)
    if normalized_phone:
        return f"phone:{normalized_phone}"
    return None


def _touch_from_payload(payload: dict) -> dict:
    """Ad identifiers the form tool sent us itself, when there is no visitor."""
    return clean_touch({
        key: payload.get(key)
        for key in (
            "ad_id", "adset_id", "campaign_id", "fbclid",
            "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
            "landing_page", "referrer",
        )
    })


async def record_tracked_lead(
    site: dict,
    payload: dict,
    source: str,
    mongo_client,
    provider: str | None = None,
    visitor: dict | None = None,
) -> dict:
    """
    Upsert one captured lead.

    Returns `{"stored": bool, "reason": str, "dedupe_key": str | None}` — the
    browser endpoints ignore it, the webhook reports it back to whoever is
    wiring the form up, and the tests assert on it.

    `visitor` is the already-loaded visitor document when one exists; the ad
    attribution comes from its touch history via `attributed_touch`, which
    credits the most recent *paid* touch and falls back to the first. A webhook
    with no visitor falls back to whatever identifiers the payload carried.
    """
    email = _clip(payload.get("email"))
    phone = _clip(payload.get("phone"))
    key = dedupe_key(email, phone)
    if not key:
        # Not a lead. A form submission with no way to contact the person is a
        # pageview with extra steps, and storing it would inflate every count
        # that follows.
        return {"stored": False, "reason": "no_email_or_phone", "dedupe_key": None}

    touch = attributed_touch(visitor) if visitor else None
    if not touch:
        touch = _touch_from_payload(payload)

    now = datetime.utcnow()
    doc = {
        "user_id": site["user_id"],
        "client_group_id": site["client_group_id"],
        "client_group_name": site.get("client_group_name"),
        "location_id": site.get("location_id"),
        "source": source,
        "provider": provider,
        "visitor_id": (visitor or {}).get("_id"),
        "dedupe_key": key,
        # Raw as typed. `match_keys` below is the normalised form for joining;
        # normalize_phone keeps only the last ten digits, which is right for
        # matching and useless for calling someone back.
        "name": _clip(payload.get("name")),
        "email": email,
        "phone": phone,
        "match_keys": compute_match_keys(email, phone),
        "ad_id": touch.get("ad_id"),
        "adset_id": touch.get("adset_id"),
        "campaign_id": touch.get("campaign_id"),
        "fbclid": touch.get("fbclid"),
        "utm_source": touch.get("utm_source"),
        "utm_medium": touch.get("utm_medium"),
        "utm_campaign": touch.get("utm_campaign"),
        "utm_content": touch.get("utm_content"),
        "landing_page": touch.get("landing_page"),
        "submitted_at": now,
    }

    db = mongo_client[DB_NAME]
    result = await db[TRACKED_LEADS].update_one(
        {"client_group_id": site["client_group_id"], "dedupe_key": key},
        {
            "$set": doc,
            "$setOnInsert": {
                "created_at": now,
                "pushed_to_ghl": False,
                "ghl_contact_id": None,
            },
        },
        upsert=True,
    )

    inserted = result.upserted_id is not None
    logger.info(
        "Tracked lead %s: group=%s source=%s ad_id=%s",
        "created" if inserted else "updated",
        site["client_group_id"], source, touch.get("ad_id"),
    )
    return {
        "stored": True,
        "reason": "created" if inserted else "updated",
        "dedupe_key": key,
    }


# ---------------------------------------------------------------------------
# Indexes (registered in core/indexes.py)
# ---------------------------------------------------------------------------

async def create_tracked_leads_indexes(mongo_client):
    """Idempotent index creation for the tracked_leads collection."""
    coll = mongo_client[DB_NAME][TRACKED_LEADS]

    # Idempotency, and the upsert's own filter.
    await coll.create_index(
        [("client_group_id", 1), ("dedupe_key", 1)],
        unique=True,
        name="group_dedupe_unique",
    )
    # The resolver's query: this client's leads in a date window, newest first.
    await coll.create_index(
        [("client_group_id", 1), ("submitted_at", -1)], name="group_submitted_at"
    )
    await coll.create_index("match_keys", name="idx_match_keys")
    # The GHL-push worker looks for leads it hasn't pushed yet.
    await coll.create_index(
        [("client_group_id", 1), ("pushed_to_ghl", 1)], name="group_pushed"
    )

    logger.info("✅ Created tracked_leads indexes")
