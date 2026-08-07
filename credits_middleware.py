"""
credits_middleware.py
---------------------
Enforce the Birdy Credits balance on AI entry points — the "stopper" when a
customer runs out. Mirrors billing_middleware.require_active_subscription: a
small helper that reads the user doc and raises 402 when the balance is gone.

Fail-open: if the balance can't be read (a metering/DB glitch), we let the
request through rather than block the product on a credits bug.
"""

import logging
import os

from fastapi import HTTPException

from core.database import get_db
from credits import _load_and_sync, _available, _status_payload

logger = logging.getLogger(__name__)

# Staged rollout (per the billing strategy: "measure first, then enforce with
# notice"). While this is off, usage is still metered and the balance is shown,
# but the stopper never blocks — so shipping metering can't lock existing users
# out of AI. Flip CREDITS_ENFORCE=true to turn the hard stop on.
CREDITS_ENFORCE = os.getenv("CREDITS_ENFORCE", "false").strip().lower() in ("1", "true", "yes", "on")


async def check_credits(user_id: str, mongo_client) -> None:
    """Raise HTTP 402 ``OUT_OF_CREDITS`` when the user has no Birdy Credits left
    (only when CREDITS_ENFORCE is on).

    The detail carries the current balance snapshot so the frontend can show the
    out-of-credits state + a top-up prompt without a second round-trip.
    """
    if not CREDITS_ENFORCE:
        return  # measurement/rollout mode — meter, but never block

    try:
        db = get_db(mongo_client)
        credits, sub = await _load_and_sync(db, user_id)
    except Exception as e:
        logger.error(f"Credit check failed for {user_id}: {e}", exc_info=True)
        return  # fail open — never block on a metering error

    if _available(credits) <= 0:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "OUT_OF_CREDITS",
                "message": "You're out of Birdy Credits. Top up to keep using Birdy AI.",
                **_status_payload(credits, sub),
            },
        )
