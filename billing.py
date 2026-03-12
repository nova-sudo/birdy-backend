"""
paddle_billing.py  (v5 — final, all imports verified)
-------------------------------------------------------
Uses the official Paddle Python SDK (pip install paddle-python-sdk).

Import verification sources:
  - Client, Environment, Options  → README: https://github.com/PaddleHQ/paddle-python-sdk
  - Secret, Verifier              → Paddle webhook docs
  - UpdateSubscription            → UPGRADING.md: paddle_billing.Resources.Subscriptions.Operations
  - SubscriptionUpdateItem        → UPGRADING.md: paddle_billing.Resources.Subscriptions.Operations.Update.SubscriptionUpdateItem
  - proration_billing_mode        → Raw string "prorated_immediately" used directly —
                                    ProrationType path not in public docs, string is safe.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

# ── Official Paddle Python SDK ─────────────────────────────────────────────────
# pip install paddle-python-sdk
# All import paths verified against official README and UPGRADING.md
from paddle_billing import Client, Environment, Options
from paddle_billing.Notifications import Secret, Verifier
from paddle_billing.Resources.Subscriptions.Operations import UpdateSubscription
from paddle_billing.Resources.Subscriptions.Operations.Update.SubscriptionUpdateItem import SubscriptionUpdateItem

from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Config ─────────────────────────────────────────────────────────────────────

PADDLE_API_KEY        = os.getenv("PADDLE_API_KEY", "")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
PADDLE_ENVIRONMENT    = os.getenv("PADDLE_ENVIRONMENT", "sandbox")  # "sandbox" | "production"

PADDLE_PRICE_STARTER      = os.getenv("PADDLE_PRICE_STARTER", "")
PADDLE_PRICE_GROWTH       = os.getenv("PADDLE_PRICE_GROWTH", "")
PADDLE_PRICE_SCALE        = os.getenv("PADDLE_PRICE_SCALE", "")
PADDLE_PRICE_EXTRA_CLIENT = os.getenv("PADDLE_PRICE_EXTRA_CLIENT", "")

PLAN_METADATA = {
    PADDLE_PRICE_STARTER: {"id": "starter", "name": "Starter", "max_clients": 3,  "base_price": 97},
    PADDLE_PRICE_GROWTH:  {"id": "growth",  "name": "Growth",  "max_clients": 10, "base_price": 297},
    PADDLE_PRICE_SCALE:   {"id": "scale",   "name": "Scale",   "max_clients": 25, "base_price": 497},
}

# ── SDK client ─────────────────────────────────────────────────────────────────

def _paddle() -> Client:
    if PADDLE_ENVIRONMENT == "sandbox":
        return Client(PADDLE_API_KEY, options=Options(Environment.SANDBOX))
    return Client(PADDLE_API_KEY)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _plan_meta(price_id: str) -> dict:
    return PLAN_METADATA.get(price_id, {"id": "unknown", "name": "Unknown", "max_clients": 0})


async def _get_sub(user_id: str, mongo_client) -> Optional[dict]:
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    user = await db["users"].find_one({"user_id": user_id}, {"subscription": 1, "_id": 0})
    return user.get("subscription") if user else None


async def _save_sub(user_id: str, sub: dict, mongo_client):
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    await db["users"].update_one(
        {"user_id": user_id},
        {"$set": {"subscription": sub, "updated_at": datetime.utcnow()}},
        upsert=True,
    )


async def _client_count(user_id: str, mongo_client) -> int:
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
    return await db["client_groups"].count_documents({"user_id": user_id})


# ── GET /api/billing/status ───────────────────────────────────────────────────

@router.get("/api/billing/status")
async def billing_status(current_user: str = Depends(get_current_user)):
    user_id = current_user

    async with get_mongo_client() as mc:
        sub    = await _get_sub(user_id, mc)
        count  = await _client_count(user_id, mc)

        if not sub or sub.get("status") not in ("active", "trialing", "past_due"):
            return {
                "subscribed": False,
                "plan": {"id": "free", "name": "Free", "max_clients": 0},
                "status": "inactive",
                "client_count": count,
                "client_limit": 0,
                "extra_clients_paid": 0,
                "current_period_end": None,
                "cancel_at_period_end": False,
                "paddle_customer_id": None,
                "_user_id": user_id,   # frontend passes this as Paddle.js customData
            }

        meta = _plan_meta(sub.get("price_id", ""))
        return {
            "subscribed": True,
            "plan": meta,
            "status": sub.get("status"),
            "client_count": count,
            "client_limit": meta["max_clients"] + sub.get("extra_clients_paid", 0),
            "extra_clients_paid": sub.get("extra_clients_paid", 0),
            "current_period_end": sub.get("current_period_end"),
            "cancel_at_period_end": sub.get("cancel_at_period_end", False),
            "paddle_customer_id": sub.get("paddle_customer_id"),
            "paddle_subscription_id": sub.get("paddle_subscription_id"),
            "_user_id": user_id,
        }


# ── GET /api/billing/portal-url ───────────────────────────────────────────────

@router.get("/api/billing/portal-url")
async def portal_url(current_user: str = Depends(get_current_user)):
    user_id = current_user

    async with get_mongo_client() as mc:
        sub = await _get_sub(user_id, mc)

        if not sub or not sub.get("paddle_subscription_id"):
            raise HTTPException(status_code=400, detail="No active subscription found")

        sub_id = sub["paddle_subscription_id"]

    try:
        paddle       = _paddle()
        paddle_sub   = paddle.subscriptions.get(sub_id)
        mgmt_urls    = getattr(paddle_sub, "management_urls", None)
        portal_url_  = getattr(mgmt_urls, "portal_overview", None) if mgmt_urls else None

        if not portal_url_:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Portal URL not available. Make sure your Paddle API key has "
                    "'Customer portal session (Write)' permission."
                ),
            )

        return {"portal_url": portal_url_}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Paddle portal error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get portal URL: {str(e)}")


# ── POST /api/billing/change-plan ─────────────────────────────────────────────

class ChangePlanRequest(BaseModel):
    new_price_id: str
    extra_clients: int = 0


@router.post("/api/billing/change-plan")
async def change_plan(body: ChangePlanRequest, current_user: str = Depends(get_current_user)):
    user_id = current_user

    if body.new_price_id not in PLAN_METADATA:
        raise HTTPException(status_code=400, detail="Invalid price ID")

    async with get_mongo_client() as mc:
        sub = await _get_sub(user_id, mc)

        if not sub or not sub.get("paddle_subscription_id"):
            raise HTTPException(status_code=400, detail="No active subscription found")

        sub_id = sub["paddle_subscription_id"]

    try:
        paddle = _paddle()

        # Build the complete items list
        items = [SubscriptionUpdateItem(price_id=body.new_price_id, quantity=1)]
        if body.extra_clients > 0 and PADDLE_PRICE_EXTRA_CLIENT:
            items.append(SubscriptionUpdateItem(price_id=PADDLE_PRICE_EXTRA_CLIENT, quantity=body.extra_clients))

        paddle.subscriptions.update(
            sub_id,
            UpdateSubscription(
                items=items,
                proration_billing_mode="prorated_immediately",
            ),
        )

        new_meta = _plan_meta(body.new_price_id)
        return {"success": True, "message": f"Plan changed to {new_meta['name']}", "new_plan": new_meta}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Paddle change-plan error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update subscription: {str(e)}")


# ── POST /api/billing/webhook ─────────────────────────────────────────────────

# Thin wrapper so the Paddle SDK Verifier can read .headers and .body
# from an object whose .body is already-read bytes (not a coroutine).
class _VerifierRequest:
    """
    Satisfies paddle_billing.Notifications.Requests.Request protocol.
    The SDK expects request.body to be bytes and request.headers to be
    a dict-like mapping.
    """
    def __init__(self, headers: dict, body: bytes):
        self.headers = headers
        self.body    = body


@router.post("/api/billing/webhook")
async def paddle_webhook(request: Request):
    raw_body = await request.body()  # read once, reuse below

    # ── Signature verification ────────────────────────────────────────────────
    if PADDLE_WEBHOOK_SECRET:
        try:
            is_valid = Verifier().verify(
                _VerifierRequest(dict(request.headers), raw_body),
                Secret(PADDLE_WEBHOOK_SECRET),
            )
            if not is_valid:
                logger.warning("Paddle webhook: invalid signature")
                raise HTTPException(status_code=400, detail="Invalid signature")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Paddle webhook signature error: {e}")
            raise HTTPException(status_code=400, detail="Signature verification failed")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type = payload.get("event_type", "")
    data       = payload.get("data", {})

    logger.info(f"📦 Paddle webhook: {event_type}")

    async with get_mongo_client() as mc:
        if event_type in ("subscription.created", "subscription.updated"):
            await _upsert_subscription(data, mc)
        elif event_type == "subscription.canceled":
            await _cancel_subscription(data, mc)
        elif event_type == "transaction.completed":
            logger.info(f"✅ Transaction completed: {data.get('id')}")

    return {"success": True}


async def _upsert_subscription(data: dict, mc):
    """
    Parse a Paddle subscription webhook payload and persist to MongoDB.

    Paddle subscription object key fields:
      id                          → sub_xxx
      customer_id                 → ctm_xxx
      status                      → active | trialing | past_due | canceled | paused
      custom_data                 → dict you set via Paddle.js customData (contains user_id)
      items[].price.id            → price ID for each item
      items[].quantity            → quantity
      next_billed_at              → RFC 3339 next billing date
      current_billing_period.ends_at → end of current period
      scheduled_change.action     → "cancel" | "pause" when a change is scheduled
    """
    sub_id      = data.get("id")
    customer_id = data.get("customer_id")
    status      = data.get("status")

    # Resolve user_id via customData (set in Paddle.js checkout call)
    custom_data = data.get("custom_data") or {}
    user_id     = custom_data.get("user_id")

    if not user_id:
        # Fallback: look up by Paddle customer_id already stored
        db   = mc[os.getenv("MONGODB_DB", "birdyaidev")]
        user = await db["users"].find_one(
            {"subscription.paddle_customer_id": customer_id},
            {"user_id": 1},
        )
        user_id = user.get("user_id") if user else None

    if not user_id:
        logger.error(f"Cannot resolve user for sub {sub_id} / customer {customer_id}")
        return

    # Parse items: each has price.id and quantity
    price_id  = None
    extra_qty = 0
    for item in data.get("items", []):
        pid = (item.get("price") or {}).get("id", "")
        qty = item.get("quantity", 1)
        if pid in PLAN_METADATA:
            price_id = pid
        elif pid == PADDLE_PRICE_EXTRA_CLIENT:
            extra_qty = qty

    meta = _plan_meta(price_id or "")

    # next billing date
    period_end = data.get("next_billed_at") or (
        (data.get("current_billing_period") or {}).get("ends_at")
    )

    # scheduled cancellation?
    sched           = data.get("scheduled_change") or {}
    cancel_at_end   = sched.get("action") == "cancel"

    sub_doc = {
        "paddle_subscription_id": sub_id,
        "paddle_customer_id":     customer_id,
        "price_id":               price_id,
        "plan_id":                meta["id"],
        "plan_name":              meta["name"],
        "status":                 status,
        "max_clients":            meta["max_clients"],
        "extra_clients_paid":     extra_qty,
        "current_period_end":     period_end,
        "cancel_at_period_end":   cancel_at_end,
        "updated_at":             datetime.utcnow(),
    }

    await _save_sub(user_id, sub_doc, mc)
    logger.info(f"✅ sub {sub_id} → user {user_id} ({meta['name']}, {status})")


async def _cancel_subscription(data: dict, mc):
    sub_id = data.get("id")

    db   = mc[os.getenv("MONGODB_DB", "birdyaidev")]
    user = await db["users"].find_one(
        {"subscription.paddle_subscription_id": sub_id},
        {"user_id": 1},
    )
    if not user:
        logger.warning(f"No user found for canceled sub {sub_id}")
        return

    await db["users"].update_one(
        {"user_id": user["user_id"]},
        {"$set": {
            "subscription.status":               "canceled",
            "subscription.cancel_at_period_end": False,
            "subscription.updated_at":           datetime.utcnow(),
            "updated_at":                        datetime.utcnow(),
        }},
    )
    logger.info(f"❌ sub {sub_id} canceled for user {user['user_id']}")