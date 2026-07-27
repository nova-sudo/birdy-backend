"""
integrations/facebook_utils/facebook_ads_write.py
-------------------------------------------------
The Meta *write* path. Everything else under facebook_utils/ only READS
(insights, leads, campaign/adset/ad fetches). Pausing and re-enabling an ad is a
mutation, so it lives here on its own, deliberately small and side-effect-only.

Requires the `ads_management` scope, which the OAuth config already requests
(see facebook.py META_OAUTH_CONFIG). Used by services/dashboard_service.py to
apply and undo "Do it for me" suggestions.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

META_API = "https://graph.facebook.com/v25.0"

# The only two configured statuses we ever set from a suggestion apply/undo.
VALID_STATUSES = {"ACTIVE", "PAUSED"}


async def get_ad_status(ad_id: str, access_token: str) -> str | None:
    """Return an ad's *configured* status ("ACTIVE" / "PAUSED" / ...), or None on
    error. This is what we snapshot before pausing so undo can restore exactly."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(
                f"{META_API}/{ad_id}",
                params={"fields": "status", "access_token": access_token},
            )
            if resp.status_code != 200:
                logger.warning(
                    "get_ad_status(%s) failed: %s %s", ad_id, resp.status_code, resp.text[:200]
                )
                return None
            return resp.json().get("status")
        except Exception as e:  # noqa: BLE001 — never let a Meta hiccup crash the caller
            logger.error("get_ad_status(%s) error: %s", ad_id, e)
            return None


async def set_ad_status(ad_id: str, status: str, access_token: str) -> tuple[bool, str | None]:
    """Set an ad's status. Returns (ok, error_message). Only ACTIVE / PAUSED are
    accepted so a bad caller can never push an ad into an unexpected state."""
    status = (status or "").upper()
    if status not in VALID_STATUSES:
        return False, f"Unsupported ad status {status!r}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{META_API}/{ad_id}",
                data={"status": status, "access_token": access_token},
            )
            if resp.status_code != 200:
                body = resp.text[:300]
                logger.warning(
                    "set_ad_status(%s -> %s) failed: %s %s", ad_id, status, resp.status_code, body
                )
                return False, f"Meta API error ({resp.status_code}): {body[:200]}"
            return True, None
        except Exception as e:  # noqa: BLE001
            logger.error("set_ad_status(%s -> %s) error: %s", ad_id, status, e)
            return False, str(e)[:200]
