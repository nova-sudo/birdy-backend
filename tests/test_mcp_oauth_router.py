"""Consent endpoints behind /connect/authorize."""

import contextlib
from urllib.parse import urlparse, parse_qs

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import routers.mcp_oauth as oauth_router
from dependencies import get_current_user, get_current_claims
from main import app
from services import oauth_store

ALICE = "alice@example.com"


@pytest.fixture
def api(mock_mongo_client, mock_db, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def sub_ok(user, client):
        return {"status": "active"}

    monkeypatch.setattr(oauth_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(oauth_router, "require_active_subscription", sub_ok)
    state = {"claims": {"sub": ALICE}, "authed": True, "sub_ok": sub_ok}

    def user():
        if not state["authed"]:
            raise HTTPException(status_code=401, detail="No authentication token provided")
        return ALICE

    def claims():
        if not state["authed"]:
            raise HTTPException(status_code=401, detail="No authentication token provided")
        return state["claims"]

    app.dependency_overrides[get_current_user] = user
    app.dependency_overrides[get_current_claims] = claims
    client = TestClient(app)
    client.state = state
    client.db = mock_db
    yield client
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_claims, None)


async def make_request(db, scopes=("birdy.write",)):
    return await oauth_store.create_request(
        db, client_id="c1", client_name="Claude", scopes=list(scopes), state="xyz",
        code_challenge="ch", redirect_uri="https://claude.ai/api/mcp/auth_callback",
        redirect_uri_provided_explicitly=True, resource=None,
    )


@pytest.mark.asyncio
async def test_get_request_view_requires_login(api):
    rid = await make_request(api.db)
    api.state["authed"] = False
    assert api.get(f"/api/oauth/requests/{rid}").status_code == 401
    api.state["authed"] = True
    r = api.get(f"/api/oauth/requests/{rid}")
    assert r.status_code == 200
    assert r.json() == {"client_name": "Claude", "redirect_host": "claude.ai", "requested_access": "write"}


@pytest.mark.asyncio
async def test_allow_returns_redirect_with_code_and_state(api):
    rid = await make_request(api.db)
    r = api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": True, "access": "read"})
    assert r.status_code == 200
    url = urlparse(r.json()["redirect_to"])
    q = parse_qs(url.query)
    assert url.netloc == "claude.ai" and q["state"] == ["xyz"] and q["code"][0]
    # single use
    assert api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": True, "access": "read"}).status_code == 404


@pytest.mark.asyncio
async def test_deny_returns_access_denied_redirect(api, monkeypatch):
    async def no_sub(user, client):
        raise HTTPException(status_code=402, detail="nope")

    monkeypatch.setattr(oauth_router, "require_active_subscription", no_sub)  # deny needs no subscription
    rid = await make_request(api.db)
    r = api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": False, "access": "read"})
    assert r.status_code == 200
    q = parse_qs(urlparse(r.json()["redirect_to"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["xyz"]


@pytest.mark.asyncio
async def test_impersonation_cannot_approve(api):
    rid = await make_request(api.db)
    api.state["claims"] = {"sub": ALICE, "imp": True, "act": "admin@example.com"}
    r = api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": True, "access": "read"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Admins can't connect AI tools while viewing as a user."
    assert await oauth_store.get_request_view(api.db, rid) is not None  # untouched


@pytest.mark.asyncio
async def test_no_subscription_cannot_approve(api, monkeypatch):
    async def no_sub(user, client):
        raise HTTPException(status_code=402, detail={"code": "NO_ACTIVE_SUBSCRIPTION"})

    monkeypatch.setattr(oauth_router, "require_active_subscription", no_sub)
    rid = await make_request(api.db)
    r = api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": True, "access": "read"})
    assert r.status_code == 402
    assert await oauth_store.get_request_view(api.db, rid) is not None  # not consumed


@pytest.mark.asyncio
async def test_expired_request_404(api):
    from datetime import datetime, timedelta
    rid = await make_request(api.db)
    await api.db["oauth_requests"].update_one({"id": rid}, {"$set": {"expires_at": datetime.utcnow() - timedelta(minutes=1)}})
    assert api.get(f"/api/oauth/requests/{rid}").status_code == 404
    assert api.post(f"/api/oauth/requests/{rid}/decision", json={"allow": True, "access": "read"}).status_code == 404
    assert api.get("/api/oauth/requests/nope").status_code == 404
