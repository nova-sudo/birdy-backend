"""
End to end: an owner adds Birdy to Claude with one link.

Drives the real root app (main.app): discovery metadata at the root, DCR,
/mcp/authorize -> Birdy's consent page, the consent POST as a logged-in owner,
code exchange, tool calls, refresh, and revoking the connection from Birdy.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.routing import Mount

from core.config import API_PUBLIC_URL, FRONTEND_URL
from dependencies import generate_tokens

ALICE = "alice@example.com"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_URL = f"{API_PUBLIC_URL}/mcp"


@pytest.fixture
def root_app(mock_db, monkeypatch):
    """main.app with a fresh instance of the same MCP sub-app mounted at /mcp: the
    streamable-HTTP session manager can only run its lifespan once per instance."""
    import main
    from ai.mcp import mcp

    fresh = mcp.http_app(path="/", stateless_http=True)
    mount = next(r for r in main.app.router.routes if isinstance(r, Mount) and r.path == "/mcp")
    monkeypatch.setattr(mount, "app", fresh)
    monkeypatch.setattr(mount, "_base_app", fresh, raising=False)
    return main.app, fresh


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _http(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=API_PUBLIC_URL, **kw)


async def _call_tool(app, token, name, args):
    def factory(headers=None, timeout=None, auth=None, **kwargs):
        return _http(app, headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                     timeout=timeout, **kwargs)

    transport = StreamableHttpTransport(url=f"{MCP_URL}/", httpx_client_factory=factory)
    async with Client(transport) as client:
        res = await client.call_tool(name, args, raise_on_error=False)
    return res.is_error, "".join(getattr(c, "text", "") for c in res.content)


async def _initialize_status(http, token):
    return await http.post(
        "/mcp/",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "0"}}},
    )


@pytest.mark.asyncio
async def test_full_connect_flow(root_app, mock_db):
    app, mcp_sub_app = root_app
    await mock_db["users"].insert_one({"user_id": ALICE, "subscription": {"status": "active", "plan_id": "growth"}})

    async with mcp_sub_app.router.lifespan_context(mcp_sub_app), _http(app) as http:
        # 1. discovery, at the root
        pr = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert pr.status_code == 200
        pr = pr.json()
        assert pr["resource"] == MCP_URL
        assert pr["authorization_servers"] == [API_PUBLIC_URL] or pr["authorization_servers"] == [f"{API_PUBLIC_URL}/"]

        asm = await http.get("/.well-known/oauth-authorization-server")
        assert asm.status_code == 200
        asm = asm.json()
        assert asm["issuer"].rstrip("/") == API_PUBLIC_URL
        assert asm["authorization_endpoint"].endswith("/mcp/authorize")
        assert asm["authorization_endpoint"] == f"{MCP_URL}/authorize"
        assert asm["token_endpoint"] == f"{MCP_URL}/token"
        assert asm["registration_endpoint"] == f"{MCP_URL}/register"
        assert asm["revocation_endpoint"] == f"{MCP_URL}/revoke"
        assert "none" in asm["token_endpoint_auth_methods_supported"]
        assert "client_secret_basic" not in asm["token_endpoint_auth_methods_supported"]

        # an unauthenticated call to /mcp points the client at the resource metadata
        unauth = await _initialize_status(http, "not-a-token")
        assert unauth.status_code == 401
        assert (f'resource_metadata="{API_PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"'
                in unauth.headers["www-authenticate"])

        # 2. dynamic client registration (public client, like Claude)
        reg = await http.post("/mcp/register", json={
            "client_name": "Claude", "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        # 3. authorize with PKCE -> Birdy's consent page
        verifier, challenge = _pkce()
        auth = await http.get("/mcp/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
            "scope": "birdy.read birdy.write", "resource": MCP_URL,
        })
        assert auth.status_code == 302, auth.text
        location = auth.headers["location"]
        assert location.startswith(f"{FRONTEND_URL}/connect/authorize?request=")
        request_id = parse_qs(urlparse(location).query)["request"][0]

        # 4. the logged-in owner approves, view-only
        session, _ = await generate_tokens(ALICE)
        decision = await http.post(
            f"/api/oauth/requests/{request_id}/decision",
            json={"allow": True, "access": "read"},
            cookies={"auth_token": session},
        )
        assert decision.status_code == 200, decision.text
        back = urlparse(decision.json()["redirect_to"])
        assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT
        q = parse_qs(back.query)
        assert q["state"] == ["st-1"]
        code = q["code"][0]

        # 5. code -> tokens
        tok = await http.post("/mcp/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        })
        assert tok.status_code == 200, tok.text
        tok = tok.json()
        access, refresh = tok["access_token"], tok["refresh_token"]
        assert tok["scope"] == "birdy.read"

        # 6. read works, write is refused (view-only)
        is_error, text = await _call_tool(app, access, "get_alerts", {})
        assert not is_error, text
        is_error, text = await _call_tool(app, access, "create_alert", {
            "name": "CPL", "metric": "cpl", "operator": ">", "value": 50})
        assert is_error and "view-only" in text
        assert await mock_db["alerts"].count_documents({}) == 0

        # 7. refresh (rotates)
        ref = await http.post("/mcp/token", data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        assert ref.status_code == 200, ref.text
        assert ref.json()["refresh_token"] != refresh

        # 8. the owner disconnects it in Birdy
        conns = (await http.get("/api/mcp/connections", cookies={"auth_token": session})).json()
        grant = next(c for c in conns if c["kind"] == "oauth")
        assert grant["name"] == "Claude" and grant["access"] == "read"
        gone = await http.delete(f"/api/mcp/connections/{grant['id']}", cookies={"auth_token": session})
        assert gone.status_code == 200, gone.text

        # 9. the still-unexpired access token stops working on the very next call
        after = await _initialize_status(http, access)
        assert after.status_code == 401
        new_access = ref.json()["access_token"]
        assert (await _initialize_status(http, new_access)).status_code == 401


@pytest.mark.asyncio
async def test_public_client_can_revoke_without_client_secret(root_app, mock_db):
    app, mcp_sub_app = root_app
    from services import oauth_store
    from ai.mcp import mcp

    async with mcp_sub_app.router.lifespan_context(mcp_sub_app), _http(app) as http:
        reg = await http.post("/mcp/register", json={
            "client_name": "Claude", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })
        client_id = reg.json()["client_id"]
        verifier, challenge = _pkce()
        auth = await http.get("/mcp/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "s",
            "scope": "birdy.read",
        })
        request_id = parse_qs(urlparse(auth.headers["location"]).query)["request"][0]
        redirect_to = await oauth_store.approve_request(mock_db, request_id, ALICE, "read")
        code = parse_qs(urlparse(redirect_to).query)["code"][0]
        tok = (await http.post("/mcp/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier,
        })).json()
        assert await mcp.auth.verify_token(tok["access_token"]) is not None

        # no client_secret field at all, as a public client sends it
        rv = await http.post("/mcp/revoke", data={
            "token": tok["refresh_token"], "token_type_hint": "refresh_token", "client_id": client_id})
        assert rv.status_code == 200, rv.text
        assert await mcp.auth.verify_token(tok["access_token"]) is None


@pytest.mark.asyncio
async def test_confidential_client_still_needs_its_secret_to_revoke(root_app, mock_db):
    app, mcp_sub_app = root_app
    async with mcp_sub_app.router.lifespan_context(mcp_sub_app), _http(app) as http:
        reg = (await http.post("/mcp/register", json={
            "client_name": "Other", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })).json()
        rv = await http.post("/mcp/revoke", data={"token": "x", "client_id": reg["client_id"]})
        assert rv.status_code == 401


def test_server_identity():
    from ai.mcp import mcp
    from ai.mcp.oauth_provider import BirdyOAuthProvider

    assert mcp.name == "Birdy"
    assert isinstance(mcp.auth, BirdyOAuthProvider)
    assert mcp.instructions.startswith("You are working with Birdy, the marketing analytics and media buying assistant")
    assert "senior media buyer" in mcp.instructions and '"Act · 3"' in mcp.instructions
    assert f"{FRONTEND_URL}/clients/<client id>" in mcp.instructions and "☐ Today" in mcp.instructions
    assert mcp.website_url == FRONTEND_URL
    assert [(i.src, i.mimeType) for i in mcp.icons] == [(f"{API_PUBLIC_URL}/birdy-mcp-icon.png", "image/png")]


@pytest.mark.asyncio
async def test_bare_connector_link_is_served_without_a_redirect(root_app):
    """Claude's connector check does not follow redirects: /mcp must answer itself (401 + challenge)."""
    app, fresh = root_app
    async with fresh.router.lifespan_context(fresh):
        async with _http(app) as http:
            r = await http.post("/mcp", json={}, headers={"accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers.get("www-authenticate", "")
