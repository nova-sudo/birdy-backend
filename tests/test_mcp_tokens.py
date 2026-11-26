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
    from ai.mcp import mcp

    verifier = mcp.auth  # the live server's verifier (BirdyOAuthProvider)
    created = await mcp_token_service.create_mcp_token(mock_db, "alice@example.com", "Claude Desktop")

    result = await verifier.verify_token(created["token"])
    assert result is not None
    assert result.claims["sub"] == "alice@example.com"

    await mcp_token_service.revoke_mcp_token(mock_db, "alice@example.com", created["id"])
    result_after_revoke = await verifier.verify_token(created["token"])
    assert result_after_revoke is None


@pytest.mark.asyncio
async def test_verifier_accepts_internal_token_without_any_db_record(mock_db):
    """The orchestrator's type: "internal" token must keep working even with an
    empty mcp_tokens collection — the revocation lookup only applies to
    type: "mcp" tokens."""
    from ai.mcp import mcp

    verifier = mcp.auth  # the live server's verifier (BirdyOAuthProvider)
    internal_token = pyjwt.encode(
        {"sub": "alice@example.com", "exp": int(time.time()) + 60, "type": "internal"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    result = await verifier.verify_token(internal_token)
    assert result is not None
    assert result.claims["sub"] == "alice@example.com"


@pytest.mark.asyncio
async def test_verifier_refuses_login_cookie(mock_db):
    """The login cookie (type: "access") is not an MCP credential: every user holds
    their own, and accepting it skipped subscription, credits and the rate limit."""
    from ai.mcp import mcp

    login_token = pyjwt.encode(
        {"sub": "alice@example.com", "exp": int(time.time()) + 60, "type": "access"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    assert await mcp.auth.verify_token(login_token) is None


@pytest.mark.asyncio
async def test_verifier_rejects_mcp_token_with_no_matching_record(mock_db):
    """A syntactically valid, correctly-signed type: "mcp" token whose jti was
    never issued (or whose record was deleted) must be rejected."""
    from ai.mcp import mcp

    verifier = mcp.auth  # the live server's verifier (BirdyOAuthProvider)
    forged = pyjwt.encode(
        {"sub": "alice@example.com", "jti": "not-a-real-jti", "exp": int(time.time()) + 60, "type": "mcp"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    result = await verifier.verify_token(forged)
    assert result is None


# ── Access levels + connections API ─────────────────────────────────────────

import contextlib
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import routers.mcp_tokens as mcp_tokens_router
from dependencies import get_current_user
from main import app

ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(mcp_tokens_router, "get_mongo_client", fake_client)
    state = {"user": ALICE}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def seed_grant(db, gid, user, name="Claude", access="read", revoked=False):
    await db["oauth_grants"].insert_one({
        "id": gid, "user_id": user, "client_id": "c1", "client_name": name,
        "redirect_host": "claude.ai", "access": access,
        "created_at": datetime.utcnow(), "last_used_at": None, "revoked": revoked,
    })


@pytest.mark.asyncio
async def test_read_token_carries_read_scope_only(mock_db):
    r = await mcp_token_service.create_mcp_token(mock_db, ALICE, "ro", access="read")
    w = await mcp_token_service.create_mcp_token(mock_db, ALICE, "rw")
    rc = pyjwt.decode(r["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    wc = pyjwt.decode(w["token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    assert rc["scope"] == "birdy.read"
    assert wc["scope"] == "birdy.read birdy.write"
    doc = await mock_db["mcp_tokens"].find_one({"jti": r["id"]})
    assert doc["access"] == "read" and doc["kind"] == "manual"


def test_connections_lists_manual_and_oauth_for_caller_only(api, mock_db):
    async def seed():
        await mcp_token_service.create_mcp_token(mock_db, ALICE, "Laptop", access="read")
        revoked = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Old")
        await mcp_token_service.revoke_mcp_token(mock_db, ALICE, revoked["id"])
        await mcp_token_service.create_mcp_token(mock_db, BOB, "Bob's")
        await seed_grant(mock_db, "g1", ALICE, name="Claude", access="write")
        await seed_grant(mock_db, "g2", ALICE, name="Gone", revoked=True)
        await seed_grant(mock_db, "g3", BOB, name="Bob app")
        # oauth access token docs must not show up as manual rows
        await mock_db["mcp_tokens"].insert_one({
            "id": "t-oauth", "jti": "t-oauth", "user_id": ALICE, "name": "x", "kind": "oauth",
            "grant_id": "g1", "created_at": datetime.utcnow(), "expires_at": datetime.utcnow(),
            "revoked": False,
        })

    import anyio
    anyio.run(seed)

    rows = api.get("/api/mcp/connections").json()
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"Laptop", "Claude"}
    assert by_name["Laptop"]["kind"] == "manual" and by_name["Laptop"]["access"] == "read"
    assert by_name["Claude"]["kind"] == "oauth" and by_name["Claude"]["access"] == "write"
    assert by_name["Claude"]["expires_at"] is None
    assert set(by_name["Laptop"]) == {"id", "kind", "name", "access", "created_at", "last_used_at", "expires_at"}


def test_delete_connection_of_other_user_404(api, mock_db):
    import anyio

    async def seed():
        t = await mcp_token_service.create_mcp_token(mock_db, BOB, "Bob's")
        await seed_grant(mock_db, "g3", BOB)
        return t["id"]

    tid = anyio.run(seed)
    assert api.delete(f"/api/mcp/connections/{tid}").status_code == 404
    assert api.delete("/api/mcp/connections/g3").status_code == 404
    assert api.delete("/api/mcp/connections/nope").status_code == 404

    async def check():
        assert await mcp_token_service.is_mcp_token_valid(mock_db, tid) is True
        assert (await mock_db["oauth_grants"].find_one({"id": "g3"}))["revoked"] is False

    anyio.run(check)


def test_delete_connection_revokes_manual_and_grant_with_tokens(api, mock_db):
    import anyio

    async def seed():
        t = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Mine")
        await seed_grant(mock_db, "g1", ALICE)
        await mock_db["mcp_tokens"].insert_one({
            "id": "tt", "jti": "tt", "user_id": ALICE, "name": "x", "kind": "oauth", "grant_id": "g1",
            "created_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(hours=1),
            "revoked": False,
        })
        return t["id"]

    tid = anyio.run(seed)
    assert api.delete(f"/api/mcp/connections/{tid}").status_code == 200
    assert api.delete("/api/mcp/connections/g1").status_code == 200

    async def check():
        assert await mcp_token_service.is_mcp_token_valid(mock_db, tid) is False
        assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt") is False

    anyio.run(check)


@pytest.mark.asyncio
async def test_revoked_grant_invalidates_its_access_tokens(mock_db):
    await seed_grant(mock_db, "g1", ALICE)
    await mock_db["mcp_tokens"].insert_one({
        "id": "tt", "jti": "tt", "user_id": ALICE, "name": "x", "kind": "oauth", "grant_id": "g1",
        "created_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(hours=1),
        "revoked": False,
    })
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt") is True
    await mock_db["oauth_grants"].update_one({"id": "g1"}, {"$set": {"revoked": True}})
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt") is False


@pytest.mark.asyncio
async def test_last_used_at_is_throttled_to_once_per_ten_minutes(mock_db):
    await seed_grant(mock_db, "g1", ALICE)
    await mock_db["mcp_tokens"].insert_one({
        "id": "tt", "jti": "tt", "user_id": ALICE, "name": "x", "kind": "oauth", "grant_id": "g1",
        "created_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(hours=1),
        "revoked": False,
    })
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt")
    first = (await mock_db["mcp_tokens"].find_one({"jti": "tt"}))["last_used_at"]
    assert first is not None
    assert (await mock_db["oauth_grants"].find_one({"id": "g1"}))["last_used_at"] is not None
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt")
    assert (await mock_db["mcp_tokens"].find_one({"jti": "tt"}))["last_used_at"] == first
    stale = datetime.utcnow() - timedelta(minutes=11)
    await mock_db["mcp_tokens"].update_one({"jti": "tt"}, {"$set": {"last_used_at": stale}})
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "tt")
    assert (await mock_db["mcp_tokens"].find_one({"jti": "tt"}))["last_used_at"] > stale


@pytest.mark.asyncio
async def test_failed_last_used_write_does_not_fail_verification(mock_db, monkeypatch):
    created = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Mine")

    class Boom:
        def __getitem__(self, name):
            coll = mock_db[name]

            class C:
                find_one = staticmethod(coll.find_one)

                async def update_one(self, *a, **k):
                    raise RuntimeError("write concern timeout")

            return C()

    assert await mcp_token_service.is_mcp_token_valid(Boom(), created["id"]) is True


@pytest.mark.asyncio
async def test_legacy_token_doc_lists_and_verifies(api, mock_db):
    await mock_db["mcp_tokens"].insert_one({
        "id": "old", "user_id": ALICE, "name": "Legacy", "jti": "old",
        "created_at": datetime.utcnow(), "expires_at": datetime.utcnow() + timedelta(days=5),
        "revoked": False, "revoked_at": None,
    })
    assert await mcp_token_service.is_mcp_token_valid(mock_db, "old") is True
    rows = await mcp_token_service.list_connections(mock_db, ALICE)
    assert [(r["name"], r["kind"], r["access"]) for r in rows] == [("Legacy", "manual", "write")]


def test_delete_oauth_access_token_id_404(api, mock_db):
    import anyio

    async def seed():
        await mock_db["mcp_tokens"].insert_one({
            "id": "tt", "jti": "tt", "user_id": ALICE, "name": "x", "kind": "oauth", "grant_id": "g1",
            "created_at": datetime.utcnow(), "expires_at": datetime.utcnow(), "revoked": False,
        })

    anyio.run(seed)
    assert api.delete("/api/mcp/connections/tt").status_code == 404
