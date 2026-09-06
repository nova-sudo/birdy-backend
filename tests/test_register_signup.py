"""
tests/test_register_signup.py
-----------------------------
What /api/register accepts and what it hands back.

Sign-up used to require a full name and a currency before an account could
exist, and its response carried a smaller `user` object than /api/login did —
which mattered because the frontend drops that object into localStorage on
both paths and reads `role` off it to gate the admin console.
"""

import contextlib

import bcrypt
import pytest
from fastapi import HTTPException, Response

import routers.auth as auth
from core.models import RegisterRequest

USER = "new@agency.com"


@pytest.fixture
def auth_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(auth, "get_mongo_client", fake_client)
    return auth


async def register(**overrides):
    payload = {"email": USER, "password": "hunter2hunter2", **overrides}
    return await auth.register_user(RegisterRequest(**payload), Response())


# ── the leaner payload ────────────────────────────────────────────────────


def test_name_and_currency_are_optional_on_the_payload():
    # The onboarding wizard collects both after the account exists, so the
    # form only posts email + password.
    req = RegisterRequest(email=USER, password="hunter2hunter2")
    assert req.name is None
    assert req.default_currency is None


@pytest.mark.asyncio
async def test_email_and_password_alone_create_a_user(auth_api, mock_db):
    result = await register()

    assert result["message"] == "Registration successful"
    doc = await mock_db["users"].find_one({"user_id": USER})
    # The keys stay on the doc (as None) so every user has the same shape and
    # get_me / login don't have to distinguish missing from not-yet-collected.
    assert doc["name"] is None
    assert doc["default_currency"] is None
    assert bcrypt.checkpw(b"hunter2hunter2", doc["password"].encode())


@pytest.mark.asyncio
async def test_a_client_still_sending_name_and_currency_keeps_working(auth_api, mock_db):
    await register(name="SOUP Marketing", default_currency="GBP")

    doc = await mock_db["users"].find_one({"user_id": USER})
    assert doc["name"] == "SOUP Marketing"
    assert doc["default_currency"] == "GBP"


# ── the response the frontend stores ──────────────────────────────────────


@pytest.mark.asyncio
async def test_response_user_matches_the_login_shape(auth_api, mock_db):
    result = await register()

    # Same keys /api/login returns — ProtectedLayout reads `role` off this.
    assert result["user"] == {
        "email": USER,
        "name": None,
        "default_currency": None,
        "role": "user",
    }


@pytest.mark.asyncio
async def test_session_cookies_are_set_so_no_second_login_is_needed(auth_api):
    response = Response()
    await auth.register_user(RegisterRequest(email=USER, password="hunter2hunter2"), response)

    cookies = response.headers.getlist("set-cookie")
    assert any(c.startswith("auth_token=") for c in cookies)
    assert any(c.startswith("refresh_token=") for c in cookies)


@pytest.mark.asyncio
async def test_duplicate_email_is_still_rejected(auth_api, mock_db):
    await register()

    with pytest.raises(HTTPException) as exc:
        await register()
    assert exc.value.status_code == 400
    assert exc.value.detail == "Email already registered"
