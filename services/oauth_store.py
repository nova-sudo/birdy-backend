"""
services/oauth_store.py
-------------------------
Mongo persistence for Birdy's OAuth 2.1 authorization server (the provider is
ai/mcp/oauth_provider.py; the consent page calls approve/deny/get_request_view).

Collections:
- oauth_clients:        DCR client info, keyed by client_id.
- oauth_requests:       a pending /authorize request waiting on the owner's consent (10 min).
- oauth_codes:          authorization codes, single use, 5 min. Only sha256(code) is stored.
- oauth_refresh_tokens: sha256(token), grant_id, 90 days. Rotated on every use.
- oauth_grants:         one per approved connection; revoking it kills every token it issued.

Every read also filters on expires_at: TTL indexes only sweep about once a minute
(and mongomock never does), so expiry must never depend on them.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

from mcp.server.auth.provider import construct_redirect_uri

from core.database import DB_NAME
from services.mcp_token_service import revoke_connection, scope_for_access

REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=5)
REFRESH_TTL = timedelta(days=90)

WRITE_SCOPE = "birdy.write"


class OAuthRequestNotFound(LookupError):
    """The authorize request doesn't exist, expired, or was already approved/denied."""


async def create_oauth_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db["oauth_clients"].create_index("client_id", unique=True)
    await db["oauth_requests"].create_index("id", unique=True)
    await db["oauth_requests"].create_index("expires_at", expireAfterSeconds=0)
    await db["oauth_codes"].create_index("code_hash", unique=True)
    await db["oauth_codes"].create_index("expires_at", expireAfterSeconds=0)
    await db["oauth_refresh_tokens"].create_index("token_hash", unique=True)
    await db["oauth_refresh_tokens"].create_index("grant_id")
    await db["oauth_refresh_tokens"].create_index("expires_at", expireAfterSeconds=0)
    await db["oauth_grants"].create_index("id", unique=True)
    await db["oauth_grants"].create_index("user_id")
    # OAuth access tokens are minted hourly per connection; sweep them once expired.
    # Partial, so expired manual tokens keep showing in the user's token list.
    await db["mcp_tokens"].create_index(
        "expires_at", expireAfterSeconds=0, partialFilterExpression={"kind": "oauth"}, name="oauth_access_ttl"
    )


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def redirect_host(uri: str) -> str:
    """What the consent page shows as "will send you back to": the host, or the scheme for app links."""
    parsed = urlparse(uri)
    return parsed.hostname or f"{parsed.scheme}://"


def access_for_scopes(scopes: list[str] | None) -> Literal["read", "write"]:
    return "write" if WRITE_SCOPE in (scopes or []) else "read"


def scopes_for_access(access: str) -> list[str]:
    return scope_for_access(access).split()


# ── clients ─────────────────────────────────────────────────────────────────

async def save_client(db, client_info: dict) -> None:
    await db["oauth_clients"].insert_one({**client_info, "created_at": _now()})


async def load_client(db, client_id: str) -> dict | None:
    return await db["oauth_clients"].find_one({"client_id": client_id}, {"_id": 0, "created_at": 0})


# ── authorize requests (consent) ────────────────────────────────────────────

async def create_request(db, *, client_id: str, client_name: str, scopes: list[str], state: str | None,
                         code_challenge: str, redirect_uri: str, redirect_uri_provided_explicitly: bool,
                         resource: str | None) -> str:
    now = _now()
    request_id = secrets.token_urlsafe(24)
    await db["oauth_requests"].insert_one({
        "id": request_id,
        "client_id": client_id,
        "client_name": client_name,
        "scopes": scopes,
        "state": state,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
        "resource": resource,
        "created_at": now,
        "expires_at": now + REQUEST_TTL,
    })
    return request_id


async def get_request_view(db, request_id: str) -> dict | None:
    """What the consent page needs to render, or None if the request is gone/expired."""
    req = await db["oauth_requests"].find_one({"id": request_id, "expires_at": {"$gt": _now()}})
    if not req:
        return None
    return {
        "client_name": req["client_name"],
        "redirect_host": redirect_host(req["redirect_uri"]),
        "requested_access": access_for_scopes(req["scopes"]),
    }


async def _consume_request(db, request_id: str) -> dict:
    # Atomic: a request can be approved or denied exactly once.
    req = await db["oauth_requests"].find_one_and_delete({"id": request_id, "expires_at": {"$gt": _now()}})
    if not req:
        raise OAuthRequestNotFound(request_id)
    return req


async def approve_request(db, request_id: str, user_id: str, access: Literal["read", "write"]) -> str:
    """Owner said yes: create the grant and a single-use code, return the client redirect.

    The grant never exceeds what the client asked for: approving "write" on a
    read-only request yields a read grant. Raises OAuthRequestNotFound.
    """
    if access not in ("read", "write"):
        raise ValueError(f"access must be 'read' or 'write', not {access!r}")
    req = await _consume_request(db, request_id)
    if access_for_scopes(req["scopes"]) == "read":
        access = "read"
    now = _now()

    grant = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "client_id": req["client_id"],
        "client_name": req["client_name"],
        "redirect_host": redirect_host(req["redirect_uri"]),
        "access": access,
        "created_at": now,
        "last_used_at": None,
        "revoked": False,
    }
    await db["oauth_grants"].insert_one(grant)

    code = secrets.token_urlsafe(32)
    await db["oauth_codes"].insert_one({
        "code_hash": _hash(code),
        "client_id": req["client_id"],
        "grant_id": grant["id"],
        "user_id": user_id,
        "scopes": scopes_for_access(access),
        "code_challenge": req["code_challenge"],
        "redirect_uri": req["redirect_uri"],
        "redirect_uri_provided_explicitly": req["redirect_uri_provided_explicitly"],
        "resource": req.get("resource"),
        "used": False,
        "created_at": now,
        "expires_at": now + CODE_TTL,
    })
    return construct_redirect_uri(req["redirect_uri"], code=code, state=req["state"])


async def deny_request(db, request_id: str) -> str:
    """Owner said no: consume the request, return redirect_uri?error=access_denied&state=…"""
    req = await _consume_request(db, request_id)
    return construct_redirect_uri(req["redirect_uri"], error="access_denied", state=req["state"])


# ── grants ──────────────────────────────────────────────────────────────────

async def get_active_grant(db, grant_id: str) -> dict | None:
    return await db["oauth_grants"].find_one({"id": grant_id, "revoked": {"$ne": True}})


async def revoke_grant(db, grant_id: str) -> None:
    """Revoke the grant, every access token it issued, and its refresh tokens."""
    grant = await db["oauth_grants"].find_one({"id": grant_id})
    if not grant:
        return
    await revoke_connection(db, grant["user_id"], grant_id)
    await db["oauth_refresh_tokens"].delete_many({"grant_id": grant_id})


# ── authorization codes ─────────────────────────────────────────────────────

async def load_code(db, client_id: str, code: str) -> dict | None:
    return await db["oauth_codes"].find_one(
        {"code_hash": _hash(code), "client_id": client_id, "used": False, "expires_at": {"$gt": _now()}}
    )


async def consume_code(db, client_id: str, code: str) -> dict | None:
    """Atomically mark the code used; None if it was already used (or expired)."""
    return await db["oauth_codes"].find_one_and_update(
        {"code_hash": _hash(code), "client_id": client_id, "used": False, "expires_at": {"$gt": _now()}},
        {"$set": {"used": True, "used_at": _now()}},
    )


# ── refresh tokens ──────────────────────────────────────────────────────────

async def create_refresh_token(db, *, grant: dict, scopes: list[str]) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(48)
    now = _now()
    expires_at = now + REFRESH_TTL
    await db["oauth_refresh_tokens"].insert_one({
        "token_hash": _hash(token),
        "client_id": grant["client_id"],
        "grant_id": grant["id"],
        "user_id": grant["user_id"],
        "scopes": scopes,
        "created_at": now,
        "expires_at": expires_at,
    })
    return token, expires_at


async def load_refresh_token(db, client_id: str, token: str) -> dict | None:
    return await db["oauth_refresh_tokens"].find_one(
        {"token_hash": _hash(token), "client_id": client_id, "expires_at": {"$gt": _now()}}
    )


async def consume_refresh_token(db, client_id: str, token: str) -> dict | None:
    """Atomically delete the refresh token; None if it was already rotated away."""
    return await db["oauth_refresh_tokens"].find_one_and_delete(
        {"token_hash": _hash(token), "client_id": client_id, "expires_at": {"$gt": _now()}}
    )
