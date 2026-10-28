"""Live GHL lookups for one lead (opportunities, appointments) with status mapping.

Imports nothing from lead_detail/lead_journey so both can use it.
"""
import logging

from integrations.gohighlevel import ghl_integration, get_subaccount_tokens

logger = logging.getLogger(__name__)

_MISSING = object()


def status_for(code: int) -> str:
    if code == 200:
        return "ok"
    return "reconnect" if code in (401, 403) else "unavailable"


async def token_for(db, user_id: str, location_id):
    """The location's GHL access token, or None. May raise on a DB error."""
    if not location_id:
        return None
    tokens = await get_subaccount_tokens(user_id, db.client)
    return (tokens or {}).get(location_id, {}).get("access_token")


async def safe_token_for(db, user_id: str, location_id):
    try:
        return await token_for(db, user_id, location_id)
    except Exception as e:
        logger.warning("token lookup failed for location %s: %s", location_id, type(e).__name__)
        return None


async def live_opportunities(db, user_id: str, location_id, contact_id, token=_MISSING):
    """(status, raw GHL opportunities). status: ok | reconnect | unavailable. Never raises.

    Pass `token` (possibly None) to skip the token lookup.
    """
    try:
        if token is _MISSING:
            token = await token_for(db, user_id, location_id)
        if not token or not contact_id:
            return "unavailable", []
        code, rows = await ghl_integration.search_contact_opportunities(location_id, token, contact_id)
        if code != 200:
            return status_for(code), []
        return "ok", [r for r in rows if isinstance(r, dict)]
    except Exception as e:
        logger.warning("live_opportunities failed for location %s: %s", location_id, type(e).__name__)
        return "unavailable", []


async def live_appointments(db, user_id: str, location_id, contact_id, token=_MISSING):
    """(status, raw GHL appointments). Same contract as live_opportunities."""
    try:
        if token is _MISSING:
            token = await token_for(db, user_id, location_id)
        if not token or not contact_id:
            return "unavailable", []
        code, rows = await ghl_integration.fetch_contact_appointments(token, contact_id)
        if code != 200:
            return status_for(code), []
        return "ok", [r for r in rows if isinstance(r, dict)]
    except Exception as e:
        logger.warning("live_appointments failed for location %s: %s", location_id, type(e).__name__)
        return "unavailable", []
