"""
billing_middleware.py
----------------------
Dependency helpers for enforcing subscription limits.

Usage in main.py:
  from billing.billing_middleware import require_active_subscription, check_client_limit

  @app.post("/api/client-groups")
  async def create_client_group(
      request: ClientGroupRequest,
      current_user: str = Depends(get_current_user),
      _: None = Depends(check_client_limit),   # ← add this
  ):
      ...
"""

import os
import logging
from fastapi import HTTPException, Request, Depends

logger = logging.getLogger(__name__)

PLAN_LIMITS = {
    "starter": 3,
    "growth":  10,
    "scale":   25,
}


async def _get_subscription_and_count(current_user: str, mongo_client):
    """Return (subscription_doc, current_client_count)."""
    db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]

    user = await db["users"].find_one(
        {"user_id": current_user},
        {"user_id": current_user},
        {"subscription": 1}
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
    Call this as a Depends() before creating a new client group.
    """
    sub, count = await _get_subscription_and_count(current_user, mongo_client)

    if not sub or sub.get("status") not in ("active", "trialing"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "NO_ACTIVE_SUBSCRIPTION",
                "message": "You need an active subscription to add clients.",
            }
        )

    plan_id      = sub.get("plan_id", "starter")
    base_limit   = PLAN_LIMITS.get(plan_id, 0)
    extra_paid   = sub.get("extra_clients_paid", 0)
    total_limit  = base_limit + extra_paid

    if count >= total_limit:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "CLIENT_LIMIT_REACHED",
                "message": (
                    f"You've reached your client limit ({total_limit}). "
                    f"Upgrade your plan or add extra client slots to continue."
                ),
                "current_count": count,
                "limit": total_limit,
                "plan": plan_id,
            }
        )

    return True