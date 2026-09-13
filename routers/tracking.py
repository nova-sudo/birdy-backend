"""
routers/tracking.py
-------------------
The public edge of the attribution system — the only Birdy endpoints a
stranger's browser talks to.

    GET  /t/{site_id}.js       the tracking snippet, with the account baked in
    POST /t/collect            a landing (Meta/UTM identifiers off the URL)
    POST /t/identify           a form submitted on the page
    POST /t/webhook/{site_id}  a form submitted somewhere we cannot read
    GET  /t/install/{site_id}  what the forwardable install page renders

Deliberate properties, all of which the tracker depends on:

  * **No authentication.** A site_id is a public key, like a Meta pixel id.
    It identifies which account a hit belongs to and grants nothing: you
    cannot read anything with it, only add pageviews to your own account.

  * **No CORS.** The bodies arrive as `text/plain`, which makes them CORS
    "simple requests" — no preflight, so the app's own strict CORS_ORIGINS
    allowlist stays untouched and the client never needs a response.

  * **Never an error.** Every *browser* write path answers 204 whatever
    happened. A bad site_id, malformed JSON or a wiped account must not put a
    red error in a customer's console on their own landing page, and a public
    endpoint that answers differently for real and fake ids is an account
    oracle.

    `/t/webhook/{site_id}` is the exception and reports real status codes: it is
    server-to-server, authenticated with a per-client secret, and has an
    operator wiring it up who needs to know when it is wrong.
"""

import hmac
import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, Response

from core.database import DB_NAME
from dependencies import get_mongo_client
from services.attribution_service import (
    META_URL_PARAMETERS,
    VISITORS,
    clean_touch,
    install_status,
    record_identity,
    record_touch,
    resolve_site,
    valid_visitor_id,
)
from services.tracked_leads import (
    SOURCE_TRACKER,
    SOURCE_WEBHOOK,
    record_tracked_lead,
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

    An unknown id still gets a working script rather than a 404 — it simply has
    nothing to report to. The id is validated where it matters, on ingest.

    The one lookup here is for this client's extra form hosts, and it is cheap
    twice over: `resolve_site` memoises for five minutes per process, and the
    response is browser-cached for an hour, so a busy landing page does not turn
    into a query per visitor.
    """
    site_id = site_file[:-3] if site_file.endswith(".js") else site_file
    endpoint = str(request.base_url).rstrip("/") + "/t"

    async with get_mongo_client() as mongo_client:
        site = await resolve_site(site_id, mongo_client)

    return Response(
        content=render_tracker(site_id, endpoint, (site or {}).get("form_hosts")),
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
    A form was submitted on the page: record the person, and the lead.

    Two things happen, and they are separate on purpose. The visitor gets an
    identity, which is what lets the backend join them to a GoHighLevel contact
    once one appears. And a `tracked_leads` row is written, which is what makes
    them a lead *now* — for a client whose form doesn't feed GHL, that row is
    the only record this person ever existed.
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

            # Read the visitor back rather than re-deriving the touch: the
            # identity write above may have created it, and its touch history
            # is what decides which ad gets the credit.
            visitor = await mongo_client[DB_NAME][VISITORS].find_one({"_id": visitor_id})
            await record_tracked_lead(
                site, payload, SOURCE_TRACKER, mongo_client, visitor=visitor
            )
        except Exception as e:
            # Never surface a stack trace into a customer's landing page.
            logger.error("identify failed for visitor %s: %s", visitor_id, e, exc_info=True)

    return _ok()


# ---------------------------------------------------------------------------
# POST /t/webhook/{site_id}
# ---------------------------------------------------------------------------

@router.post("/webhook/{site_id}")
async def form_webhook(
    site_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    Receive a form submission server-side, from a form we cannot read.

    Typeform, ROASForm, Jotform, a Zap, a Make scenario, the client's own
    backend — anything that lives in an iframe on someone else's domain is
    invisible to our script, so the form tool posts the submission here instead.

    Accepts `{email, phone, name, birdy_visitor_id?, ad_id?, utm_*?}`. When the
    visitor id came through the form, the lead inherits that visitor's ad
    attribution; otherwise it uses whatever identifiers the payload carried.

    **This endpoint reports failures, unlike the rest of this router.** The
    others answer 204 to everything because they run in a stranger's browser on
    a page we don't own, where an error is a red console message and a
    different answer for real and fake ids is an account oracle. This one is
    server-to-server with a person wiring it up who needs to know it is wrong.
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    async with get_mongo_client() as mongo_client:
        site = await resolve_site(site_id, mongo_client)
        if not site:
            raise HTTPException(status_code=404, detail="Unknown site id")

        group = await mongo_client[DB_NAME]["client_groups"].find_one(
            {"id": site["client_group_id"]}, {"lead_collection": 1}
        )
        secret = ((group or {}).get("lead_collection") or {}).get("webhook_secret")
        # Fail closed. An unconfigured webhook accepting anonymous posts would
        # let anyone write leads into a customer's reporting.
        if not secret:
            raise HTTPException(
                status_code=503,
                detail="No webhook secret configured for this client. Generate one in Birdy first.",
            )
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
        if not hmac.compare_digest(authorization.split(" ", 1)[1], secret):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

        visitor = None
        visitor_id = payload.get("birdy_visitor_id") or payload.get("visitor_id")
        if valid_visitor_id(visitor_id):
            visitor = await mongo_client[DB_NAME][VISITORS].find_one({"_id": visitor_id})

        result = await record_tracked_lead(
            site, payload, SOURCE_WEBHOOK, mongo_client,
            provider=_clip_provider(payload.get("provider")),
            visitor=visitor,
        )

    if not result["stored"]:
        raise HTTPException(
            status_code=400,
            detail="A lead needs an email or a phone number; this submission had neither.",
        )
    return {"received": True, **result}


def _clip_provider(value) -> str | None:
    """The form tool's own name, for display. Untrusted, so kept short."""
    if not isinstance(value, str):
        return None
    value = value.strip()[:40]
    return value or None


# ---------------------------------------------------------------------------
# GET /t/install/{site_id}
# ---------------------------------------------------------------------------

@router.get("/install/{site_id}")
async def install_details(site_id: str, request: Request):
    """
    What the public install page renders.

    No authentication: the whole point is that an agency forwards this link to
    whoever actually owns the landing page — a client, their web person, a
    developer at another company — none of whom have a Birdy login.

    So it returns the client's name and the snippet and nothing else about the
    account. An unknown site id gets the same shape with `known: false`, rather
    than a 404 that would confirm which ids are real.
    """
    endpoint = str(request.base_url).rstrip("/") + "/t"
    src = f"{endpoint}/{site_id}.js"
    body = {
        "known": False,
        "client_name": None,
        "site_id": site_id,
        "script_url": src,
        "snippet": f'<script async src="{src}"></script>',
        "meta_url_parameters": META_URL_PARAMETERS,
        "hit_received": False,
    }

    async with get_mongo_client() as mongo_client:
        site = await resolve_site(site_id, mongo_client)
        if not site:
            return body
        status = await install_status(site["client_group_id"], mongo_client)

    body["known"] = True
    body["client_name"] = site.get("client_group_name")
    body["hit_received"] = bool(status.get("installed"))
    body["ad_click_seen"] = bool(status.get("first_ad_click_seen"))
    return body
