"""
services/mcp_token_service.py + the revocation check wired into
ai/mcp/server.py's auth verifier. Covers the full lifecycle: mint, list
(metadata only, never the raw secret), revoke, and that revocation actually
takes effect at the verifier level immediately — without needing to rotate
JWT_SECRET, which would invalidate every user's normal session.
"""

import time

import jwt as pyjwt
import pytest

from core.config import JWT_SECRET, JWT_ALGORITHM
from services import mcp_token_service


@pytest.mark.asyncio
async def test_create_mcp_token_mints_a_decodable_type_mcp_jwt(mock_db):
    result = await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "My Laptop")

    claims = pyjwt.decode(result["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert claims["sub"] == "alice@example.com"
    assert claims["type"] == "mcp"
    assert claims["jti"] == result["id"]
    assert claims["exp"] > time.time()


@pytest.mark.asyncio
async def test_list_mcp_tokens_never_exposes_the_raw_token(mock_db):
    await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "My Laptop")

    tokens = await mcp_token_service.list_mcp_tokens(mock_db, "alice@example.com")
    assert len(tokens) == 1
    assert tokens[0]["name"] == "My Laptop"
    assert tokens[0]["revoked"] is False
    assert "token" not in tokens[0]


@pytest.mark.asyncio
async def test_list_mcp_tokens_scoped_per_user(mock_db):
    await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "Alice's token")
    await mcp_token_service.create_mcp_token(mock_db, "bob@example.com", "Bob's token")

    alice_tokens = await mcp_token_service.list_mcp_tokens(mock_db, "alice@example.com")
    assert len(alice_tokens) == 1
    assert alice_tokens[0]["name"] == "Alice's token"


@pytest.mark.asyncio
async def test_revoke_mcp_token_rejects_other_users_token_id(mock_db):
    created = await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "Alice's token")

    revoked = await mcp_token_service.revoke_mcp_token(mock_db, "bob@example.com", created["id"])
    assert revoked is False  # bob doesn't own alice's token

    still_valid = await mcp_token_service.is_mcp_token_valid(mock_db, created["id"])
    assert still_valid is True


@pytest.mark.asyncio
async def test_revoke_mcp_token_by_owner_takes_effect(mock_db):
    created = await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "Alice's token")
    assert await mcp_token_service.is_mcp_token_valid(mock_db, created["id"]) is True

    revoked = await mcp_token_service.revoke_mcp_token(mock_db, "alice@example.com", created["id"])
    assert revoked is True
    assert await mcp_token_service.is_mcp_token_valid(mock_db, created["id"]) is False


@pytest.mark.asyncio
async def test_is_mcp_token_valid_false_for_unknown_jti(mock_db):
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "never-issued") is False


# ── Verifier-level integration: the same check the real MCP auth path runs ──

@pytest.mark.asyncio
async def test_verifier_accepts_valid_mcp_token_and_rejects_after_revocation(mock_db):
    from ai.mcp.server import _RevocationAwareVerifier

    verifier = _RevocationAwareVerifier()
    created = await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "Claude Desktop")

    result = await verifier.verify_token(created["token"])
    assert result is not None
    assert result.claims["sub"] == "alice@example.com"

    await mcp_token_service.revoke_mcp_token(mock_db, "alice@example.com", created["id"])
    result_after_revoke = await verifier.verify_token(created["token"])
    assert result_after_revoke is None


@pytest.mark.asyncio
async def test_verifier_accepts_normal_access_token_without_any_db_record(mock_db):
    """type: "access" tokens (login cookie, or the orchestrator's internal
    token) must keep working even with an empty mcp_tokens collection —
    the revocation lookup only applies to type: "mcp" tokens."""
    from ai.mcp.server import _RevocationAwareVerifier

    verifier = _RevocationAwareVerifier()
    access_token = pyjwt.encode(
        {"sub": "alice@example.com", "exp": int(time.time()) + 60, "type": "access"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    result = await verifier.verify_token(access_token)
    assert result is not None
    assert result.claims["sub"] == "alice@example.com"


@pytest.mark.asyncio
async def test_verifier_rejects_mcp_token_with_no_matching_record(mock_db):
    """A syntactically valid, correctly-signed type: "mcp" token whose jti was
    never issued (or whose record was deleted) must be rejected."""
    from ai.mcp.server import _RevocationAwareVerifier

    verifier = _RevocationAwareVerifier()
    forged = pyjwt.encode(
        {"sub": "alice@example.com", "jti": "not-a-real-jti", "exp": int(time.time()) + 60, "type": "mcp"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    result = await verifier.verify_token(forged)
    assert result is None
