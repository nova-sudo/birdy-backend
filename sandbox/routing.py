"""
sandbox/routing.py
------------------
The one choke point. ``install()`` wraps ``httpx.AsyncClient.send`` (which ``get``, ``post``,
``request`` and ``stream`` all go through, whatever ``transport=`` or ``mounts=`` the client was
built with). A request to a provider host that carries a sandbox credential (``sbx_`` at a token
boundary in the URL, a header or the body) is answered in-process by the fake app registered
for that host; it is never sent. Everything else is passed to the real ``send`` untouched.

A provider host with no registered app is answered by ``not_implemented_app`` (generic JSON
error, logged as ``sandbox_unhandled``), never by the real host. The sync ``httpx.Client`` is
guarded too: the product never calls a provider with it, so a sandbox request there raises
``SandboxLeakBlocked`` instead of going out.

    register_app([".example.com", "api.example.com"], asgi_app)   # exact host or ".suffix"
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Iterable, Optional

import httpx

from sandbox.credentials import has_marker

logger = logging.getLogger(__name__)

# Hosts (exact, or ".suffix" for the domain and all its subdomains) that belong to a provider.
# A sandbox request to any of them is answered in-process, registered app or not.
PROVIDER_HOSTS: dict[str, str] = {
    ".facebook.com": "meta",        # graph.facebook.com, www.facebook.com (OAuth dialog), graph-video
    ".fbcdn.net": "meta",           # creative media
    ".leadconnectorhq.com": "ghl",  # services.leadconnectorhq.com
    ".gohighlevel.com": "ghl",      # marketplace.gohighlevel.com (OAuth dialog)
    ".msgsndr.com": "ghl",          # GHL's media / recording host
    ".hookscall.com": "hp",         # service.hookscall.com (Custom API v2)
    ".hotprospector.com": "hp",     # app.hotprospector.com (legacy v1)
}

_registry: dict[str, Callable] = {}
_provider_apps: dict[str, Callable] = {}   # provider -> its fake, for hosts configured through env
_clients: dict[int, httpx.AsyncClient] = {}
_installed = {"done": False}
_orig_async_send = httpx.AsyncClient.send
_orig_sync_send = httpx.Client.send


class SandboxLeakBlocked(RuntimeError):
    """A sandbox credential was about to leave the process."""


def _match(host: str, table) -> Optional[str]:
    host = (host or "").lower().rstrip(".")
    if host in table:
        return host
    for key in table:
        if key.startswith(".") and (host == key[1:] or host.endswith(key)):
            return key
    return None


def configured_hosts() -> dict[str, str]:
    """Provider hosts that come from configuration rather than the table above: wherever the
    HOTPROSPECTOR_*_URL overrides point (sandbox.hp_app.configured_hosts). Read on every call, so
    an override set after start-up is covered too."""
    if not _installed["done"]:
        return {}
    from sandbox import hp_app

    return {host: "hp" for host in hp_app.configured_hosts()}


def provider_of(host: str) -> Optional[str]:
    """"meta" / "ghl" / "hp" for a provider host, else None."""
    key = _match(host, PROVIDER_HOSTS)
    if key:
        return PROVIDER_HOSTS[key]
    return configured_hosts().get((host or "").lower().rstrip("."))


def register_app(hosts: Iterable[str], asgi_app: Callable) -> None:
    """Answer sandbox requests to ``hosts`` (exact names, or ".suffix") with ``asgi_app``.
    Registering a host again replaces its app. A host does not have to be a provider host,
    but only provider hosts are ever routed."""
    for host in hosts:
        _registry[host.lower()] = asgi_app


def unregister_app(hosts: Iterable[str]) -> None:
    for host in hosts:
        _registry.pop(host.lower(), None)


def app_for(host: str) -> Callable:
    """The app that answers sandbox requests to ``host``: the registered one, else the default."""
    exact = (host or "").lower()
    if exact in _registry:
        return _registry[exact]
    key = _match(host, {k: v for k, v in _registry.items() if k.startswith(".")})
    if key:
        return _registry[key]
    provider = configured_hosts().get(exact.rstrip("."))
    return _provider_apps.get(provider, not_implemented_app)


async def not_implemented_app(scope, receive, send):
    """Answers a provider host nobody registered: generic JSON error, logged ``sandbox_unhandled``."""
    if scope["type"] != "http":
        return
    method, path = scope.get("method", "?"), scope.get("path", "")
    host = dict(scope.get("headers") or []).get(b"host", b"").decode()
    logger.warning("sandbox_unhandled method=%s host=%s path=%s", method, host, path)
    message = f"sandbox: {method} {path} is not implemented"
    body = json.dumps({"error": {"message": message, "type": "sandbox_not_implemented", "code": 501}}).encode()
    await send({"type": "http.response.start", "status": 501,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def _chunks(request: httpx.Request) -> list[bytes]:
    chunks = [str(request.url).encode()]
    chunks.extend(value for _, value in request.headers.raw)
    if hasattr(request, "_content"):
        chunks.append(request.content)
    return chunks


async def sandbox_app_for_request(request: httpx.Request) -> Optional[Callable]:
    """The fake app that must answer this request, or None when it may go out for real."""
    if not provider_of(request.url.host):
        return None
    if not hasattr(request, "_content"):
        await request.aread()  # a streamed body: buffer it so it can be checked (and still sent)
    return app_for(request.url.host) if has_marker(_chunks(request)) else None


def _client_for(app: Callable) -> httpx.AsyncClient:
    client = _clients.get(id(app))
    if client is None:
        # raise_app_exceptions=False: a fake that crashes answers 500, like a provider would.
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                   trust_env=False)
        _clients[id(app)] = client
    return client


async def _sandbox_send(self, request, *, stream=False, auth=httpx.USE_CLIENT_DEFAULT,
                        follow_redirects=httpx.USE_CLIENT_DEFAULT):
    app = await sandbox_app_for_request(request)
    if app is None:
        return await _orig_async_send(self, request, stream=stream, auth=auth, follow_redirects=follow_redirects)
    logger.debug("sandbox_routed method=%s url=%s", request.method, request.url.copy_with(query=None))
    return await _orig_async_send(_client_for(app), request, stream=stream, follow_redirects=False)


def _guarded_sync_send(self, request, *args, **kwargs):
    if provider_of(request.url.host):
        if not hasattr(request, "_content"):
            request.read()
        if has_marker(_chunks(request)):
            logger.error("sandbox_blocked_sync method=%s url=%s", request.method, request.url.copy_with(query=None))
            raise SandboxLeakBlocked(
                f"sandbox: {request.method} {request.url.host}{request.url.path} from a sync httpx.Client; "
                "sandbox requests are only answered for httpx.AsyncClient")
    return _orig_sync_send(self, request, *args, **kwargs)


def _register_fakes() -> None:
    # Imported here, not at module top: each fake imports sandbox.fakes, which imports this.
    from sandbox import ghl_app, hp_app, meta_app

    register_app(meta_app.HOSTS, meta_app.app)
    register_app(ghl_app.HOSTS, ghl_app.app)
    register_app(hp_app.HOSTS, hp_app.app)
    _provider_apps.update(meta=meta_app.app, ghl=ghl_app.app, hp=hp_app.app)


def install() -> None:
    """Wrap httpx's send methods and register the fakes. Idempotent."""
    if _installed["done"]:
        return
    _register_fakes()
    httpx.AsyncClient.send = _sandbox_send
    httpx.Client.send = _guarded_sync_send
    _installed["done"] = True
    logger.info("sandbox routing installed")


def uninstall() -> None:
    """Restore httpx's own send methods (tests)."""
    httpx.AsyncClient.send = _orig_async_send
    httpx.Client.send = _orig_sync_send
    _installed["done"] = False


def installed() -> bool:
    return _installed["done"] and httpx.AsyncClient.send is _sandbox_send
