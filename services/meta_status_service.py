"""
services/meta_status_service.py
-------------------------------
Pause / resume a Meta campaign, ad set, or ad, and keep the cached status in
Mongo consistent.

Extracted from routers/meta.py's inline POST /api/facebook/update-status so the
same, single implementation backs both that route and the suggestion "apply"
endpoint (routers/dashboard.py). Meta uses the same `status` field on all three
node types, so one call handles campaign/adset/ad.

Raises MetaStatusError (with an HTTP-ish status_code) on failure so request-
scoped callers can translate it to an HTTPException while non-request callers
(future auto-run) can handle it directly.
"""

import logging

import httpx

from core.constants import META_CACHE_PRESETS
from core.database import DB_NAME
from integrations.facebook_utils.facebook import get_facebook_token

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v25.0"

_VALID_TYPES = ("campaign", "adset", "ad")
_VALID_STATUSES = ("ACTIVE", "PAUSED")
_COLLECTION_KEY = {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}


class MetaStatusError(Exception):
    """Raised when a status update can't be performed. Carries an HTTP status."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def set_object_status(
    user_id: str,
    object_id: str,
    object_type: str,
    status: str,
    mongo_client,
) -> dict:
    """
    Toggle a Meta object between ACTIVE and PAUSED using the owning user's token.

    Returns {success, object_id, object_type, new_status} on success.
    """
    if object_type not in _VALID_TYPES:
        raise MetaStatusError("object_type must be 'campaign', 'adset', or 'ad'", 400)
    if status not in _VALID_STATUSES:
        raise MetaStatusError("status must be 'ACTIVE' or 'PAUSED'", 400)

    token_data = await get_facebook_token(user_id, mongo_client)
    if not token_data or not token_data.get("access_token"):
        raise MetaStatusError("No valid Facebook token found", 401)
    access_token = token_data["access_token"]

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_GRAPH}/{object_id}",
                data={"status": status, "access_token": access_token},
            )
    except httpx.HTTPError as e:
        logger.error("Meta status update transport error for %s: %s", object_id, e)
        raise MetaStatusError(f"Failed to reach Meta: {e}", 502)

    if resp.status_code != 200:
        try:
            error_msg = resp.json().get("error", {}).get("message", "Unknown error")
        except Exception:
            error_msg = f"HTTP {resp.status_code}"
        logger.error("Meta status update failed for %s: %s", object_id, error_msg)
        code = resp.status_code if 400 <= resp.status_code < 600 else 502
        raise MetaStatusError(f"Meta API error: {error_msg}", code)

    logger.info("Updated %s %s to %s for user %s", object_type, object_id, status, user_id)
    await _rewrite_cached_status(mongo_client, user_id, object_type, object_id, status)
    return {
        "success": True,
        "object_id": object_id,
        "object_type": object_type,
        "new_status": status,
    }


async def _rewrite_cached_status(mongo_client, user_id: str, object_type: str,
                                 object_id: str, status: str) -> None:
    """
    Mirror the new status into every cached preset bucket so the UI stays
    consistent across reloads without waiting for the next Meta refresh. Best-
    effort: a path that doesn't exist for some group/preset is simply skipped.
    """
    db = mongo_client[DB_NAME]
    new_status = status.capitalize()  # "Active"/"Paused" — matches Meta's cached format
    collection_key = _COLLECTION_KEY[object_type]

    paths = [f"facebook_cache.{p}.{collection_key}" for p in META_CACHE_PRESETS]
    paths.append(f"facebook_cache.{collection_key}")  # backward-compat top-level

    for path in paths:
        try:
            await db.client_groups.update_many(
                {"user_id": user_id, f"{path}.id": object_id},
                {"$set": {f"{path}.$.status": new_status}},
            )
        except Exception:
            pass  # path may not exist for all groups/presets
