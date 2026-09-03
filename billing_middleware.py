"""
billing_middleware.py
----------------------
Dependency helpers for enforcing subscription limits.

Plan rules:
  - Starter: max 3 clients,  NO extra slots
  - Growth:  max 10 clients, NO extra slots
  - Scale:   max 25 clients, CAN purchase extra slots at $10/mo each
"""

import os
import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)

PLAN_LIMITS = {
    "starter": 3,
    "growth":  10,
    "scale":   25,
}

# Only Scale plan users can purchase extra client slots
EXTRA_CLIENTS_ALLOWED_PLANS = {"scale"}


async def _get_subscription_and_count(current_user: str, mongo_client):
    """Return (subscription_doc, current_client_count)."""
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]

    user = await db["users"].find_one(
        {"user_id": current_user},
        projection={"subscription": 1, "_id": 0}
    )
    sub   = user.get("subscription") if user else None
    count = await db["client_groups"].count_documents({"user_id": current_user})
    return sub, count


async def require_active_subscription(current_user: str, mongo_client) -> dict:
    """
    Raise 402 if the user has no active/trialing subscription.
    Returns the subscription dict if valid.
    """
    sub, _ = await _get_subscription_and_count(current_user, mongo_client)

    if not sub or sub.get("status") not in ("active", "trialing"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "NO_ACTIVE_SUBSCRIPTION",
                "message": "An active subscription is required to use this feature.",
            }
        )
    return sub


async def check_client_limit(current_user: str, mongo_client):
    """
    Raise 402 if the user is at or over their client group limit.

    Limit logic:
      - Starter / Growth: base limit only — extra_clients_paid is ignored
      - Scale: base limit (25) + extra_clients_paid purchased slots
    """
    sub, count = await _get_subscription_and_count(current_user, mongo_client)

    # A brand-new account's very first client group is free — lets someone
    # see Birdy actually work before paying. Every client after this one
    # requires an active subscription, including onboarding's own bulk
    # import (which is gated by its own mandatory billing step before
    # import-subaccounts — the other caller of this function — ever runs,
    # so in practice a real subscription already exists by then).
    if count == 0:
        return True

    if not sub or sub.get("status") not in ("active", "trialing"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "NO_ACTIVE_SUBSCRIPTION",
                "message": "You need an active subscription to add clients.",
            }
        )

    plan_id    = sub.get("plan_id", "starter")
    base_limit = PLAN_LIMITS.get(plan_id, 0)

    # Extra slots only apply on Scale plan
    extra_paid  = sub.get("extra_clients_paid", 0) if plan_id in EXTRA_CLIENTS_ALLOWED_PLANS else 0
    total_limit = base_limit + extra_paid

    if count >= total_limit:
        if plan_id in EXTRA_CLIENTS_ALLOWED_PLANS:
            msg = (
                f"You've reached your client limit ({total_limit}). "
                f"Add extra client slots ($10/mo each) from the Billing page to continue."
            )
        else:
            msg = (
                f"You've reached your {plan_id.title()} plan limit of {total_limit} clients. "
                f"Upgrade to a higher plan to add more clients."
            )

        raise HTTPException(
            status_code=402,
            detail={
                "code": "CLIENT_LIMIT_REACHED",
                "message": msg,
                "current_count": count,
                "limit": total_limit,
                "plan": plan_id,
                "can_add_extra_slots": plan_id in EXTRA_CLIENTS_ALLOWED_PLANS,
            }
        )

    return True


def can_purchase_extra_slots(plan_id: str) -> bool:
    """Returns True only if the plan supports extra client slots."""
    return plan_id in EXTRA_CLIENTS_ALLOWED_PLANS