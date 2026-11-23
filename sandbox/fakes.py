"""
sandbox/fakes.py
----------------
What the three fake provider apps share: building the app (routes, then the not-implemented
catch-all, error handlers), reading the sandbox credential off a request, and raising an error
in the provider's own shape.

A fake is a FastAPI app built from one APIRouter:

    router = APIRouter()

    @router.get("/me")
    async def me(request: Request):
        cred = await require_credential(request, "meta")      # 401 in the provider's shape if absent
        world = await world_of(cred)                           # the account's world, writes overlaid
        ...

    app = build_app("meta", router, error=graph_error, not_implemented_status=400)

Endpoints are plain FastAPI handlers, so they can take query/body parameters as usual.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Iterable, Optional
from urllib.parse import parse_qsl

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from sandbox.credentials import Credential, find_credentials

logger = logging.getLogger(__name__)

# error(status, message, **extra) -> JSONResponse, in the provider's own error shape.
ErrorFn = Callable[..., JSONResponse]


class ProviderError(Exception):
    """Raise inside a fake endpoint; the app answers it with its provider's error shape."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


async def request_fields(request: Request) -> dict:
    """Query params, then form or JSON body fields, merged (body wins). Cached on the request."""
    cached = getattr(request.state, "sandbox_fields", None)
    if cached is not None:
        return cached
    fields = dict(request.query_params)
    body = await request.body()
    if body:
        ctype = request.headers.get("content-type", "")
        if "json" in ctype or body[:1] in (b"{", b"["):
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    fields.update(parsed)
                else:
                    fields["_json"] = parsed
            except ValueError:
                pass
        else:
            fields.update(parse_qsl(body.decode("utf-8", errors="ignore"), keep_blank_values=True))
    request.state.sandbox_fields = fields
    return fields


async def credentials_of(request: Request) -> list[Credential]:
    """Every valid sandbox credential on the request: Authorization header first, then the URL,
    then the body."""
    found = find_credentials(request.headers.get("authorization", ""))
    found += find_credentials(str(request.url))
    found += find_credentials(await request.body())
    return found


async def require_credential(request: Request, *kinds: str) -> Credential:
    """The first credential whose kind (or base kind, ``ghl_location`` for ``ghl_location:x``) is
    one of ``kinds`` (any kind when none given). Raises a 401 ProviderError when there is none."""
    for cred in await credentials_of(request):
        if not kinds or cred.kind in kinds or cred.base_kind in kinds:
            return cred
    wanted = ", ".join(kinds) if kinds else "any"
    raise ProviderError(401, f"sandbox: no valid sandbox credential ({wanted}) on this request",
                        reason="invalid_token")


async def world_of(cred: Credential):
    """The credential's account world with its remembered writes applied."""
    from sandbox import state, world

    return world.world_for(cred.user_id, overrides=await state.overrides(cred.user_id))


def default_error(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": status, **extra}}, status_code=status)


def build_app(provider: str, router: APIRouter, *, error: ErrorFn = default_error,
              not_implemented_status: int = 501, middleware: Iterable[Callable] = ()) -> Callable:
    """A FastAPI app serving ``router``; any other path answers "not implemented" in the provider's
    error shape and logs ``sandbox_unhandled``. ``middleware`` wraps the result (pure ASGI
    callables taking the app), e.g. to strip a version prefix from the path."""
    app = FastAPI(title=f"sandbox {provider}", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError):
        return error(exc.status, exc.message, **exc.extra)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception):
        logger.exception("sandbox_error provider=%s method=%s path=%s", provider, request.method, request.url.path)
        return error(500, f"sandbox: {provider} fake failed on {request.method} {request.url.path}: {exc}")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def _not_implemented(request: Request, path: str):
        return not_implemented(provider, request.method, request.url.path, error, not_implemented_status)

    asgi = app
    for wrap in middleware:
        asgi = wrap(asgi)
    return asgi


def not_implemented(provider: str, method: str, path: str, error: ErrorFn = default_error,
                    status: int = 501, detail: Optional[str] = None) -> JSONResponse:
    """The answer to an endpoint a fake does not have yet (also for an unknown HP Method)."""
    what = f"{method} {path}" + (f" {detail}" if detail else "")
    logger.warning("sandbox_unhandled provider=%s method=%s path=%s%s", provider, method, path,
                   f" detail={detail}" if detail else "")
    return error(status, f"sandbox: {what} is not implemented", sandbox_unhandled=True)
