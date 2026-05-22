"""
services/call_logs_service.py
-----------------------------
Inbound call-log webhook ingestion.

Providers push call events to /webhooks/call_logs; we normalize and persist
them in the `call_logs` collection. A `source` identifier lives on every
row (e.g. "ghl", "hotprospector") so multiple providers can coexist and we
can later filter or join per provider.

Idempotency: a unique index on (source, source_event_id) prevents duplicate
inserts when a provider retries. Docs without a source_event_id are simply
inserted (we can't safely dedup without an ID).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.database import DB_NAME

logger = logging.getLogger(__name__)


# Providers we accept on the webhook endpoint. Add new sources here as you
# onboard them. Lowercased; compared case-insensitively by resolve_source.
ALLOWED_SOURCES = {"ghl", "hotprospector"}


# ---------------------------------------------------------------------------
# Source dispatch
# ---------------------------------------------------------------------------

def resolve_source(raw: str | None) -> str | None:
    """Normalize the X-Call-Source header value; return None if not allowed."""
    if not raw:
        return None
    norm = raw.strip().lower()
    return norm if norm in ALLOWED_SOURCES else None


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _first(payload: dict, *keys: str) -> Any:
    """Return the first non-empty value among payload[keys[0]], payload[keys[1]], ..."""
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_dt(v: Any) -> datetime | None:
    """Best-effort datetime parse. Accepts ISO strings, ms epoch, s epoch, datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):
        try:
            ts = float(v)
            if ts > 1e12:  # looks like ms
                ts = ts / 1000.0
            return datetime.utcfromtimestamp(ts)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _parse_int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-source normalizers
# ---------------------------------------------------------------------------

def _normalize_ghl(payload: dict) -> dict:
    """
    Map a GHL workflow webhook body to our normalized shape.

    GHL workflow bodies are user-configured in the workflow builder, so we
    look for several common key spellings (snake / camel / GHL-native) and
    fall through gracefully when a field isn't present.
    """
    return {
        "source_event_id": str(_first(payload, "id", "callId", "eventId", "messageId") or "") or None,
        "location_id":     _first(payload, "locationId", "location_id"),
        "contact_id":      _first(payload, "contactId", "contact_id"),
        "contact_phone":   _first(payload, "phone", "phoneNumber", "contactPhone", "from"),
        "contact_email":   _first(payload, "email", "contactEmail"),
        "direction":       _first(payload, "direction", "callDirection"),
        "status":          _first(payload, "status", "callStatus"),
        "duration_seconds": _parse_int(_first(payload, "duration", "durationSeconds", "callDuration")),
        "started_at":      _parse_dt(_first(payload, "startedAt", "callStartTime", "dateAdded", "createdAt")),
        "ended_at":        _parse_dt(_first(payload, "endedAt", "callEndTime")),
        "recording_url":   _first(payload, "recordingUrl", "recording_url"),
    }


_NORMALIZERS = {
    "ghl": _normalize_ghl,
    # Add new providers here, e.g.:
    # "hotprospector": _normalize_hotprospector,
}


def normalize_call_log(source: str, payload: dict) -> dict:
    """Dispatch to the per-source normalizer. Unknown sources get an empty dict."""
    fn = _NORMALIZERS.get(source)
    if fn is None:
        return {}
    return fn(payload)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

async def resolve_user_id_from_location(location_id: str | None, mongo_client) -> str | None:
    """Find the user_id that owns this GHL location via the client_groups mapping."""
    if not location_id:
        return None
    db = mongo_client[DB_NAME]
    group = await db["client_groups"].find_one(
        {"ghl_location_id": location_id},
        projection={"user_id": 1},
    )
    return group.get("user_id") if group else None


async def save_call_log(
    source: str,
    payload: dict,
    headers: dict,
    mongo_client,
) -> dict:
    """
    Normalize, resolve the owning user, and insert (or detect duplicate).

    Returns:
        On insert:    {"id": "<inserted_id>", "duplicate": False, ...}
        On duplicate: {"id": "<existing_id>", "duplicate": True}
    """
    db = mongo_client[DB_NAME]
    coll = db["call_logs"]

    normalized = normalize_call_log(source, payload)
    user_id = await resolve_user_id_from_location(normalized.get("location_id"), mongo_client)

    now = datetime.utcnow()
    doc = {
        "source": source,
        "user_id": user_id,
        **normalized,
        "raw_payload": payload,
        "headers": headers,
        "received_at": now,
        "created_at": now,
    }

    event_id = normalized.get("source_event_id")

    # Idempotent path: dedup on (source, source_event_id) when present.
    if event_id:
        existing = await coll.find_one(
            {"source": source, "source_event_id": event_id},
            projection={"_id": 1},
        )
        if existing:
            logger.info("Duplicate call log ignored: source=%s event_id=%s", source, event_id)
            return {"id": str(existing["_id"]), "duplicate": True}

    result = await coll.insert_one(doc)
    logger.info(
        "Stored call log: source=%s event_id=%s user_id=%s location_id=%s",
        source, event_id, user_id, normalized.get("location_id"),
    )
    return {
        "id": str(result.inserted_id),
        "duplicate": False,
        "user_id": user_id,
        "location_id": normalized.get("location_id"),
    }


# ---------------------------------------------------------------------------
# Indexes (called from main.py lifespan startup)
# ---------------------------------------------------------------------------

async def create_call_logs_indexes(mongo_client):
    """Idempotent index creation for the call_logs collection."""
    db = mongo_client[DB_NAME]
    coll = db["call_logs"]

    # Idempotency: unique only when source_event_id is a string so that
    # docs missing the ID don't all collide on a single null entry.
    await coll.create_index(
        [("source", 1), ("source_event_id", 1)],
        unique=True,
        partialFilterExpression={"source_event_id": {"$type": "string"}},
        name="source_event_unique",
    )
    await coll.create_index([("user_id", 1), ("started_at", -1)], name="user_started_at")
    await coll.create_index([("location_id", 1), ("started_at", -1)], name="location_started_at")
    await coll.create_index([("received_at", -1)], name="received_at_desc")
