"""
routers/webhooks.py
-------------------
Inbound webhook endpoints for third-party integrations.

Today: call-log events from GHL (and future providers — Hot Prospector,
others). The handler:
    1. Authenticates with a shared secret in `Authorization: Bearer ...`
       (same idiom as routers/cron.py).
    2. Identifies the source via the `X-Call-Source` header
       (e.g. "GHL"), so multiple providers share the same URL.
    3. Hands the body to services/call_logs_service for normalization
       and storage in the `call_logs` collection.

Provider configuration (e.g. GHL workflow "Webhook" step):

    URL:        https://<your-backend>/webhooks/call_logs
    Method:     POST
    Headers:
        Content-Type:    application/json
        Authorization:   Bearer <CALL_LOGS_WEBHOOK_SECRET>
        X-Call-Source:   GHL

    Body: provider-specific JSON. See services/call_logs_service for the
    field names we look for (location_id, contact_id, phone, status,
    direction, duration, started_at, recording_url, ...). Unknown fields
    are preserved in raw_payload for debug / replay.
"""

import hmac
import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request

from dependencies import get_mongo_client
from services.call_logs_service import resolve_source, save_call_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# Headers we strip out before persisting the request envelope — never store
# secrets in the DB.
_REDACTED_HEADERS = {"authorization", "cookie", "x-webhook-secret"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _verify_webhook_secret(authorization: str | None) -> None:
    """
    Constant-time compare of the Bearer token against CALL_LOGS_WEBHOOK_SECRET.
    Fail-closed if the env var is not set (refuse the request rather than
    accept everything).
    """
    expected = os.getenv("CALL_LOGS_WEBHOOK_SECRET")
    if not expected:
        logger.error("CALL_LOGS_WEBHOOK_SECRET env var is not set — refusing webhook")
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")

    received = authorization.split(" ", 1)[1]
    if not hmac.compare_digest(received, expected):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")


def _sanitize_headers(raw: dict) -> dict:
    """Drop secret-carrying headers before persisting the request envelope."""
    return {k: v for k, v in raw.items() if k.lower() not in _REDACTED_HEADERS}


# ---------------------------------------------------------------------------
# POST /webhooks/call_logs
# ---------------------------------------------------------------------------

@router.post("/call_logs")
async def receive_call_log(
    request: Request,
    authorization: str | None = Header(default=None),
    x_call_source: str | None = Header(default=None),
):
    """
    Receive a call-log event from a configured provider.

    Required headers:
        Authorization: Bearer <CALL_LOGS_WEBHOOK_SECRET>
        X-Call-Source: GHL                (or another value in ALLOWED_SOURCES)
        Content-Type:  application/json

    Responses:
        200  {"received": true, "id": "...", "duplicate": false, ...}
        400  bad source or bad JSON
        401  bad / missing secret
        503  CALL_LOGS_WEBHOOK_SECRET env var not configured
    """
    _verify_webhook_secret(authorization)

    source = resolve_source(x_call_source)
    if not source:
        raise HTTPException(
            status_code=400,
            detail=(
                "X-Call-Source header missing or not in allowed list. "
                "Set it to one of: ghl, hotprospector."
            ),
        )

    try:
        payload = await request.json()
    except Exception as e:
        logger.warning("Bad JSON on /webhooks/call_logs: %s", e)
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    headers = _sanitize_headers(dict(request.headers))

    async with get_mongo_client() as mongo_client:
        result = await save_call_log(source, payload, headers, mongo_client)

    return {"received": True, **result}
