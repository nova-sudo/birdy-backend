"""
sandbox/hp_app.py
-----------------
The fake Hot Prospector API: Custom API v2 on service.hookscall.com (``/glu/api/v2/auth/token``,
``/glu/api/v2/auth/refresh``, ``/glu/api/v2/request`` with a Bearer token) and the legacy v1
``app.hotprospector.com/glu/custom_api`` (credentials in the body).

Operations are routed by the ``Method`` field of the body. Register one with ``@method("Name")``;
the handler gets ``(cred, body, world)`` and returns the JSON to send (HP's envelope is
``{"response": "true", ...}``; an error is ``{"response": "false", "message": "..."}``, HTTP 200).
An unknown Method answers that error shape and logs ``sandbox_unhandled``. The operations live in
sandbox/hp_methods.py (shapes in sandbox/hp_rows.py).

Hosts: HP's own domains, plus whatever host HOTPROSPECTOR_BASE_URL / _AUTH_URL / _REFRESH_URL /
_LEGACY_URL point at (``configured_hosts``; sandbox/routing.py treats those as HP hosts too), so an
override cannot send a sandbox credential anywhere real.

Recordings: sandbox calls report none (empty ``recording``), so the product never downloads fake
audio or sends it to a transcription service. A request for a world recording URL answers 404.
"""

from __future__ import annotations

import os
import sys
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from sandbox.credentials import Credential, make_credential
from sandbox.fakes import ProviderError, build_app, not_implemented, request_fields, require_credential, world_of

HOSTS = [".hookscall.com", ".hotprospector.com"]
TOKEN_EXPIRES_IN = 21600
# The env variables integrations/hotprospector.py reads its endpoints from.
URL_ENV = ("HOTPROSPECTOR_BASE_URL", "HOTPROSPECTOR_AUTH_URL", "HOTPROSPECTOR_REFRESH_URL", "HOTPROSPECTOR_LEGACY_URL")
_CONFIG_KEYS = ("base_url", "auth_token_url", "auth_refresh_url", "legacy_base_url")

router = APIRouter()
METHODS: dict[str, Callable[..., Awaitable[dict]]] = {}


def hp_error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"success": False, "response": "false", "message": message, **extra}, status_code=status)


def method(name: str):
    """Register a handler for an HP ``Method``: ``async def handler(cred, body, world) -> dict``."""
    def register(fn):
        METHODS[name] = fn
        return fn
    return register


def sandbox_hp_credentials(user_id: str) -> tuple[str, str]:
    """The (api_uid, api_key) a sandbox account's Hot Prospector connect stores. Stable per account."""
    return make_credential(user_id, "hp_uid", nonce=""), make_credential(user_id, "hp_key", nonce="")


def configured_hosts() -> set[str]:
    """The hosts the product's Hot Prospector endpoints point at, from the env overrides and, once
    loaded, integrations.hotprospector.HOTPROSPECTOR_CONFIG (which can be changed at runtime)."""
    urls = [os.getenv(name) for name in URL_ENV]
    config = getattr(sys.modules.get("integrations.hotprospector"), "HOTPROSPECTOR_CONFIG", None)
    if isinstance(config, dict):
        urls += [config.get(key) for key in _CONFIG_KEYS]
    hosts = set()
    for url in urls:
        try:
            host = urlsplit(str(url or "")).hostname
        except ValueError:
            host = None
        if host:
            hosts.add(host.lower().rstrip("."))
    return hosts


def _tokens(user_id: str) -> dict:
    return {"success": True, "data": {"access_token": make_credential(user_id, "hp"),
                                      "refresh_token": make_credential(user_id, "hp_refresh"),
                                      "token_type": "Bearer", "expires_in": TOKEN_EXPIRES_IN}}


# ---------------------------------------------------------------------------
# Auth (v2)
# ---------------------------------------------------------------------------
@router.post("/glu/api/v2/auth/token")
async def auth_token(request: Request):
    fields = await request_fields(request)
    uid = await require_credential(request, "hp_uid")
    key = await require_credential(request, "hp_key")
    if uid.user_id != key.user_id or not str(fields.get("api_key", "")).startswith("sbx_"):
        raise ProviderError(401, "Invalid api_uId or api_key")
    return _tokens(uid.user_id)


@router.post("/glu/api/v2/auth/refresh")
async def auth_refresh(request: Request):
    cred = await require_credential(request, "hp_refresh")
    return _tokens(cred.user_id)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------
async def _dispatch(request: Request, cred: Credential):
    body = await request_fields(request)
    name = str(body.get("Method") or "")
    handler = METHODS.get(name)
    if handler is None:
        return not_implemented("hp", request.method, request.url.path, hp_error, status=200,
                               detail=f"Method={name or '(none)'}")
    return await handler(cred, body, await world_of(cred))


@router.post("/glu/api/v2/request")
async def request_v2(request: Request):
    return await _dispatch(request, await require_credential(request, "hp"))


@router.post("/glu/custom_api")
async def request_v1(request: Request):
    # Legacy: api_uId and api_key travel in the body, and must be the same account's.
    uid = await require_credential(request, "hp_uid")
    key = await require_credential(request, "hp_key")
    if uid.user_id != key.user_id:
        raise ProviderError(401, "Invalid api_uId or api_key")
    return await _dispatch(request, uid)


@router.get("/sandbox/{token}/{name}")
async def recording(token: str, name: str):
    """A world recording URL (sandbox/world.py points call recordings here). Sandbox calls have no
    recording, so there is nothing to download, and no fake audio reaches a transcription service."""
    return hp_error(404, "Recording not found: sandbox calls have no recordings")


# ---------------------------------------------------------------------------
# Operations: sandbox/hp_methods.py registers them with @method (imported here so they exist
# whenever this app does).
# ---------------------------------------------------------------------------
from sandbox import hp_methods  # noqa: E402,F401


app = build_app("hp", router, error=hp_error, not_implemented_status=404)
