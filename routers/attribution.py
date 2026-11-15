"""
routers/attribution.py
----------------------
The authenticated side of attribution — what the agency sees and installs.

    GET /attribution/portal/{group_id}           everything the portal renders
    PUT /attribution/lead-collection/{group_id}  how this client collects leads
    GET /attribution/diagnostics/{group_id}      the setup checklist
    GET /attribution/overview                    every client's tracking status
    GET /attribution/setup/{group_id}            site id, snippet, parameters
    GET /attribution/status/{group_id}           is the snippet live
    GET /attribution/leads-by-ad/{group_id}      attributed leads per Meta ad
    GET /attribution/funnel/{group_id}           the landing-page funnel

`portal` exists so the setup screen is one round trip rather than five: it mints
the site id on first read, so nothing has to be provisioned ahead of time, and
returns the snippet, the Meta URL-parameter string, the webhook URL and secret,
and the verification state together.
"""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.config import CORS_ORIGINS
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from pydantic import BaseModel

from services import attribution_reports
from services import lead_collection as lead_collection_service
from services.attribution_service import (
    META_URL_PARAMETERS,
    ensure_site_id,
    install_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attribution", tags=["attribution"])




async def _require_group(group_id: str, user_id: str, mongo_client) -> dict:
    group = await mongo_client[DB_NAME]["client_groups"].find_one(
        {"id": group_id, "user_id": user_id},
        projection={
            "id": 1, "name": 1, "user_id": 1,
            "attribution_site_id": 1, "lead_collection": 1,
        },
    )
    if not group:
        raise HTTPException(status_code=404, detail="Client group not found")
    return group


def _app_base() -> str:
    """
    Where the *frontend* lives, for links to pages the app renders.

    Distinct from `_tracking_base`, which is the API origin the snippet and the
    webhook point at. The public install page is a Next route, so building its
    URL from the API origin produced a link that 404s — and that link is the one
    thing in this feature designed to be forwarded to someone outside the
    account, who has no way to work out what went wrong.

    Falls back to the first configured CORS origin, which is the production
    frontend, so this needs no new configuration to be correct.
    """
    configured = os.getenv("APP_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return (CORS_ORIGINS[0] if CORS_ORIGINS else "").rstrip("/")


def _tracking_base(request: Request) -> str:
    """
    Where the snippet points. TRACKING_BASE_URL wins so the tag can be served
    from a customer-friendly host (track.birdy.ai) rather than the raw API
    origin; otherwise fall back to whatever host this request arrived on.
    """
    configured = os.getenv("TRACKING_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


@router.get("/setup/{group_id}")
async def get_setup(
    group_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Everything the customer needs to install tracking for one client."""
    async with get_mongo_client() as mongo_client:
        group = await _require_group(group_id, current_user, mongo_client)
        site_id = await ensure_site_id(group, mongo_client)
        status = await install_status(group_id, mongo_client)

    src = f"{_tracking_base(request)}/t/{site_id}.js"
    return {
        "group_id": group_id,
        "group_name": group.get("name"),
        "site_id": site_id,
        "snippet": f'<script async src="{src}"></script>',
        "script_url": src,
        "meta_url_parameters": META_URL_PARAMETERS,
        # The parameter a form provider (Typeform, ROASForm, a custom form)
        # should carry through untouched for a 100%-confidence match.
        "visitor_id_parameter": "birdy_visitor_id",
        **status,
    }


@router.get("/status/{group_id}")
async def get_status(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """Poll target for onboarding's '✓ Tracking detected' / '✓ First click seen'."""
    async with get_mongo_client() as mongo_client:
        await _require_group(group_id, current_user, mongo_client)
        return await install_status(group_id, mongo_client)


@router.get("/leads-by-ad/{group_id}")
async def get_leads_by_ad(
    group_id: str,
    start: str | None = Query(default=None, description="ISO date, inclusive"),
    end: str | None = Query(default=None, description="ISO date, inclusive"),
    include_predated: bool = Query(
        default=False,
        description="Include contacts that already existed before the click",
    ),
    current_user: str = Depends(get_current_user),
):
    """
    Attributed leads per Meta ad — the ad → lead half of the funnel.

    Counted by when the contact was created, so the totals reconcile with
    every other lead figure in Birdy rather than with when we happened to
    resolve the match.
    """
    async with get_mongo_client() as mongo_client:
        await _require_group(group_id, current_user, mongo_client)
        try:
            return await attribution_reports.leads_by_ad_report(
                current_user, group_id, start, end, mongo_client, include_predated=include_predated,
            )
        except attribution_reports.AttributionInputError as e:
            raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# The landing-page funnel
# ---------------------------------------------------------------------------

@router.get("/funnel/{group_id}")
async def get_landing_funnel(
    group_id: str,
    start: str | None = Query(default=None, description="ISO date, inclusive"),
    end: str | None = Query(default=None, description="ISO date, inclusive"),
    current_user: str = Depends(get_current_user),
):
    """
    Landing views → form starts → opt-ins for one client, with the drop-off.

    `applicable` is the field the hubs read before drawing anything. A client on
    Meta Instant Forms has no landing page of ours to measure and gets a hard
    false — four zeroes on a screen imply a page that is failing, when the truth
    is that there is no page. A client who has not said how they collect leads
    yet gets the funnel only once their pages have actually reported something,
    so declaring the method stays a convenience rather than a prerequisite.
    """
    if not start or not end:
        raise HTTPException(status_code=400, detail="start and end are required")

    async with get_mongo_client() as mongo_client:
        group = await _require_group(group_id, current_user, mongo_client)
        try:
            return await attribution_reports.funnel_report(group, start, end, mongo_client)
        except attribution_reports.AttributionInputError as e:
            raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# The setup portal
# ---------------------------------------------------------------------------

class LeadCollectionRequest(BaseModel):
    method: str
    form_provider: str | None = None
    push_to_ghl: bool = False
    # Hosts this client's form tool lives on, if it isn't one of the providers
    # every account shares.
    form_hosts: list[str] | None = None


@router.get("/portal/{group_id}")
async def get_portal(
    group_id: str,
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """
    Everything the per-client setup portal renders, in one call.

    Deliberately one round trip: the portal shows the snippet, the Meta
    parameters, the webhook details and the live checklist on one screen, and
    five separate fetches would make it flicker through five loading states on a
    page whose whole job is to feel like a checklist.
    """
    async with get_mongo_client() as mongo_client:
        group = await _require_group(group_id, current_user, mongo_client)
        site_id = await ensure_site_id(group, mongo_client)
        config = lead_collection_service.read(group)
        diagnostics = await lead_collection_service.diagnose(group, mongo_client)
        status = await install_status(group_id, mongo_client)

    base = _tracking_base(request)
    src = base + "/t/" + site_id + ".js"
    return {
        "group_id": group_id,
        "group_name": group.get("name"),
        "site_id": site_id,
        "lead_collection": {
            # The webhook secret is returned deliberately: whoever is wiring up
            # Typeform has to paste it. It grants only the ability to add leads
            # to this one client, and nothing to read.
            **config,
            "needs_script": config["method"] in lead_collection_service.NEEDS_SCRIPT,
            "needs_webhook": config["method"] in lead_collection_service.NEEDS_WEBHOOK,
        },
        "snippet": '<script async src="' + src + '"></script>',
        "script_url": src,
        "meta_url_parameters": META_URL_PARAMETERS,
        "visitor_id_parameter": "birdy_visitor_id",
        "webhook_url": base + "/t/webhook/" + site_id,
        "install_page_url": _app_base() + "/install/" + site_id,
        "diagnostics": diagnostics,
        **status,
    }


@router.put("/lead-collection/{group_id}")
async def put_lead_collection(
    group_id: str,
    body: LeadCollectionRequest,
    current_user: str = Depends(get_current_user),
):
    """Record how this client collects their leads."""
    # An older page may still send the retired "external_form"; it means the
    # same client, so it is accepted and stored under its new name.
    if body.method not in (*lead_collection_service.METHODS,
                           lead_collection_service.METHOD_EXTERNAL_FORM):
        raise HTTPException(
            status_code=400,
            detail="method must be one of: " + ", ".join(lead_collection_service.METHODS),
        )

    async with get_mongo_client() as mongo_client:
        await _require_group(group_id, current_user, mongo_client)
        config = await lead_collection_service.save(
            group_id, body.method, mongo_client,
            form_provider=body.form_provider,
            push_to_ghl=body.push_to_ghl,
            form_hosts=body.form_hosts,
        )
    return {"ok": True, "lead_collection": config}


@router.get("/diagnostics/{group_id}")
async def get_diagnostics(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """The setup checklist — what has happened, and the next thing that must."""
    async with get_mongo_client() as mongo_client:
        group = await _require_group(group_id, current_user, mongo_client)
        return await lead_collection_service.diagnose(group, mongo_client)


@router.get("/overview")
async def get_overview(current_user: str = Depends(get_current_user)):
    """
    One row per client for the agency-wide Tracking table.

    Onboarding imports sub-accounts in bulk, so an agency finishes the wizard
    with fourteen clients and needs one screen to see which are live and chase
    the rest — not fourteen portals opened one at a time. Unconfigured clients
    sort first, because they are the ones needing attention.
    """
    async with get_mongo_client() as mongo_client:
        return await attribution_reports.overview_report(current_user, mongo_client)
