"""
C1: the REST API's cookie auth (dependencies.get_current_user / get_current_claims)
accepts only browser session tokens (`type: "access"`). MCP manual tokens, OAuth
access tokens (both `type: "mcp"`) and refresh-shaped tokens share JWT_SECRET but
must not work as a session cookie — otherwise a read-only or revoked connection
is a full REST session.
"""

import time

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from core.config import JWT_SECRET, JWT_ALGORITHM
from dependencies import get_current_claims
from main import app
from services import mcp_token_service

ALICE = "alice@example.com"


def _session_token(**extra):
    return pyjwt.encode({"sub": ALICE, "exp": int(time.time()) + 600, "type": "access", **extra},
                        JWT_SECRET, algorithm=JWT_ALGORITHM)


def _get_tokens(token):
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client.get("/api/mcp/tokens")


def test_login_session_token_still_works(mock_mongo_client):
    assert _get_tokens(_session_token()).status_code == 200


@pytest.mark.asyncio
async def test_manual_mcp_token_rejected_on_rest(mock_db):
    minted = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Laptop", access="read")
    assert _get_tokens(minted["token"]).status_code == 401


@pytest.mark.asyncio
async def test_oauth_access_token_rejected_on_rest(mock_db):
    minted = await mcp_token_service.create_oauth_access_token(
        mock_db, user_id=ALICE, grant_id="g1", client_id="c1", client_name="Claude",
        access="write", lifetime_seconds=3600,
    )
    assert _get_tokens(minted["token"]).status_code == 401


@pytest.mark.parametrize("type_", ["refresh", None])
def test_refresh_or_untyped_token_rejected_on_rest(mock_mongo_client, type_):
    claims = {"sub": ALICE, "exp": int(time.time()) + 600}
    if type_:
        claims["type"] = type_
    token = pyjwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert _get_tokens(token).status_code == 401


class _Req:
    def __init__(self, token):
        self.cookies = {"auth_token": token}


@pytest.mark.asyncio
async def test_get_current_claims_rejects_mcp_token_accepts_session(mock_db):
    minted = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Laptop")
    with pytest.raises(HTTPException) as exc:
        await get_current_claims(_Req(minted["token"]))
    assert exc.value.status_code == 401

    claims = await get_current_claims(_Req(_session_token(imp=True, act="admin@birdy.ai")))
    assert claims["sub"] == ALICE and claims["imp"] is True


@pytest.mark.asyncio
async def test_mcp_server_still_accepts_mcp_token(mock_db):
    from ai.mcp import mcp

    minted = await mcp_token_service.create_mcp_token(mock_db, ALICE, "Laptop")
    result = await mcp.auth.verify_token(minted["token"])
    assert result is not None and result.claims["sub"] == ALICE
