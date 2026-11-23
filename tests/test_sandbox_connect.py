"""
Sandbox accounts: the three connect flows end to end through the app, for a sandbox account (all
answered in-process by the fakes) and for a normal account (unchanged), plus the refusals.
"""

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from core.config import JWT_ALGORITHM, JWT_SECRET
from core.database import DB_NAME
from integrations.facebook_utils.facebook import facebook_integration
from integrations.gohighlevel import ghl_integration
from main import app
from sandbox import make_credential, parse_credential

SBX = "tester@sandbox.birdy.test"
REAL = "alice@example.com"
FB_REDIRECT = "https://api.example.test/oauth/callback/facebook"
GHL_REDIRECT = "https://api.example.test/oauth/callback"


@pytest.fixture(autouse=True)
def _redirects(monkeypatch):
    monkeypatch.setattr(facebook_integration, "redirect_uri", FB_REDIRECT)
    monkeypatch.setattr(ghl_integration, "redirect_uri", GHL_REDIRECT)


@pytest.fixture
def outgoing(monkeypatch):
    """Every request that reached httpx's real transport layer (nothing may, for a sandbox account)."""
    seen = []
    real = httpx.AsyncHTTPTransport.handle_async_request

    async def record(self, request):
        seen.append(str(request.url))
        raise AssertionError(f"left the process: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record)
    yield seen
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", real)


def _client(user):
    token = pyjwt.encode({"sub": user, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client


def _follow_auth_url(client, auth_url):
    """What the browser does: open the auth URL, which for a sandbox account is our own callback."""
    parts = urlsplit(auth_url)
    return client.get(f"{parts.path}?{parts.query}", follow_redirects=False)


async def _user(mock_db, user):
    return await mock_db["users"].find_one({"user_id": user}) or {}


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------
async def test_meta_connect_for_a_sandbox_account(mock_db, outgoing):
    client = _client(SBX)
    auth_url = client.get("/api/connect/facebook").json()["auth_url"]
    assert auth_url.startswith(FB_REDIRECT + "?")
    code = parse_qs(urlsplit(auth_url).query)["code"][0]
    assert parse_credential(code).kind == "meta_code"

    r = _follow_auth_url(client, auth_url)
    assert r.status_code == 302 and "status=success" in r.headers["location"]
    token = (await _user(mock_db, SBX))["integrations"]["facebook"]["access_token"]
    assert parse_credential(token).user_id == SBX and parse_credential(token).kind == "meta"
    assert outgoing == []


def test_meta_connect_url_unchanged_for_a_normal_account():
    r = _client(REAL).get("/api/connect/facebook")
    assert r.json()["auth_url"] == facebook_integration.generate_auth_url()
    assert r.json()["auth_url"].startswith("https://www.facebook.com/")


async def test_meta_callback_refuses_mismatched_codes(mock_db, monkeypatch):
    async def must_not_exchange(code):
        raise AssertionError("exchanged a refused code")

    monkeypatch.setattr(facebook_integration, "exchange_code_for_token", must_not_exchange)
    r = _client(SBX).get("/oauth/callback/facebook?code=AQDrealcode", follow_redirects=False)
    assert "status=error" in r.headers["location"]
    sbx_code = make_credential(SBX, "meta_code")
    r = _client(REAL).get(f"/oauth/callback/facebook?code={sbx_code}", follow_redirects=False)
    assert "status=error" in r.headers["location"]
    assert not (await _user(mock_db, SBX)).get("integrations")
    assert not (await _user(mock_db, REAL)).get("integrations")


async def test_meta_callback_refuses_a_real_token_for_a_sandbox_account(mock_db, monkeypatch):
    async def exchange(code):
        return True, {"access_token": "EAArealtoken", "expires_in": 100, "expires_at": "2030-01-01T00:00:00"}

    monkeypatch.setattr(facebook_integration, "exchange_code_for_token", exchange)
    r = _client(SBX).get(f"/oauth/callback/facebook?code={make_credential(SBX, 'meta_code')}", follow_redirects=False)
    assert "status=error" in r.headers["location"]
    assert not (await _user(mock_db, SBX)).get("integrations")


# ---------------------------------------------------------------------------
# GoHighLevel
# ---------------------------------------------------------------------------
async def test_ghl_connect_for_a_sandbox_account(mock_db, outgoing):
    client = _client(SBX)
    auth_url = client.get("/api/connect").json()["auth_url"]
    assert auth_url.startswith(GHL_REDIRECT + "?")
    r = _follow_auth_url(client, auth_url)
    assert r.status_code == 302 and "status=success" in r.headers["location"]
    agency = (await _user(mock_db, SBX))["integrations"]["gohighlevel"]["agency"]
    assert parse_credential(agency["access_token"]).kind == "ghl"
    assert parse_credential(agency["refresh_token"]).kind == "ghl_refresh"
    from sandbox.world import world_for
    assert agency["company_id"] == world_for(SBX).agency.ghl_company_id
    assert agency["userType"] == "Company"

    # The agency token mints location tokens for the world's locations, in-process too.
    location_id = world_for(SBX).clients[0].ghl_location_id
    ok, tokens = await ghl_integration.generate_location_token(agency["company_id"], location_id, agency["access_token"])
    assert ok and parse_credential(tokens["access_token"]).kind == f"ghl_location:{location_id}"
    ok, bad = await ghl_integration.generate_location_token(agency["company_id"], "notmine", agency["access_token"])
    assert not ok and bad["status_code"] == 400

    # And refreshes in-process.
    ok, fresh = await ghl_integration.refresh_agency_token(agency["refresh_token"])
    assert ok and parse_credential(fresh["access_token"]).user_id == SBX
    assert outgoing == []


def test_ghl_connect_url_unchanged_for_a_normal_account():
    r = _client(REAL).get("/api/connect")
    assert r.json()["auth_url"] == ghl_integration.generate_auth_url()
    assert r.json()["auth_url"].startswith("https://marketplace.gohighlevel.com/")


async def test_ghl_callback_refuses_mismatched_codes(mock_db, monkeypatch):
    async def must_not_exchange(code):
        raise AssertionError("exchanged a refused code")

    monkeypatch.setattr(ghl_integration, "exchange_code_for_token", must_not_exchange)
    r = _client(SBX).get("/oauth/callback?code=realcode123", follow_redirects=False)
    assert "status=error" in r.headers["location"]
    r = _client(REAL).get(f"/oauth/callback?code={make_credential(SBX, 'ghl_code')}", follow_redirects=False)
    assert "status=error" in r.headers["location"]
    assert not (await _user(mock_db, SBX)).get("integrations", {}).get("gohighlevel")
    assert not (await _user(mock_db, REAL)).get("integrations", {}).get("gohighlevel")


# ---------------------------------------------------------------------------
# Hot Prospector
# ---------------------------------------------------------------------------
async def test_hp_connect_for_a_sandbox_account_ignores_what_was_typed(mock_db, outgoing):
    r = _client(SBX).post("/api/hotprospector/connect", json={"api_uid": "anything", "api_key": "at all"})
    assert r.status_code == 200, r.text
    creds = (await _user(mock_db, SBX))["integrations"]["hotprospector"]["credentials"]
    assert parse_credential(creds["api_uid"]).kind == "hp_uid"
    assert parse_credential(creds["api_key"]).kind == "hp_key"
    # The members check ran against the fake, through the v2 bearer-token flow.
    shared = await mock_db["hp_tokens"].find_one({"_id": creds["api_uid"]})
    assert parse_credential(shared["access_token"]).kind == "hp"
    from integrations.hotprospector import HotProspectorIntegration
    ok, members = await HotProspectorIntegration(creds["api_uid"], creds["api_key"]).get_member_users()
    assert ok and len(members) >= 15 and all(m["email"].endswith(".example.com") for m in members)
    assert outgoing == []


async def test_hp_connect_refuses_sandbox_credentials_for_a_normal_account(mock_db, monkeypatch):
    from integrations import hotprospector as hp

    async def must_not_call(self):
        raise AssertionError("tested refused credentials")

    monkeypatch.setattr(hp.HotProspectorIntegration, "get_member_users", must_not_call)
    uid, key = make_credential(SBX, "hp_uid", nonce=""), make_credential(SBX, "hp_key", nonce="")
    r = _client(REAL).post("/api/hotprospector/connect", json={"api_uid": uid, "api_key": key})
    assert r.status_code == 400
    assert not (await _user(mock_db, REAL)).get("integrations")


async def test_hp_connect_unchanged_for_a_normal_account(mock_db, monkeypatch):
    from integrations import hotprospector as hp
    seen = {}

    async def members(self):
        seen["creds"] = (self.api_uid, self.api_key)
        return True, []

    monkeypatch.setattr(hp.HotProspectorIntegration, "get_member_users", members)
    r = _client(REAL).post("/api/hotprospector/connect", json={"api_uid": "uid-1", "api_key": "key-1"})
    assert r.status_code == 200 and seen["creds"] == ("uid-1", "key-1")
    creds = (await _user(mock_db, REAL))["integrations"]["hotprospector"]["credentials"]
    assert (creds["api_uid"], creds["api_key"]) == ("uid-1", "key-1")
