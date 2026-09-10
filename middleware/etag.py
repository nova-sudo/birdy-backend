"""
ETags for the read-heavy rollup endpoints, so an unchanged answer costs a
round trip instead of a payload.

The dashboard's cache (SWR, five-minute freshness) already stops these being
re-fetched while you move around the app. What it can't help with is the
revalidation after that window, or a full page reload, which drops the
in-memory cache entirely — both of which re-download the whole body to
discover it is the same body. On the measured account that is 0.68MB of
figures plus 0.90MB of history.

── On the Cache-Control chosen here ────────────────────────────────────────

`private, no-cache` — which does not mean "don't cache". It means "you may
store this, but revalidate before reusing it", so the browser keeps the body
and asks with If-None-Match; a 304 lets it serve what it already has.

Deliberately NOT stale-while-revalidate, which was the original plan. These
responses are per-user and mutable, and several places in the app refetch
immediately after a write to show the result — save a view, top up credits,
add a client. Under stale-while-revalidate the browser is entitled to answer
that refetch from its stored copy, and the user would be looking at the state
from before their own change. The saving being chased here is the body, not
the round trip, and no-cache gives all of that with none of the staleness.

`private` keeps it out of shared caches, and Vary: Cookie keeps any
intermediary from serving one account's rollup to another.
"""

import hashlib

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

# Only the big, frequently-revalidated reads. Everything else — writes,
# webhooks, the tracker edge, the MCP surface — is left alone; hashing a body
# to save nothing is just work.
CACHEABLE_PATHS = (
    "/api/client-groups",
    "/api/client-groups/daily",
    "/api/dashboard/summary",
    "/api/alerts",
    "/api/facebook-leads/series",
    "/api/user/views",
)

CACHE_CONTROL = "private, no-cache"


def _is_cacheable(request) -> bool:
    if request.method != "GET":
        return False
    path = request.url.path.rstrip("/")
    return path in CACHEABLE_PATHS


class ETagMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)

        if not _is_cacheable(request) or response.status_code != 200:
            return response

        # BaseHTTPMiddleware hands back a streaming response, so the body has
        # to be collected before it can be hashed. These endpoints already
        # build their whole answer in memory before returning it, so nothing
        # is being buffered here that wasn't already.
        body = b"".join([chunk async for chunk in response.body_iterator])

        etag = f'"{hashlib.blake2b(body, digest_size=16).hexdigest()}"'
        headers = _headers_with_etag(response, etag)

        if request.headers.get("if-none-match") == etag:
            # Nothing to send: the browser already has this exact body.
            return _raw_response(304, headers)

        return _raw_response(
            200,
            headers + [(b"content-length", str(len(body)).encode("latin-1"))],
            body,
        )


def _headers_with_etag(response, etag: str):
    """
    Rebuild the outgoing headers as a raw list, not a dict.

    dict(response.headers) would be shorter and wrong: Set-Cookie is the one
    header that legitimately appears more than once, and a token refresh sets
    both auth_token and refresh_token. Collapsing them to a dict silently
    drops one, so a request that happened to refresh its tokens would log the
    user out on the next one.
    """
    dropped = {b"content-length", b"etag", b"cache-control", b"vary"}
    headers = [(k, v) for k, v in response.headers.raw if k.lower() not in dropped]

    existing_vary = [
        v.decode("latin-1") for k, v in response.headers.raw if k.lower() == b"vary"
    ]
    vary = ", ".join(dict.fromkeys(
        [part.strip() for value in existing_vary for part in value.split(",")] + ["Cookie"]
    ))

    headers.append((b"etag", etag.encode("latin-1")))
    headers.append((b"cache-control", CACHE_CONTROL.encode("latin-1")))
    headers.append((b"vary", vary.encode("latin-1")))
    return headers


def _raw_response(status_code: int, headers, body: bytes = b"") -> Response:
    response = Response(status_code=status_code)
    response.body = body
    response.raw_headers = headers
    return response
