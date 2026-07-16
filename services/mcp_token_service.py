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

import time
import uuid
from datetime import datetime, timedelta

import jwt as pyjwt

from core.config import JWT_SECRET, JWT_ALGORITHM
from core.database import DB_NAME

DEFAULT_EXPIRY_DAYS = 365


async def create_mcp_tokens_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db["mcp_tokens"].create_index("jti", unique=True)
    await db["mcp_tokens"].create_index("user_id")


async def create_mcp_token(db, user_id: str, name: str, expiry_days: int | None = None) -> dict:
    """Mint a new long-lived MCP bearer token and record it for revocation."""
    days = expiry_days if expiry_days and expiry_days > 0 else DEFAULT_EXPIRY_DAYS
    jti = uuid.uuid4().hex
    now = datetime.utcnow()
    expires_at = now + timedelta(days=days)

    # time.time() (not datetime.utcnow().timestamp()) — the latter treats the
    # naive UTC value as local time, silently shifting `exp` by the host's UTC
    # offset on any non-UTC machine.
    exp_epoch = int(time.time()) + days * 86400
    token = pyjwt.encode(
        {"sub": user_id, "jti": jti, "exp": exp_epoch, "type": "mcp"},
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
    docs = await db["mcp_tokens"].find({"user_id": user_id}).sort("created_at", -1).to_list(None)
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


async def is_mcp_token_valid(db, jti: str) -> bool:
    """Belt-and-suspenders check used by the MCP auth verifier: the JWT's own
    `exp` is already checked by JWTVerifier before this runs; this additionally
    confirms the token hasn't been revoked and its DB record still exists.
    """
    doc = await db["mcp_tokens"].find_one({"jti": jti})
    if not doc or doc.get("revoked"):
        return False
    return True
