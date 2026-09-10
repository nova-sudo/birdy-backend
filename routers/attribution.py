"""
routers/attribution.py
----------------------
The authenticated side of attribution — what the agency sees and installs.

    GET /attribution/setup/{group_id}    site id, snippet, Meta URL parameters
    GET /attribution/status/{group_id}   is the snippet live, are clicks landing
    GET /attribution/leads-by-ad/{group_id}
                                         attributed leads per Meta ad

The setup endpoint is the one that makes onboarding two steps instead of ten:
it hands back a ready-to-paste script tag and the exact Meta URL-parameter
string, and mints the site id on first request so nothing has to be
provisioned ahead of time.
"""

import logging
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services.attribution_service import (
    ensure_site_id,
    install_status,
    leads_by_ad,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attribution", tags=["attribution"])


# Meta's dynamic URL parameters. The names are cosmetic — `ad_id` is what the
# reports join on, and Birdy already knows what that ad is called, so a client
# who mistypes a campaign name here costs themselves nothing.
META_URL_PARAMETERS = (
    "utm_source=facebook"
    "&utm_medium=paid"
    "&utm_campaign={{campaign.name}}"
    "&utm_content={{ad.name}}"
    "&campaign_id={{campaign.id}}"
    "&adset_id={{adset.id}}"
    "&ad_id={{ad.id}}"
)


async def _require_group(group_id: str, user_id: str, mongo_client) -> dict:
    group = await mongo_client[DB_NAME]["client_groups"].find_one(
        {"id": group_id, "user_id": user_id},
        projection={"id": 1, "name": 1, "user_id": 1, "attribution_site_id": 1},
    )
    if not group:
        raise HTTPException(status_code=404, detail="Client group not found")
    return group


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
    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date: {value}")

    async with get_mongo_client() as mongo_client:
        await _require_group(group_id, current_user, mongo_client)
        rows = await leads_by_ad(
            current_user, group_id, _parse(start), _parse(end),
            mongo_client, include_predated=include_predated,
        )

    return {
        "group_id": group_id,
        "start": start,
        "end": end,
        "ads": rows,
        "attributed_leads": sum(r["leads"] for r in rows),
    }
