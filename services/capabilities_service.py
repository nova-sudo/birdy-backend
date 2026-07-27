"""
services/capabilities_service.py
--------------------------------
Per-user Birdy AI "capabilities" — optional agent abilities the user can turn
on or off in Settings -> Capabilities. Stored on the user document under the
top-level ``capabilities`` field, e.g. ``capabilities.media_buying``.

Capabilities are behavioural toggles for the chat agent (routers/chat.py ->
ai/orchestrator.py). The first one, ``media_buying``, gates whether the
senior-media-buyer analysis module is injected into the chat system prompt on
the campaigns / dashboard / client-detail / opportunities / leads / general
chat surfaces. Off by default so nothing changes until a user opts in.

Design notes:
- One Mongo read per new chat session (the system prompt is built once per
  session), so this is cheap; no caching needed.
- Unknown keys in a stored document are ignored on read and rejected on write,
  so a rollback can never surface a half-written flag as truthy.
- To add a capability later: add it to DEFAULT_CAPABILITIES, expose it in the
  Settings UI, and gate the relevant behaviour on it. Nothing else changes.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# The full set of known capabilities and their default (opt-in => False).
DEFAULT_CAPABILITIES: dict[str, bool] = {
    "media_buying": False,
}


def _coerce(stored: dict | None) -> dict[str, bool]:
    """Merge a stored capabilities doc onto the defaults, keeping only known
    keys and coercing every value to a plain bool."""
    stored = stored or {}
    return {key: bool(stored.get(key, default)) for key, default in DEFAULT_CAPABILITIES.items()}


async def get_capabilities(db, user_id: str) -> dict[str, bool]:
    """Return the user's capability flags, with defaults applied for anything
    not yet set. Never raises — a lookup failure degrades to all-defaults so a
    DB hiccup can't take down chat."""
    try:
        user_doc = await db["users"].find_one(
            {"user_id": user_id},
            {"capabilities": 1},
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"capabilities lookup failed for {user_id}, using defaults: {e}")
        return dict(DEFAULT_CAPABILITIES)
    return _coerce((user_doc or {}).get("capabilities"))


async def set_capabilities(db, user_id: str, updates: dict[str, bool]) -> dict[str, bool]:
    """Persist a partial capabilities update (only known keys are written) and
    return the full, resolved capability set. Upserts so a user who has never
    saved settings still gets a document."""
    known = {
        f"capabilities.{key}": bool(value)
        for key, value in (updates or {}).items()
        if key in DEFAULT_CAPABILITIES
    }
    if known:
        known["updated_at"] = datetime.utcnow()
        await db["users"].update_one(
            {"user_id": user_id},
            {"$set": known},
            upsert=True,
        )
        logger.info(f"Updated capabilities for {user_id}: { {k: v for k, v in updates.items() if k in DEFAULT_CAPABILITIES} }")
    return await get_capabilities(db, user_id)
