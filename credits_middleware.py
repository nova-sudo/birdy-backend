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

from fastapi import HTTPException

from core.database import get_db
from credits import _load_and_sync, _available, _status_payload, get_credits_settings

logger = logging.getLogger(__name__)


async def check_credits(user_id: str, mongo_client) -> None:
    """Raise HTTP 402 ``OUT_OF_CREDITS`` when the user has no Birdy Credits left
    (only when enforcement is on — the admin-toggleable ``enforce`` setting).

    The detail carries the current balance snapshot so the frontend can show the
    out-of-credits state + a top-up prompt without a second round-trip.
    """
    try:
        db = get_db(mongo_client)
        # Read fresh so flipping the admin enforcement toggle takes effect on the
        # very next request (no cross-instance cache lag on the stopper).
        settings = await get_credits_settings(db, fresh=True)
    except Exception as e:
        logger.error(f"Credit settings read failed for {user_id}: {e}", exc_info=True)
        return  # fail open — never block on a settings error

    if not settings["enforce"]:
        logger.debug(f"credits gate: user={user_id} enforce=off → allow")
        return  # measurement/rollout mode — meter, but never block

    try:
        credits, sub = await _load_and_sync(db, user_id)
    except Exception as e:
        logger.error(f"Credit check failed for {user_id}: {e}", exc_info=True)
        return  # fail open — never block on a metering error

    avail = _available(credits)
    if avail <= 0:
        logger.info(f"credits gate: BLOCK user={user_id} enforce=on available={avail}")
        raise HTTPException(
            status_code=402,
            detail={
                "code": "OUT_OF_CREDITS",
                "message": "You're out of Birdy Credits. Top up to keep using Birdy AI.",
                **_status_payload(credits, sub, rate_mode=settings["rate_mode"], enforce=True),
            },
        )
    logger.info(f"credits gate: allow user={user_id} enforce=on available={avail}")
