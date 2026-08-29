"""
routers/call_logs.py
--------------------
Read endpoints for the unified call_logs collection.

Writes happen in routers/webhooks.py (inbound provider webhooks).
This module is the read side: paginated listing per client group,
respecting the group's `call_log_provider` choice.

Routing rule:
    - provider == "ghl"            → query call_logs collection
                                     (source = "ghl", location_id =
                                      group.ghl_location_id)
    - provider == "hotprospector"  → empty result set + a `message`
                                     until HP endpoints are ready

The Sales-Hub frontend consumes this endpoint when the selected
client's `call_log_provider` is "ghl". HP-provider clients short-
circuit with the empty payload.
"""

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["call_logs"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(dt: Any) -> str | None:
    """Convert a Mongo datetime to ISO 8601 (or None)."""
    if isinstance(dt, datetime):
        return dt.isoformat()
    return None


def _serialize(row: dict) -> dict:
    """Project a stored call_log document down to the frontend shape."""
    return {
        "id":               str(row.get("_id")),
        "source":           row.get("source"),
        "source_event_id":  row.get("source_event_id"),
        "user_id":          row.get("user_id"),
        "location_id":      row.get("location_id"),
        "contact_id":       row.get("contact_id"),
        "contact_phone":    row.get("contact_phone"),
        "contact_email":    row.get("contact_email"),
        "direction":        row.get("direction"),
        "status":           row.get("status"),
        "duration_seconds": row.get("duration_seconds"),
        "started_at":       _iso(row.get("started_at")),
        "ended_at":         _iso(row.get("ended_at")),
        "recording_url":    row.get("recording_url"),
        "received_at":      _iso(row.get("received_at")),
    }


# ---------------------------------------------------------------------------
# GET /api/call_logs?group_id=...&skip=...&limit=...
# ---------------------------------------------------------------------------

@router.get("/call_logs")
async def list_call_logs(
    group_id: str = Query(..., description="Client group id (client_groups.id)"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    source: str | None = Query(
        None,
        description="Optional source override; defaults to the group's call_log_provider.",
    ),
    current_user: str = Depends(get_current_user),
):
    """
    Paginated call-log list for a single client group.

    The group's `call_log_provider` determines which rows we look at:
      - "ghl"           → call_logs where source="ghl" and
                          location_id = group.ghl_location_id
      - "hotprospector" → empty list (HP integration not yet wired)

    Caller can override with ?source=ghl explicitly (used when debugging
    a group whose provider field hasn't been set yet on legacy rows).

    Response:
        {
          "data":  [<call_log_doc>, ...],
          "meta":  { total, returned, skip, limit, provider, group_id, location_id },
          "message"?: "..."  // present when no data is returned for a reason
        }
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        group = await db["client_groups"].find_one(
            {"id": group_id, "user_id": current_user},
            projection={
                "id": 1,
                "name": 1,
                "ghl_location_id": 1,
                "call_log_provider": 1,
            },
        )
        if not group:
            raise HTTPException(status_code=404, detail="Client group not found")

        provider = (source or group.get("call_log_provider") or "ghl").lower()
        location_id = group.get("ghl_location_id")

        # Only known providers are queried; unknown values fail loudly below
        # so config bugs (e.g. a stray value in Mongo) don't silently 200.
        if provider not in ("ghl", "hotprospector"):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown call_log_provider for group {group_id}: {provider!r}",
            )

        if not location_id:
            return {
                "data": [],
                "meta": {
                    "total": 0,
                    "returned": 0,
                    "skip": skip,
                    "limit": limit,
                    "provider": provider,
                    "group_id": group_id,
                    "location_id": None,
                },
                "message": "This client group has no linked GHL location.",
            }

        # Both providers share the same call_logs schema — only the `source`
        # tag differs. GHL rows come from the /webhooks/call_logs handler;
        # HotProspector rows are stamped by the HP cron (see
        # services/hp_service.py::_persist_hp_calls_to_call_logs).
        coll = db["call_logs"]
        filt = {
            "user_id": current_user,
            "source": provider,
            "location_id": location_id,
        }

        total = await coll.count_documents(filt)
        cursor = (
            coll.find(filt)
            .sort("started_at", -1)
            .skip(skip)
            .limit(limit)
        )
        rows = await cursor.to_list(length=limit)
        data = [_serialize(r) for r in rows]

        logger.info(
            "list_call_logs: user=%s group=%s provider=%s total=%d returned=%d",
            current_user, group_id, provider, total, len(data),
        )

        return {
            "data": data,
            "meta": {
                "total": total,
                "returned": len(data),
                "skip": skip,
                "limit": limit,
                "provider": provider,
                "group_id": group_id,
                "location_id": location_id,
            },
        }


# ---------------------------------------------------------------------------
# POST /api/call_logs/analyze — single-call AI analysis (Sales-Hub button)
# ---------------------------------------------------------------------------

class AnalyzeCallRequest(BaseModel):
    """Address the call by Mongo id OR recording URL.

    The Sales-Hub Calls tab is fed by the HotProspector cache and its rows
    carry no Mongo id — the recording_url is the durable handle there. The
    optional meta fields only label the ad-hoc path (call not yet synced into
    call_logs by the HP cron); when a doc exists, its own fields win.
    """
    call_id: str | None = None
    recording_url: str | None = None
    force: bool = False
    # Display metadata from the HP cache row (ad-hoc path only).
    agent_name: str | None = None
    lead_name: str | None = None
    direction: str | None = None
    duration_seconds: int | None = None


@router.post("/call_logs/analyze")
async def analyze_call(
    request: AnalyzeCallRequest,
    current_user: str = Depends(get_current_user),
):
    """
    Transcribe (Whisper) + AI-analyze one call recording. Synchronous —
    typically 10-40s for an untranscribed call, instant when cached.

    Returns { call_id?, agent, lead_name, direction, duration_seconds,
              started_at, analysis, cached }.
    """
    from services import call_analysis_service
    from credits_middleware import check_credits

    if not request.call_id and not request.recording_url:
        raise HTTPException(status_code=422, detail="Provide call_id or recording_url.")

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        # Same stopper as chat: analysis spends real Whisper/model money.
        await check_credits(current_user, mongo_client)
        try:
            result = await call_analysis_service.analyze_single_call(
                db,
                current_user,
                call_id=request.call_id,
                recording_url=request.recording_url,
                extra_meta={
                    "raw_payload": {
                        "caller_name": request.agent_name,
                        "lead_name": request.lead_name,
                    },
                    "direction": request.direction,
                    "duration_seconds": request.duration_seconds,
                },
                force=request.force,
            )
        except ValueError as e:
            # Service-level "can't do that" errors (bad id, no recording,
            # transcription failure) — user-visible, not server faults.
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error("Single-call analysis failed for %s: %s", current_user, e, exc_info=True)
            raise HTTPException(status_code=500, detail="Call analysis failed. Please try again.")

    logger.info("analyze_call: user=%s cached=%s", current_user, result.get("cached"))
    return result
