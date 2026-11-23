"""
sandbox/ghl_app.py
------------------
The fake LeadConnector API (services.leadconnectorhq.com, *.gohighlevel.com, *.msgsndr.com).

Errors use LeadConnector's shape: ``{"statusCode": 401, "message": "...", "error": "Unauthorized"}``.
The token endpoints live here (``POST /oauth/token``: authorization_code and refresh_token, agency
and location; ``POST /oauth/locationToken``); every other endpoint the product calls (locations,
contacts, opportunities, pipelines, conversations, appointments, notes, tasks, users, custom
fields) is in sandbox/ghl_endpoints.py, built on the GHL view of the world in sandbox/ghl_data.py.
"""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sandbox.credentials import make_credential
from sandbox.fakes import ProviderError, build_app, request_fields, require_credential, world_of

HOSTS = [".leadconnectorhq.com", ".gohighlevel.com", ".msgsndr.com"]
TOKEN_EXPIRES_IN = 86399
SCOPE = ("oauth.readonly oauth.write contacts.readonly contacts.write locations.readonly locations.write "
         "opportunities.readonly opportunities.write conversations/message.readonly conversations/message.write")

router = APIRouter()


def ghl_error(status: int, message: str, **extra) -> JSONResponse:
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "Error"
    return JSONResponse({"statusCode": status, "message": message, "error": phrase, **extra}, status_code=status)


def _agency_tokens(world, user_id: str) -> dict:
    return {
        "access_token": make_credential(user_id, "ghl"),
        "token_type": "Bearer",
        "expires_in": TOKEN_EXPIRES_IN,
        "refresh_token": make_credential(user_id, "ghl_refresh"),
        "scope": SCOPE,
        "refreshTokenId": f"sbx-{world.agency.ghl_company_id[:12]}",
        "userType": "Company",
        "companyId": world.agency.ghl_company_id,
        "userId": world.agency.ghl_user_id,
        "isBulkInstallation": True,
    }


def _location_tokens(world, user_id: str, location_id: str) -> dict:
    return {
        "access_token": make_credential(user_id, f"ghl_location:{location_id}"),
        "token_type": "Bearer",
        "expires_in": TOKEN_EXPIRES_IN,
        "refresh_token": make_credential(user_id, f"ghl_location_refresh:{location_id}"),
        "scope": SCOPE,
        "userType": "Location",
        "companyId": world.agency.ghl_company_id,
        "locationId": location_id,
        "userId": world.agency.ghl_user_id,
        "planId": "sandbox",
    }


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
@router.post("/oauth/token")
async def oauth_token(request: Request):
    fields = await request_fields(request)
    grant = fields.get("grant_type")
    if grant == "authorization_code":
        cred = await require_credential(request, "ghl_code")
        return _agency_tokens(await world_of(cred), cred.user_id)
    if grant == "refresh_token":
        cred = await require_credential(request, "ghl_refresh", "ghl_location_refresh")
        world = await world_of(cred)
        if cred.base_kind == "ghl_location_refresh":
            return _location_tokens(world, cred.user_id, cred.arg)
        return _agency_tokens(world, cred.user_id)
    raise ProviderError(400, f"Unsupported grant_type: {grant}")


@router.post("/oauth/locationToken")
async def location_token(request: Request):
    cred = await require_credential(request, "ghl")
    fields = await request_fields(request)
    world = await world_of(cred)
    location_id = fields.get("locationId")
    if fields.get("companyId") != world.agency.ghl_company_id:
        raise ProviderError(400, "companyId does not match this token")
    if not world.client(location_id or "") or world.client(location_id).ghl_location_id != location_id:
        raise ProviderError(400, f"Location {location_id} is not part of this company")
    return JSONResponse(_location_tokens(world, cred.user_id, location_id), status_code=201)


from sandbox import ghl_endpoints  # noqa: E402,F401  (registers the phase 2 endpoints on router)


app = build_app("ghl", router, error=ghl_error, not_implemented_status=404)
