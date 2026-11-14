"""
services/mcp_token_service.py
-------------------------------
Long-lived bearer tokens for external MCP clients (Claude Desktop, Claude
Code, etc.) connecting to /mcp. Distinct from the normal `auth_token` login
cookie and from the orchestrator's own 60-second internal token
(ai/mcp_client.py): those are `type: "access"` and are never checked against
this collection. Tokens minted here are `type: "mcp"` and carry a `jti` that
is looked up on every MCP request (see ai/mcp/server.py's verifier), so a
leaked or unwanted token can be revoked without rotating JWT_SECRET and
invalidating every user's session.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta

import jwt as pyjwt

from core.config import JWT_SECRET, JWT_ALGORITHM
from core.database import DB_NAME

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_DAYS = 365
LAST_USED_THROTTLE = timedelta(minutes=10)


async def create_mcp_tokens_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db["mcp_tokens"].create_index("jti", unique=True)
    await db["mcp_tokens"].create_index("user_id")


async def create_mcp_token(db, user_id: str, name: str, expiry_days: int | None = None, access: str = "write") -> dict:
    """Mint a new long-lived MCP bearer token and record it for revocation."""
    days = expiry_days if expiry_days and expiry_days > 0 else DEFAULT_EXPIRY_DAYS
    jti = uuid.uuid4().hex
    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

    # time.time() (not datetime.utcnow().timestamp()) — the latter treats the
    # naive UTC value as local time, silently shifting `exp` by the host's UTC
    # offset on any non-UTC machine.
    exp_epoch = int(time.time()) + days * 86400
    # Lazy: importing ai.mcp at module load is circular (ai.mcp.server imports this module).
    from ai.mcp.access import READ_SCOPE, WRITE_SCOPE

    scope = READ_SCOPE if access == "read" else f"{READ_SCOPE} {WRITE_SCOPE}"
    token = pyjwt.encode(
        {"sub": user_id, "jti": jti, "exp": exp_epoch, "type": "mcp", "scope": scope},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    doc = {
        "id": jti,
        "user_id": user_id,
        "name": name,
        "jti": jti,
        "created_at": now,
        "expires_at": expires_at,
        "revoked": False,
        "revoked_at": None,
        "access": "read" if access == "read" else "write",
        "kind": "manual",
        "last_used_at": None,
    }
    await db["mcp_tokens"].insert_one(doc)

    return {
        "id": jti,
        "name": name,
        "token": token,
        "created_at": now,
        "expires_at": expires_at,
    }


async def list_mcp_tokens(db, user_id: str) -> list[dict]:
    """List a user's issued MCP tokens (never the raw secret — that's shown once at creation)."""
    docs = await db["mcp_tokens"].find({"user_id": user_id, "kind": {"$ne": "oauth"}}).sort("created_at", -1).to_list(None)
    return [
        {
            "id": d["id"],
            "name": d["name"],
            "created_at": d["created_at"],
            "expires_at": d["expires_at"],
            "revoked": d.get("revoked", False),
        }
        for d in docs
    ]


async def revoke_mcp_token(db, user_id: str, token_id: str) -> bool:
    """Revoke a token by id. Returns False if it doesn't exist or isn't owned by user_id."""
    result = await db["mcp_tokens"].update_one(
        {"id": token_id, "user_id": user_id},
        {"$set": {"revoked": True, "revoked_at": datetime.utcnow()}},
    )
    return result.matched_count > 0


def _is_fresh(doc: dict | None, cutoff: datetime) -> bool:
    last = (doc or {}).get("last_used_at")
    return bool(last and last >= cutoff)


async def _touch_last_used(db, doc: dict, grant: dict | None = None) -> None:
    """Best-effort: record use at most once per LAST_USED_THROTTLE for the token and its grant.
    Telemetry only, so a failed write must never fail the request."""
    now = datetime.utcnow()
    cutoff = now - LAST_USED_THROTTLE
    stale = {"$or": [{"last_used_at": None}, {"last_used_at": {"$lt": cutoff}}]}
    try:
        if not _is_fresh(doc, cutoff):
            await db["mcp_tokens"].update_one({"jti": doc["jti"], **stale}, {"$set": {"last_used_at": now}})
        if doc.get("grant_id") and not _is_fresh(grant, cutoff):
            await db["oauth_grants"].update_one({"id": doc["grant_id"], **stale}, {"$set": {"last_used_at": now}})
    except Exception as exc:
        logger.warning("MCP last_used_at update failed: %s", exc)


async def is_mcp_token_valid(db, jti: str) -> bool:
    """Belt-and-suspenders check used by the MCP auth verifier: the JWT's own
    `exp` is already checked by JWTVerifier before this runs; this additionally
    confirms the token hasn't been revoked (directly, or via its OAuth grant)
    and its DB record still exists. Also stamps `last_used_at` (throttled).
    """
    doc = await db["mcp_tokens"].find_one({"jti": jti})
    if not doc or doc.get("revoked"):
        return False
    grant = None
    if doc.get("grant_id"):
        grant = await db["oauth_grants"].find_one({"id": doc["grant_id"]})
        if not grant or grant.get("revoked"):
            return False
    await _touch_last_used(db, doc, grant)
    return True


async def list_connections(db, user_id: str) -> list[dict]:
    """Active manual tokens and OAuth grants for this user, newest first."""
    rows = []
    manual = await db["mcp_tokens"].find(
        {"user_id": user_id, "kind": {"$ne": "oauth"}, "revoked": {"$ne": True}}
    ).to_list(None)
    for d in manual:
        rows.append({
            "id": d["id"], "kind": "manual", "name": d["name"],
            "access": d.get("access", "write"),
            "created_at": d["created_at"], "last_used_at": d.get("last_used_at"),
            "expires_at": d.get("expires_at"),
        })
    grants = await db["oauth_grants"].find({"user_id": user_id, "revoked": {"$ne": True}}).to_list(None)
    for g in grants:
        rows.append({
            "id": g["id"], "kind": "oauth", "name": g["client_name"], "access": g["access"],
            "created_at": g["created_at"], "last_used_at": g.get("last_used_at"), "expires_at": None,
        })
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


async def revoke_connection(db, user_id: str, connection_id: str) -> bool:
    """Revoke a manual token or an OAuth grant (and all its tokens) owned by user_id."""
    now = datetime.utcnow()
    res = await db["mcp_tokens"].update_one(
        {"id": connection_id, "user_id": user_id, "kind": {"$ne": "oauth"}},
        {"$set": {"revoked": True, "revoked_at": now}},
    )
    if res.matched_count > 0:
        return True
    res = await db["oauth_grants"].update_one(
        {"id": connection_id, "user_id": user_id},
        {"$set": {"revoked": True, "revoked_at": now}},
    )
    if res.matched_count == 0:
        return False
    await db["mcp_tokens"].update_many(
        {"grant_id": connection_id, "user_id": user_id},
        {"$set": {"revoked": True, "revoked_at": now}},
    )
    return True
