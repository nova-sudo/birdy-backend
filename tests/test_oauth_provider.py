"""
ai/mcp/oauth_provider.py + services/oauth_store.py: the Mongo-backed OAuth 2.1
authorization server that lets Claude/ChatGPT "Add custom connector" register
(DCR), send the owner to Birdy's consent page, and swap the code for tokens.

The /register and /token handlers are the MCP SDK's own (PKCE and redirect_uri
matching live there); these tests drive them through a throwaway Starlette app
built from provider.get_routes().
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt
import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from starlette.applications import Starlette

from core.config import FRONTEND_URL, JWT_ALGORITHM, JWT_SECRET

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def provider(mock_db):
    from ai.mcp.oauth_provider import BirdyOAuthProvider

    return BirdyOAuthProvider()


@pytest.fixture
def http(provider):
    app = Starlette(routes=provider.get_routes())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://birdy.test")


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


async def _register(provider, scope="birdy.read birdy.write", redirect=REDIRECT):
    client = OAuthClientInformationFull(
        client_id=secrets.token_hex(8),
        redirect_uris=[redirect],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        client_name="Claude",
        scope=scope,
    )
    await provider.register_client(client)
    return await provider.get_client(client.client_id)


async def _authorize(provider, client, scopes=None, state="st-1"):
    verifier, challenge = _pkce()
    params = AuthorizationParams(
        state=state,
        scopes=scopes,
        code_challenge=challenge,
        redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True,
    )
    url = await provider.authorize(client, params)
    request_id = parse_qs(urlparse(url).query)["request"][0]
    return request_id, verifier


async def _approved_code(mock_db, provider, access="write", user="alice@example.com", scopes=None):
    from services import oauth_store

    client = await _register(provider)
    request_id, verifier = await _authorize(provider, client, scopes=scopes)
    redirect = await oauth_store.approve_request(mock_db, request_id, user, access)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    return client, code, verifier


async def _token(http, client, **form):
    return await http.post("/token", data={"client_id": client.client_id, **form})


async def test_register_client_rejects_plain_http_non_localhost(http):
    base = {"grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "client_name": "X"}

    bad = await http.post("/register", json={**base, "redirect_uris": ["http://evil.example.com/cb"]})
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_redirect_uri"

    for ok_uri in ("https://claude.ai/api/mcp/auth_callback", "http://localhost:6274/cb",
                   "http://127.0.0.1:33418/cb", "cursor://anysphere.cursor-retrieval/oauth/cb",
                   "vscode://vscode.github-authentication/did-authenticate"):
        ok = await http.post("/register", json={**base, "redirect_uris": [ok_uri]})
        assert ok.status_code == 201, (ok_uri, ok.text)

    for bad_uri in ("javascript://x/%0aalert(1)", "http://localhost.evil.com/cb"):
        resp = await http.post("/register", json={**base, "redirect_uris": [bad_uri]})
        assert resp.status_code == 400, bad_uri


async def test_authorize_returns_frontend_consent_url_and_stores_request(mock_db, provider):
    from services import oauth_store

    client = await _register(provider, scope="birdy.read")
    request_id, _ = await _authorize(provider, client, scopes=["birdy.read"])

    doc = await mock_db["oauth_requests"].find_one({"id": request_id})
    assert doc["client_id"] == client.client_id
    assert doc["redirect_uri"] == REDIRECT
    assert doc["state"] == "st-1"
    assert 9 * 60 < (doc["expires_at"] - doc["created_at"]).total_seconds() <= 10 * 60

    url = await provider.authorize(client, AuthorizationParams(
        state=None, scopes=None, code_challenge="c" * 43, redirect_uri=REDIRECT,
        redirect_uri_provided_explicitly=True))
    assert url.startswith(f"{FRONTEND_URL}/connect/authorize?request=")

    view = await oauth_store.get_request_view(mock_db, request_id)
    assert view == {"client_name": "Claude", "redirect_host": "claude.ai", "requested_access": "read"}
    assert await oauth_store.get_request_view(mock_db, "nope") is None


async def test_expired_request_is_not_viewable_or_approvable(mock_db, provider):
    from datetime import datetime, timedelta
    from services import oauth_store

    client = await _register(provider)
    request_id, _ = await _authorize(provider, client)
    await mock_db["oauth_requests"].update_one(
        {"id": request_id}, {"$set": {"expires_at": datetime.utcnow() - timedelta(seconds=1)}})

    assert await oauth_store.get_request_view(mock_db, request_id) is None
    with pytest.raises(oauth_store.OAuthRequestNotFound):
        await oauth_store.approve_request(mock_db, request_id, "alice@example.com", "write")


async def test_deny_redirects_with_access_denied(mock_db, provider):
    from services import oauth_store

    client = await _register(provider)
    request_id, _ = await _authorize(provider, client)
    url = await oauth_store.deny_request(mock_db, request_id)
    q = parse_qs(urlparse(url).query)
    assert url.startswith(REDIRECT)
    assert q == {"error": ["access_denied"], "state": ["st-1"]}
    # consumed: cannot be approved afterwards
    with pytest.raises(oauth_store.OAuthRequestNotFound):
        await oauth_store.approve_request(mock_db, request_id, "alice@example.com", "write")


async def test_approve_creates_grant_and_single_use_code(mock_db, provider, http):
    from services import oauth_store

    client = await _register(provider)
    request_id, verifier = await _authorize(provider, client)
    redirect = await oauth_store.approve_request(mock_db, request_id, "alice@example.com", "read")

    q = parse_qs(urlparse(redirect).query)
    assert redirect.startswith(REDIRECT + "?")
    assert q["state"] == ["st-1"]
    code = q["code"][0]

    grant = await mock_db["oauth_grants"].find_one({"user_id": "alice@example.com"})
    assert grant["client_id"] == client.client_id
    assert grant["client_name"] == "Claude"
    assert grant["redirect_host"] == "claude.ai"
    assert grant["access"] == "read"
    assert grant["revoked"] is False
    assert grant["last_used_at"] is None

    stored = await mock_db["oauth_codes"].find_one({})
    assert code not in str(stored)  # only a hash is stored
    assert 4 * 60 < (stored["expires_at"] - stored["created_at"]).total_seconds() <= 5 * 60

    # the request is consumed
    with pytest.raises(oauth_store.OAuthRequestNotFound):
        await oauth_store.approve_request(mock_db, request_id, "alice@example.com", "read")

    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    first = await _token(http, client, **form)
    assert first.status_code == 200, first.text
    again = await _token(http, client, **form)
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


async def test_exchange_code_with_wrong_pkce_verifier_fails(mock_db, provider, http):
    client, code, verifier = await _approved_code(mock_db, provider)

    wrong = await _token(http, client, grant_type="authorization_code", code=code,
                         redirect_uri=REDIRECT, code_verifier=secrets.token_urlsafe(48))
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    assert await mock_db["mcp_tokens"].count_documents({}) == 0

    other_client = await _register(provider)
    stolen = await _token(http, other_client, grant_type="authorization_code", code=code,
                          redirect_uri=REDIRECT, code_verifier=verifier)
    assert stolen.status_code == 400


async def test_refresh_rotates_and_old_refresh_token_is_rejected(mock_db, provider, http):
    client, code, verifier = await _approved_code(mock_db, provider)
    tok = (await _token(http, client, grant_type="authorization_code", code=code,
                        redirect_uri=REDIRECT, code_verifier=verifier)).json()
    r1 = tok["refresh_token"]
    assert r1 not in str(await mock_db["oauth_refresh_tokens"].find_one({}))  # hashed at rest

    second = await _token(http, client, grant_type="refresh_token", refresh_token=r1)
    assert second.status_code == 200, second.text
    r2 = second.json()["refresh_token"]
    assert r2 != r1
    assert await provider.load_access_token(second.json()["access_token"]) is not None

    replay = await _token(http, client, grant_type="refresh_token", refresh_token=r1)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"

    third = await _token(http, client, grant_type="refresh_token", refresh_token=r2)
    assert third.status_code == 200, third.text
    assert await mock_db["oauth_refresh_tokens"].count_documents({}) == 1


async def test_refresh_token_from_another_client_or_after_revoke_fails(mock_db, provider, http):
    client, code, verifier = await _approved_code(mock_db, provider)
    tok = (await _token(http, client, grant_type="authorization_code", code=code,
                        redirect_uri=REDIRECT, code_verifier=verifier)).json()

    other = await _register(provider)
    assert (await _token(http, other, grant_type="refresh_token", refresh_token=tok["refresh_token"])).status_code == 400

    access = await provider.load_access_token(tok["access_token"])
    await provider.revoke_token(access)
    assert await provider.load_access_token(tok["access_token"]) is None
    resp = await _token(http, client, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert resp.status_code == 400
    grant = await mock_db["oauth_grants"].find_one({})
    assert grant["revoked"] is True


async def test_revoke_endpoint_revokes_whole_grant(mock_db, provider, http):
    client, code, verifier = await _approved_code(mock_db, provider)
    tok = (await _token(http, client, grant_type="authorization_code", code=code,
                        redirect_uri=REDIRECT, code_verifier=verifier)).json()

    # mcp 1.24's RevocationRequest makes client_secret a required (nullable) form field, even for public clients.
    resp = await http.post("/revoke", data={"client_id": client.client_id, "client_secret": "",
                                            "token": tok["refresh_token"], "token_type_hint": "refresh_token"})
    assert resp.status_code == 200
    assert await provider.load_access_token(tok["access_token"]) is None
    assert (await mock_db["oauth_grants"].find_one({}))["revoked"] is True


@pytest.mark.parametrize("access,expected", [("read", "birdy.read"), ("write", "birdy.read birdy.write")])
async def test_issued_access_token_scope_matches_grant_access(mock_db, provider, http, access, expected):
    client, code, verifier = await _approved_code(mock_db, provider, access=access)
    resp = await _token(http, client, grant_type="authorization_code", code=code,
                        redirect_uri=REDIRECT, code_verifier=verifier)
    body = resp.json()
    assert body["token_type"].lower() == "bearer"
    assert body["expires_in"] == 3600
    assert set(body["scope"].split()) == set(expected.split())

    claims = pyjwt.decode(body["access_token"], JWT_SECRET, algorithms=[JWT_ALGORITHM])
    grant = await mock_db["oauth_grants"].find_one({})
    assert claims["type"] == "mcp"
    assert claims["sub"] == "alice@example.com"
    assert claims["scope"] == expected
    assert claims["grant_id"] == grant["id"]
    assert claims["client_id"] == client.client_id
    assert 3500 < claims["exp"] - time.time() <= 3600

    row = await mock_db["mcp_tokens"].find_one({"jti": claims["jti"]})
    assert row["kind"] == "oauth"
    assert row["grant_id"] == grant["id"]
    assert row["user_id"] == "alice@example.com"
    assert row["name"] == "Claude"
    assert row["access"] == access
    assert row["revoked"] is False

    loaded = await provider.load_access_token(body["access_token"])
    assert loaded.claims["sub"] == "alice@example.com"
    assert set(loaded.scopes) == set(expected.split())


async def test_approving_write_for_read_only_request_stays_read(mock_db, provider, http):
    client, code, verifier = await _approved_code(mock_db, provider, access="write", scopes=["birdy.read"])
    body = (await _token(http, client, grant_type="authorization_code", code=code,
                         redirect_uri=REDIRECT, code_verifier=verifier)).json()
    assert body["scope"] == "birdy.read"
    assert (await mock_db["oauth_grants"].find_one({}))["access"] == "read"


async def test_internal_and_manual_tokens_verify_but_login_cookies_do_not(mock_db, provider):
    from services import mcp_token_service

    internal = pyjwt.encode({"sub": "alice@example.com", "type": "internal", "exp": int(time.time()) + 600},
                            JWT_SECRET, algorithm=JWT_ALGORITHM)
    loaded = await provider.load_access_token(internal)
    assert loaded is not None and loaded.claims["sub"] == "alice@example.com"

    # The login cookie, and an impersonation cookie, must not work as a bearer at /mcp:
    # they would skip subscription, credits and the rate limit.
    login = pyjwt.encode({"sub": "alice@example.com", "type": "access", "exp": int(time.time()) + 600},
                         JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert await provider.load_access_token(login) is None
    impersonation = pyjwt.encode({"sub": "alice@example.com", "type": "access", "imp": True,
                                  "act": "admin@birdy.ai", "exp": int(time.time()) + 600},
                                 JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert await provider.load_access_token(impersonation) is None
    untyped = pyjwt.encode({"sub": "alice@example.com", "exp": int(time.time()) + 600},
                           JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert await provider.load_access_token(untyped) is None

    manual = await mcp_token_service.create_mcp_token(mock_db, "bob@example.com", "Laptop", access="read")
    loaded = await provider.load_access_token(manual["token"])
    assert loaded is not None and loaded.claims["sub"] == "bob@example.com"

    await mcp_token_service.revoke_mcp_token(mock_db, "bob@example.com", manual["id"])
    assert await provider.load_access_token(manual["token"]) is None

    forged = pyjwt.encode({"sub": "alice@example.com", "type": "access", "exp": int(time.time()) + 600},
                          "not-the-secret", algorithm=JWT_ALGORITHM)
    assert await provider.load_access_token(forged) is None
    no_jti = pyjwt.encode({"sub": "alice@example.com", "type": "mcp", "exp": int(time.time()) + 600},
                          JWT_SECRET, algorithm=JWT_ALGORITHM)
    assert await provider.load_access_token(no_jti) is None


def test_provider_config():
    from ai.mcp.oauth_provider import BirdyOAuthProvider
    from core.config import API_PUBLIC_URL

    p = BirdyOAuthProvider()
    assert str(p.base_url).rstrip("/") == f"{API_PUBLIC_URL}/mcp"
    assert str(p.issuer_url).rstrip("/") == API_PUBLIC_URL
    assert p.client_registration_options.enabled is True
    assert p.client_registration_options.valid_scopes == ["birdy.read", "birdy.write"]
    assert p.client_registration_options.default_scopes == ["birdy.read", "birdy.write"]
    assert p.revocation_options.enabled is True


async def test_approve_rejects_unknown_access_level(mock_db, provider):
    from services import oauth_store

    client = await _register(provider)
    request_id, _ = await _authorize(provider, client)
    with pytest.raises(ValueError):
        await oauth_store.approve_request(mock_db, request_id, "alice@example.com", "admin")
    assert await oauth_store.get_request_view(mock_db, request_id) is not None  # not consumed


async def test_create_oauth_indexes_runs(mock_mongo_client):
    from services.oauth_store import create_oauth_indexes

    await create_oauth_indexes(mock_mongo_client)
