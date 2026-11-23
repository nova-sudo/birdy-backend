"""
sandbox/meta_app.py
-------------------
The fake Graph API (graph.facebook.com and the rest of *.facebook.com / *.fbcdn.net).

Paths are matched without the version prefix: ``/v25.0/me/adaccounts`` is served by
``@router.get("/me/adaccounts")`` (the version is in ``request.scope["graph_version"]``).
Errors use Graph's shape: ``{"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}``.

Phase 1 has the connect flow only: code -> token, the long-lived exchange, /me, /me/permissions,
/debug_token. Phase 2: add endpoints to ``router`` below (an ADD ENDPOINTS HERE marker shows where).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sandbox.credentials import make_credential
from sandbox.fakes import ProviderError, build_app, request_fields, require_credential, world_of

HOSTS = [".facebook.com", ".fbcdn.net"]
TOKEN_EXPIRES_IN = 60 * 24 * 3600 - 5000   # what Meta gives a long-lived token, roughly
SCOPES = ["ads_management", "ads_read", "read_insights", "pages_show_list", "business_management",
          "leads_retrieval", "pages_read_engagement", "pages_manage_ads", "public_profile"]

router = APIRouter()


def graph_error(status: int, message: str, *, code: int = None, type: str = None, error_subcode: int = None,
                **extra) -> JSONResponse:
    """Graph's error body. 401s become Graph's OAuthException (code 190, sent as HTTP 400)."""
    if status == 401:
        status, code, type = 400, code or 190, type or "OAuthException"
    body = {"message": message, "type": type or ("GraphMethodException" if status < 500 else "FacebookApiException"),
            "code": code or (100 if status < 500 else 2), "fbtrace_id": "sandbox"}
    if error_subcode:
        body["error_subcode"] = error_subcode
    body.update(extra)
    return JSONResponse({"error": body}, status_code=status)


_VERSION = re.compile(r"^/v\d+\.\d+(?=/|$)")


def strip_version(app):
    """ASGI wrapper: /v25.0/x -> /x, keeping the version in scope["graph_version"]."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            m = _VERSION.match(scope.get("path", ""))
            if m:
                path = scope["path"][m.end():] or "/"
                scope = {**scope, "path": path, "raw_path": path.encode(), "graph_version": m.group(0)[1:]}
        await app(scope, receive, send)
    return wrapped


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------
@router.api_route("/oauth/access_token", methods=["GET", "POST"])
async def access_token(request: Request):
    """``code`` -> short-lived token, or ``grant_type=fb_exchange_token`` -> long-lived token."""
    fields = await request_fields(request)
    if fields.get("grant_type") == "fb_exchange_token" or fields.get("fb_exchange_token"):
        cred = await require_credential(request, "meta")
    elif fields.get("code"):
        cred = await require_credential(request, "meta_code")
    else:
        raise ProviderError(400, "Missing code or fb_exchange_token", code=100)
    return {"access_token": make_credential(cred.user_id, "meta"), "token_type": "bearer",
            "expires_in": TOKEN_EXPIRES_IN}


@router.get("/me")
async def me(request: Request):
    cred = await require_credential(request, "meta")
    world = await world_of(cred)
    return {"id": world.agency.meta_user_id, "name": world.agency.meta_user_name}


@router.get("/me/permissions")
async def me_permissions(request: Request):
    await require_credential(request, "meta")
    return {"data": [{"permission": p, "status": "granted"} for p in SCOPES]}


@router.get("/debug_token")
async def debug_token(request: Request):
    cred = await require_credential(request, "meta")
    world = await world_of(cred)
    return {"data": {"app_id": "sandbox", "type": "USER", "application": "Birdy (sandbox)", "is_valid": True,
                     "expires_at": 0, "data_access_expires_at": 0, "scopes": SCOPES,
                     "user_id": world.agency.meta_user_id}}


# ---------------------------------------------------------------------------
# ADD ENDPOINTS HERE (phase 2): /me/adaccounts, /{act}/campaigns, /{act}/ads, /{act}/insights,
# the batch POST "/", /{ad_id}?fields=leads{...}, status / budget writes via sandbox.state.
# ---------------------------------------------------------------------------


app = build_app("meta", router, error=graph_error, not_implemented_status=400, middleware=[strip_version])
