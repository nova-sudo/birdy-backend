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

# The most clients an admin override may grant. A typo of 1000 for 100 is
# the failure this guards against, not anyone's real need.
MAX_CLIENT_LIMIT_OVERRIDE = 1000


def plan_client_limit(sub: dict | None) -> int:
    """What the subscription alone allows: the plan's base, plus paid extra
    slots on plans that sell them. 0 with no active/trialing subscription."""
    if not sub or sub.get("status") not in ("active", "trialing"):
        return 0
    plan_id = sub.get("plan_id", "starter")
    extra = sub.get("extra_clients_paid", 0) if plan_id in EXTRA_CLIENTS_ALLOWED_PLANS else 0
    return PLAN_LIMITS.get(plan_id, 0) + extra


def override_limit(user: dict | None) -> int | None:
    """
    An admin's override of the client limit, or None.

    Kept on users.client_limit_override rather than inside `subscription`: the
    Whop webhook rewrites the whole subscription document on every event, and
    would silently erase anything an admin had put there.
    """
    o = (user or {}).get("client_limit_override") or {}
    limit = o.get("limit")
    return int(limit) if isinstance(limit, (int, float)) and limit >= 0 else None


async def _get_subscription_and_count(current_user: str, mongo_client):
    """Return (subscription_doc, current_client_count)."""
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]

    user = await db["users"].find_one(
        {"user_id": current_user},
        projection={"subscription": 1, "client_limit_override": 1, "_id": 0}
    )
    sub   = user.get("subscription") if user else None
    count = await db["client_groups"].count_documents({"user_id": current_user})
    return sub, count, user


async def require_active_subscription(current_user: str, mongo_client) -> dict:
    """
    Raise 402 if the user has no active/trialing subscription.
    Returns the subscription dict if valid.
    """
    sub, _, _user = await _get_subscription_and_count(current_user, mongo_client)

    if not sub or sub.get("status") not in ("active", "trialing"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "NO_ACTIVE_SUBSCRIPTION",
                "message": "An active subscription is required to use this feature.",
            }
        )
    return sub


def needs_plan(user: dict | None) -> bool:
    """Whether this account must pick a plan before it can use Birdy: no active
    subscription, and no client limit granted by an admin. `user` is the users
    document (or just its `subscription` and `client_limit_override` fields)."""
    if override_limit(user) is not None:
        return False
    sub = (user or {}).get("subscription")
    return not sub or sub.get("status") not in ("active", "trialing")


async def check_client_limit(current_user: str, mongo_client):
    """
    Raise 402 if the user is at or over their client group limit.

    Limit logic:
      - Admin override (users.client_limit_override): that number, whatever
        the subscription says — including no subscription at all.
      - Starter / Growth: base limit only — extra_clients_paid is ignored
      - Scale: base limit (25) + extra_clients_paid purchased slots
    """
    sub, count, user = await _get_subscription_and_count(current_user, mongo_client)

    override = override_limit(user)
    if override is not None:
        if count >= override:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "CLIENT_LIMIT_REACHED",
                    "message": (
                        f"You've reached your account's limit of {override} clients. "
                        f"Contact Birdy support to change it."
                    ),
                    "current_count": count,
                    "limit": override,
                    "plan": (sub or {}).get("plan_id"),
                    "can_add_extra_slots": False,
                    "limit_override": True,
                },
            )
        return True

    # There is no free tier: every client needs an active plan, the first one
    # included. Setup has a required plan step before the first client, and a
    # sandbox account gets its plan there without paying (billing.sandbox_plan).
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