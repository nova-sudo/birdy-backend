"""
billing.py
----------
Whop billing integration.

Uses the official Whop Python SDK (``whop-sdk``) to read memberships and the
Standard Webhooks signature scheme (``whop-sdk[webhooks]`` → ``standardwebhooks``)
to verify incoming webhooks.

Design notes
============
Whop does **not** have Paddle's "update the subscription's items in place" API.
Plan changes and cancellations happen through Whop's hosted customer portal
(``membership.manage_url``). So this module only needs to:

  * expose the user's subscription status (``GET /api/billing/status``),
  * hand back the Whop portal URL (``GET /api/billing/portal-url``), and
  * react to Whop membership / payment webhooks (``POST /api/billing/webhook``).

New subscriptions and the optional "extra client slot" add-on are started from
the frontend via Whop's embedded checkout; the webhook is what actually flips a
subscription live here.

User linking
============
In this codebase ``user_id`` *is* the account email (the JWT ``sub``). The
frontend prefills that email into Whop checkout, so a membership webhook is
matched back to the account by ``metadata.user_id`` (when a checkout session set
it) and otherwise by the buyer's Whop email.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from whop_sdk import AsyncWhop
from standardwebhooks import Webhook as StandardWebhook

from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Config ─────────────────────────────────────────────────────────────────────

WHOP_API_KEY        = os.getenv("WHOP_API_KEY", "")
WHOP_WEBHOOK_SECRET = os.getenv("WHOP_WEBHOOK_SECRET", "")

WHOP_PLAN_STARTER      = os.getenv("WHOP_PLAN_STARTER", "")
WHOP_PLAN_GROWTH       = os.getenv("WHOP_PLAN_GROWTH", "")
WHOP_PLAN_SCALE        = os.getenv("WHOP_PLAN_SCALE", "")
# Optional: a separate recurring "extra client slot" plan (Scale add-on).
WHOP_PLAN_EXTRA_CLIENT = os.getenv("WHOP_PLAN_EXTRA_CLIENT", "")

PLAN_METADATA = {
    WHOP_PLAN_STARTER: {"id": "starter", "name": "Starter", "max_clients": 3,  "base_price": 97},
    WHOP_PLAN_GROWTH:  {"id": "growth",  "name": "Growth",  "max_clients": 10, "base_price": 297},
    WHOP_PLAN_SCALE:   {"id": "scale",   "name": "Scale",   "max_clients": 25, "base_price": 497},
}
# Drop an empty-string key if any plan env var is unset, so a blank plan id
# from an unmapped membership never accidentally matches a configured plan.
PLAN_METADATA.pop("", None)

# Only the Scale plan supports extra client slots
EXTRA_CLIENTS_ALLOWED_PLANS = {"scale"}

# Whop membership statuses that grant access. "canceling" = set to cancel at
# period end but still valid until then.
ACTIVE_STATUSES = {"active", "trialing", "past_due", "canceling"}

# ── SDK client ─────────────────────────────────────────────────────────────────

def _whop() -> AsyncWhop:
    if not WHOP_API_KEY:
        raise HTTPException(status_code=500, detail="Billing is not configured (missing WHOP_API_KEY).")
    return AsyncWhop(api_key=WHOP_API_KEY, webhook_key=WHOP_WEBHOOK_SECRET or None)


def _secret_fingerprint() -> str:
    """A short, non-reversible fingerprint of the configured webhook secret, so
    a config mismatch (wrong value / not redeployed) can be diagnosed from logs
    WITHOUT ever logging the secret itself. Compare it to the expected value:
        printf %s 'the-sandbox-signing-secret' | \
          python3 -c "import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:12])"
    """
    if not WHOP_WEBHOOK_SECRET:
        return "UNSET"
    return hashlib.sha256(WHOP_WEBHOOK_SECRET.encode()).hexdigest()[:12]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _plan_meta(plan_id: str) -> dict:
    """Map a Whop plan id → internal plan metadata."""
    return PLAN_METADATA.get(plan_id or "", {"id": "unknown", "name": "Unknown", "max_clients": 0})


def _db(mongo_client):
    return mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]


async def _get_sub(user_id: str, mongo_client) -> Optional[dict]:
    user = await _db(mongo_client)["users"].find_one(
        {"user_id": user_id},
        projection={"subscription": 1, "_id": 0},
    )
    return user.get("subscription") if user else None


async def _save_sub(user_id: str, sub: dict, mongo_client):
    await _db(mongo_client)["users"].update_one(
        {"user_id": user_id},
        {"$set": {"subscription": sub, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def _client_count(user_id: str, mongo_client) -> int:
    return await _db(mongo_client)["client_groups"].count_documents({"user_id": user_id})


def _iso(value) -> Optional[str]:
    """Normalise a Whop timestamp (datetime, unix seconds, or ISO string) to ISO-8601."""
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# ── GET /api/billing/status ───────────────────────────────────────────────────

@router.get("/api/billing/status")
async def billing_status(current_user: str = Depends(get_current_user)):
    user_id = current_user

    async with get_mongo_client() as mc:
        sub   = await _get_sub(user_id, mc)
        count = await _client_count(user_id, mc)

        if not sub or sub.get("status") not in ACTIVE_STATUSES:
            return {
                "subscribed": False,
                "plan": {"id": "free", "name": "Free", "max_clients": 0},
                "status": "inactive",
                "client_count": count,
                "client_limit": 0,
                "extra_clients_paid": 0,
                "can_add_extra_slots": False,
                "current_period_end": None,
                "cancel_at_period_end": False,
                "whop_membership_id": None,
                "_user_id": user_id,
            }

        plan = {
            "id":          sub.get("plan_id", "unknown"),
            "name":        sub.get("plan_name", "Unknown"),
            "max_clients": sub.get("max_clients", 0),
        }
        extra_paid = sub.get("extra_clients_paid", 0) if plan["id"] in EXTRA_CLIENTS_ALLOWED_PLANS else 0

        return {
            "subscribed": True,
            "plan": plan,
            "status": sub.get("status"),
            "client_count": count,
            "client_limit": plan["max_clients"] + extra_paid,
            "extra_clients_paid": extra_paid,
            "can_add_extra_slots": plan["id"] in EXTRA_CLIENTS_ALLOWED_PLANS,
            "current_period_end": sub.get("current_period_end"),
            "cancel_at_period_end": sub.get("cancel_at_period_end", False),
            "whop_membership_id": sub.get("whop_membership_id"),
            "_user_id": user_id,
        }


# ── GET /api/billing/portal-url ───────────────────────────────────────────────

@router.get("/api/billing/portal-url")
async def portal_url(current_user: str = Depends(get_current_user)):
    """Return the Whop-hosted URL where the customer can manage their membership
    (change plan, update payment method, cancel)."""
    user_id = current_user

    async with get_mongo_client() as mc:
        sub = await _get_sub(user_id, mc)
        if not sub or not sub.get("whop_membership_id"):
            raise HTTPException(status_code=400, detail="No active subscription found")
        membership_id = sub["whop_membership_id"]
        stored_url    = sub.get("manage_url")

    # Prefer a freshly fetched manage_url; fall back to the one captured at webhook time.
    url = stored_url
    try:
        async with _whop() as whop:
            membership = await whop.memberships.retrieve(membership_id)
        url = getattr(membership, "manage_url", None) or stored_url
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Whop portal fetch failed: {e}", exc_info=True)

    if not url:
        raise HTTPException(status_code=500, detail="Manage URL not available yet. Please try again shortly.")
    return {"portal_url": url}


# ── POST /api/billing/webhook ─────────────────────────────────────────────────

@router.post("/api/billing/webhook")
async def whop_webhook(request: Request):
    raw = await request.body()
    payload_str = raw.decode("utf-8")

    if WHOP_WEBHOOK_SECRET:
        try:
            # Standard Webhooks: verifies webhook-id / webhook-timestamp /
            # webhook-signature headers against the shared secret.
            StandardWebhook(WHOP_WEBHOOK_SECRET).verify(payload_str, dict(request.headers))
        except Exception as e:
            present = {k.lower() for k in request.headers}
            logger.warning(
                "Whop webhook signature verification FAILED: %s "
                "| backend secret_fp=%s secret_len=%d body_len=%d "
                "| headers present: webhook-id=%s webhook-timestamp=%s webhook-signature=%s",
                e, _secret_fingerprint(), len(WHOP_WEBHOOK_SECRET), len(payload_str),
                "webhook-id" in present, "webhook-timestamp" in present, "webhook-signature" in present,
            )
            raise HTTPException(status_code=400, detail="Invalid signature")
    else:
        logger.warning("Whop webhook received but WHOP_WEBHOOK_SECRET is not set — skipping verification")

    try:
        event = json.loads(payload_str)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Whop's event type lives in `type` (newer) or `action` (older API versions).
    event_type = event.get("type") or event.get("action") or ""
    data       = event.get("data") or {}

    logger.info(f"📦 Whop webhook: {event_type}")

    async with get_mongo_client() as mc:
        if event_type.startswith("membership."):
            await _handle_membership(data, mc)
        elif event_type.startswith("payment."):
            logger.info(f"💳 Whop payment {event_type}: {data.get('id')}")

    # Upserts are idempotent (rebuilt from full membership state), so Whop's
    # at-least-once redelivery is safe to ack unconditionally.
    return {"success": True}


async def _resolve_user_id(data: dict, mongo_client) -> Optional[str]:
    """Resolve the birdy account (user_id == email) that a Whop membership belongs to."""
    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    if user_id:
        return user_id

    email = (data.get("user") or {}).get("email")
    if email:
        user = await _db(mongo_client)["users"].find_one(
            {"user_id": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
            projection={"user_id": 1, "_id": 0},
        )
        if user:
            return user["user_id"]
        # user_id IS the email in this system — use it directly if the account
        # row cannot be found (e.g. it will be upserted).
        return email

    return None


async def _handle_membership(data: dict, mongo_client):
    membership_id = data.get("id")
    whop_plan_id  = (data.get("plan") or {}).get("id", "")
    status        = data.get("status", "")

    user_id = await _resolve_user_id(data, mongo_client)
    if not user_id:
        logger.error(f"Whop webhook: cannot resolve user for membership {membership_id} (plan {whop_plan_id})")
        return

    # Extra-client-slot add-on membership → adjust the slot count, leave the base plan alone.
    if WHOP_PLAN_EXTRA_CLIENT and whop_plan_id == WHOP_PLAN_EXTRA_CLIENT:
        await _handle_extra_slots(user_id, membership_id, data, status, mongo_client)
        return

    if whop_plan_id not in PLAN_METADATA:
        logger.warning(f"Whop webhook: membership {membership_id} has unmapped plan {whop_plan_id}; ignoring")
        return

    meta = _plan_meta(whop_plan_id)

    if status not in ACTIVE_STATUSES:
        await _deactivate(user_id, membership_id, status, mongo_client)
        return

    existing = await _get_sub(user_id, mongo_client) or {}
    extra_paid = existing.get("extra_clients_paid", 0) if meta["id"] in EXTRA_CLIENTS_ALLOWED_PLANS else 0

    sub_doc = {
        "whop_membership_id":   membership_id,
        "whop_user_id":         (data.get("user") or {}).get("id"),
        "whop_plan_id":         whop_plan_id,
        "plan_id":              meta["id"],
        "plan_name":            meta["name"],
        "status":               status,
        "max_clients":          meta["max_clients"],
        "extra_clients_paid":   extra_paid,
        "current_period_end":   _iso(data.get("renewal_period_end")),
        "cancel_at_period_end": bool(data.get("cancel_at_period_end", False)),
        "manage_url":           data.get("manage_url"),
        "updated_at":           datetime.now(timezone.utc),
    }
    # Preserve the extra-slot add-on membership link across base-plan rewrites.
    if meta["id"] in EXTRA_CLIENTS_ALLOWED_PLANS and existing.get("whop_extra_membership_id"):
        sub_doc["whop_extra_membership_id"] = existing["whop_extra_membership_id"]

    await _save_sub(user_id, sub_doc, mongo_client)
    logger.info(f"✅ Whop membership {membership_id} → {user_id} ({meta['name']}, {status}, extra={extra_paid})")


async def _handle_extra_slots(user_id: str, membership_id: str, data: dict, status: str, mongo_client):
    """A membership on the dedicated 'extra client slot' plan tracks how many
    additional Scale slots the user has purchased."""
    users = _db(mongo_client)["users"]

    if status in ACTIVE_STATUSES:
        try:
            qty = int(data.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        await users.update_one(
            {"user_id": user_id},
            {"$set": {
                "subscription.extra_clients_paid":      max(qty, 0),
                "subscription.whop_extra_membership_id": membership_id,
                "subscription.updated_at":              datetime.now(timezone.utc),
            }},
        )
        logger.info(f"✅ Whop extra-slot membership {membership_id} → {user_id} (qty={qty})")
    else:
        await users.update_one(
            {"user_id": user_id, "subscription.whop_extra_membership_id": membership_id},
            {"$set": {
                "subscription.extra_clients_paid": 0,
                "subscription.updated_at":         datetime.now(timezone.utc),
            }},
        )
        logger.info(f"❌ Whop extra-slot membership {membership_id} ended for {user_id}")


async def _deactivate(user_id: str, membership_id: str, status: str, mongo_client):
    """Mark the stored subscription inactive — but only if it is the membership
    that actually ended (a stale event for an old membership must not wipe a
    newer, active one)."""
    result = await _db(mongo_client)["users"].update_one(
        {"user_id": user_id, "subscription.whop_membership_id": membership_id},
        {"$set": {
            "subscription.status":               status or "canceled",
            "subscription.cancel_at_period_end": False,
            "subscription.updated_at":           datetime.now(timezone.utc),
        }},
    )
    if result.matched_count:
        logger.info(f"❌ Whop membership {membership_id} → {user_id} is now {status or 'canceled'}")
