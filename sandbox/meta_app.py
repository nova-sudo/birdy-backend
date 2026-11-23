"""
sandbox/meta_app.py
-------------------
The fake Graph API (graph.facebook.com and the rest of *.facebook.com / *.fbcdn.net).

Paths are matched without the version prefix: ``/v25.0/me/adaccounts`` is served by
``@router.get("/me/adaccounts")`` (the version is in ``request.scope["graph_version"]``).
Errors use Graph's shape: ``{"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}``.

Phase 1: the connect flow (code -> token, the long-lived exchange, /me/permissions, /debug_token).
Phase 2: everything else Birdy asks Graph, answered from the account's world by sandbox/meta_graph.py:
any node (``GET /{id}``, with field expansion), any edge (``GET /{id}/{edge}``, paged), insights,
leads, the batch API (``POST /`` with ``batch``), writes (``POST /{id}``, ``POST /act_x/{edge}``,
``DELETE /{id}``), and placeholder media on the fbcdn host (``GET /sandbox/{token}/{file}``).
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from sandbox.credentials import make_credential
from sandbox.credentials import find_credentials
from sandbox.fakes import ProviderError, build_app, request_fields, require_credential, world_of

logger = logging.getLogger(__name__)

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
# Phase 2: the Graph API over the world (sandbox/meta_graph.py)
# ---------------------------------------------------------------------------
TOKEN_KINDS = ("meta", "meta_page")     # a user token, or a Page token (GET /{page}?fields=access_token)


def _params(fields: dict) -> dict:
    return {k: v for k, v in fields.items() if k != "_json"}


async def _ctx(request: Request, cred=None):
    from sandbox import meta_graph

    cred = cred or await require_credential(request, *TOKEN_KINDS)
    return await meta_graph.load_ctx(cred, request.scope.get("graph_version"))


def _error_body(exc: ProviderError) -> tuple:
    resp = graph_error(exc.status, exc.message, **exc.extra)
    return resp.status_code, json.loads(resp.body)


@router.get("/sandbox/{token}/{filename}")
async def cdn_media(token: str, filename: str):
    """Placeholder media on *.fbcdn.net (what the backend downloads, e.g. /act/adimages ``url``)."""
    from sandbox import meta_media

    info = meta_media.read_token(token)
    if info is None:
        raise ProviderError(404, "sandbox: unknown media", code=100)
    return Response(meta_media.png(info[2]), media_type="image/png",
                    headers={"cache-control": "public, max-age=86400"})


@router.post("/")
async def batch(request: Request):
    """Graph's batch API: ``batch`` = JSON [{method, relative_url, body?, name?}] -> [{code, headers, body}]."""
    from sandbox import meta_graph

    fields = await request_fields(request)
    if "batch" not in fields:
        raise ProviderError(400, "(#100) The parameter batch is required for a POST to /", code=100)
    try:
        items = json.loads(fields["batch"])
    except (TypeError, ValueError):
        raise ProviderError(400, "(#100) Param batch must be a JSON array", code=100)
    if not isinstance(items, list) or not items:
        raise ProviderError(400, "(#100) Param batch must be a non-empty JSON array", code=100)
    if len(items) > 50:
        raise ProviderError(400, "(#1) Maximum number of requests in a batch is 50", code=1)
    outer = await require_credential(request, *TOKEN_KINDS)
    ctx = await _ctx(request, outer)
    include_headers = str(fields.get("include_headers", "true")).lower() != "false"
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("relative_url"):
            out.append({"code": 400, "body": json.dumps({"error": {
                "message": "(#100) Each batch item needs a relative_url", "type": "OAuthException", "code": 100,
                "fbtrace_id": "sandbox"}}, separators=(",", ":"))})
            continue
        method = str(item.get("method") or "GET").upper()
        path, _, query = str(item["relative_url"]).partition("?")
        params = dict(parse_qsl(query, keep_blank_values=True))
        if item.get("body"):
            params.update(parse_qsl(str(item["body"]), keep_blank_values=True))
        item_ctx = ctx
        own = find_credentials(params.get("access_token", ""))
        if own and own[0].kind in TOKEN_KINDS and own[0].raw != outer.raw:
            item_ctx = await _ctx(request, own[0])
        try:
            body, code = await meta_graph.handle(item_ctx, method, path, params), 200
        except ProviderError as exc:
            code, body = _error_body(exc)
        except Exception as exc:   # one item failing must not fail the batch
            logger.exception("sandbox_error provider=meta batch item %s %s", method, path)
            code, body = 500, {"error": {"message": f"sandbox: {exc}", "type": "FacebookApiException", "code": 2,
                                         "fbtrace_id": "sandbox"}}
        entry = {"code": code, "body": json.dumps(body, separators=(",", ":"))}
        if include_headers:
            entry["headers"] = [{"name": "Content-Type", "value": "text/javascript; charset=UTF-8"}]
        out.append(entry)
        if method != "GET":
            ctx = await _ctx(request, outer)    # later items read what this one wrote
    return out


@router.api_route("/{node_id}", methods=["GET", "POST", "DELETE"])
async def node(request: Request, node_id: str):
    from sandbox import meta_graph

    ctx = await _ctx(request)
    return await meta_graph.handle(ctx, request.method, f"/{node_id}", _params(await request_fields(request)))


@router.api_route("/{node_id}/{edge}", methods=["GET", "POST", "DELETE"])
async def node_edge(request: Request, node_id: str, edge: str):
    from sandbox import meta_graph

    ctx = await _ctx(request)
    return await meta_graph.handle(ctx, request.method, f"/{node_id}/{edge}",
                                   _params(await request_fields(request)))


app = build_app("meta", router, error=graph_error, not_implemented_status=400, middleware=[strip_version])
