"""
routers/tracking.py
-------------------
The public edge of the attribution system — the only Birdy endpoints a
stranger's browser talks to.

    GET  /t/{site_id}.js   the tracking snippet, with the account baked in
    POST /t/collect        a landing (Meta/UTM identifiers off the URL)
    POST /t/identify       an email/phone typed into a form on the page

Deliberate properties, all of which the tracker depends on:

  * **No authentication.** A site_id is a public key, like a Meta pixel id.
    It identifies which account a hit belongs to and grants nothing: you
    cannot read anything with it, only add pageviews to your own account.

  * **No CORS.** The bodies arrive as `text/plain`, which makes them CORS
    "simple requests" — no preflight, so the app's own strict CORS_ORIGINS
    allowlist stays untouched and the client never needs a response.

  * **Never an error.** Every write path answers 204 whatever happened. A
    bad site_id, malformed JSON or a wiped account must not put a red error
    in a customer's console on their own landing page, and a public endpoint
    that answers differently for real and fake ids is an account oracle.
"""

import json
import logging

from fastapi import APIRouter, Request, Response

from dependencies import get_mongo_client
from services.attribution_service import (
    clean_touch,
    record_identity,
    record_touch,
    resolve_site,
    valid_visitor_id,
)
from services.tracker_script import render_tracker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/t", tags=["tracking"])


# A hit body is a handful of short strings; anything larger is not ours.
MAX_BODY_BYTES = 8_192

def _ok() -> Response:
    """204, always. A new Response per call — Starlette responses are stateful."""
    return Response(status_code=204)


async def _read_json(request: Request) -> dict | None:
    """Parse the beacon body ourselves — it arrives as text/plain, not JSON."""
    raw = await request.body()
    if not raw or len(raw) > MAX_BODY_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# GET /t/{site_id}.js
# ---------------------------------------------------------------------------

@router.get("/{site_file}")
async def tracker_script(site_file: str, request: Request):
    """
    Serve the tracker for a site id, e.g. GET /t/abc123XYZ.js

    Not checked against the database on purpose: this runs on every page load
    of every customer landing page, and serving an inert script for an unknown
    id costs nothing while a lookup here would cost a round trip per visitor.
    The id is validated where it matters — on ingest.
    """
    site_id = site_file[:-3] if site_file.endswith(".js") else site_file
    endpoint = str(request.base_url).rstrip("/") + "/t"
    return Response(
        content=render_tracker(site_id, endpoint),
        media_type="application/javascript; charset=utf-8",
        headers={
            # Long enough that repeat visitors don't re-fetch, short enough
            # that a fix reaches every installed site the same day.
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# POST /t/collect
# ---------------------------------------------------------------------------

@router.post("/collect", status_code=204)
async def collect(request: Request):
    """Record a landing: which ad/campaign identifiers were on the URL."""
    payload = await _read_json(request)
    if not payload:
        return _ok()

    visitor_id = payload.get("visitor_id")
    if not valid_visitor_id(visitor_id):
        return _ok()

    touch = clean_touch(payload.get("touch"))

    async with get_mongo_client() as mongo_client:
        site = await resolve_site(payload.get("site_id"), mongo_client)
        if not site:
            return _ok()
        await record_touch(site, visitor_id, touch, mongo_client)

    return _ok()


# ---------------------------------------------------------------------------
# POST /t/identify
# ---------------------------------------------------------------------------

@router.post("/identify", status_code=204)
async def identify(request: Request):
    """
    Attach an email/phone to a visitor.

    This is the moment an anonymous click becomes a person, and it is what
    lets the backend join to a GoHighLevel contact later even when the form
    provider dropped every parameter we handed it.
    """
    payload = await _read_json(request)
    if not payload:
        return _ok()

    visitor_id = payload.get("visitor_id")
    if not valid_visitor_id(visitor_id):
        return _ok()

    email = payload.get("email")
    phone = payload.get("phone")
    if not email and not phone:
        return _ok()

    async with get_mongo_client() as mongo_client:
        site = await resolve_site(payload.get("site_id"), mongo_client)
        if not site:
            return _ok()
        try:
            await record_identity(site, visitor_id, email, phone, mongo_client)
        except Exception as e:
            # Never surface a stack trace into a customer's landing page.
            logger.error("identify failed for visitor %s: %s", visitor_id, e, exc_info=True)

    return _ok()
