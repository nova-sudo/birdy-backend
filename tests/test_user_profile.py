"""
tests/test_user_profile.py
--------------------------
The account fields behind the Settings General tab.

That tab shipped as a literal placeholder — "General settings content goes
here" — because none of these had a write path. Name and currency were stored
but unreachable, timezone was not stored at all, and the only way to change a
password was to not have one.
"""

import contextlib

import bcrypt
import pytest
from fastapi import HTTPException

import routers.settings as settings

USER = "user@example.com"


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.fixture
def profile_api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(settings, "get_mongo_client", fake_client)
    return settings


async def a_user(db, password="correct-horse", **extra):
    await db["users"].insert_one({
        "user_id": USER,
        "name": "SOUP Marketing",
        "default_currency": "GBP",
        "password": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        **extra,
    })


async def patch_profile(payload, user=USER):
    return await settings.update_user_profile(FakeRequest(payload), current_user=user)


# ── reading ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_profile_returns_the_stored_fields(profile_api, mock_db):
    await a_user(mock_db, timezone="Europe/London")

    out = await settings.get_user_profile(current_user=USER)

    assert out["name"] == "SOUP Marketing"
    assert out["default_currency"] == "GBP"
    assert out["timezone"] == "Europe/London"


@pytest.mark.asyncio
async def test_email_is_the_account_id(profile_api, mock_db):
    """There is no separate email field — the user id is the email, which is
    also why the tab shows it read-only."""
    await a_user(mock_db)
    out = await settings.get_user_profile(current_user=USER)
    assert out["email"] == USER


@pytest.mark.asyncio
async def test_profile_defaults_rather_than_returning_nulls(profile_api, mock_db):
    await mock_db["users"].insert_one({"user_id": USER})
    out = await settings.get_user_profile(current_user=USER)
    assert out["name"] == ""
    assert out["default_currency"] == "USD"
    assert out["timezone"] == ""


# ── writing ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_partial_update_leaves_the_rest_alone(profile_api, mock_db):
    await a_user(mock_db, timezone="Europe/London")

    await patch_profile({"name": "New Name"})

    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["name"] == "New Name"
    assert stored["default_currency"] == "GBP"
    assert stored["timezone"] == "Europe/London"


@pytest.mark.asyncio
async def test_currency_is_upper_cased(profile_api, mock_db):
    await a_user(mock_db)
    await patch_profile({"default_currency": "eur"})
    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["default_currency"] == "EUR"


@pytest.mark.asyncio
async def test_name_is_trimmed(profile_api, mock_db):
    await a_user(mock_db)
    await patch_profile({"name": "  Padded  "})
    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["name"] == "Padded"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   "])
async def test_a_blank_name_is_rejected(profile_api, mock_db, bad):
    await a_user(mock_db)
    with pytest.raises(HTTPException) as exc:
        await patch_profile({"name": bad})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["POUNDS", "G", "12X", ""])
async def test_a_malformed_currency_is_rejected(profile_api, mock_db, bad):
    await a_user(mock_db)
    with pytest.raises(HTTPException) as exc:
        await patch_profile({"default_currency": bad})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_an_empty_update_is_rejected(profile_api, mock_db):
    await a_user(mock_db)
    with pytest.raises(HTTPException) as exc:
        await patch_profile({})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_an_unknown_user_is_404(profile_api, mock_db):
    with pytest.raises(HTTPException) as exc:
        await patch_profile({"name": "Nobody"})
    assert exc.value.status_code == 404


# ── password ──────────────────────────────────────────────────────────────


async def change(current, new, user=USER):
    return await settings.change_password(
        FakeRequest({"current_password": current, "new_password": new}),
        current_user=user,
    )


@pytest.mark.asyncio
async def test_password_changes_when_the_current_one_is_right(profile_api, mock_db):
    await a_user(mock_db, password="correct-horse")

    out = await change("correct-horse", "battery-staple-9")

    assert out["success"] is True
    stored = await mock_db["users"].find_one({"user_id": USER})
    assert bcrypt.checkpw(b"battery-staple-9", stored["password"].encode())


@pytest.mark.asyncio
async def test_the_old_password_stops_working(profile_api, mock_db):
    await a_user(mock_db, password="correct-horse")
    await change("correct-horse", "battery-staple-9")

    stored = await mock_db["users"].find_one({"user_id": USER})
    assert not bcrypt.checkpw(b"correct-horse", stored["password"].encode())


@pytest.mark.asyncio
async def test_a_wrong_current_password_is_refused(profile_api, mock_db):
    await a_user(mock_db, password="correct-horse")

    with pytest.raises(HTTPException) as exc:
        await change("wrong", "battery-staple-9")

    # 400 rather than 401 deliberately: 401 is the app's session-expired
    # signal and would bounce the user to /login mid-form.
    assert exc.value.status_code == 400

    stored = await mock_db["users"].find_one({"user_id": USER})
    assert bcrypt.checkpw(b"correct-horse", stored["password"].encode())


@pytest.mark.asyncio
async def test_a_short_password_is_refused(profile_api, mock_db):
    await a_user(mock_db)
    with pytest.raises(HTTPException) as exc:
        await change("correct-horse", "short")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reusing_the_same_password_is_refused(profile_api, mock_db):
    await a_user(mock_db, password="correct-horse")
    with pytest.raises(HTTPException) as exc:
        await change("correct-horse", "correct-horse")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_the_stored_hash_is_not_the_plaintext(profile_api, mock_db):
    await a_user(mock_db, password="correct-horse")
    await change("correct-horse", "battery-staple-9")
    stored = await mock_db["users"].find_one({"user_id": USER})
    assert stored["password"] != "battery-staple-9"
    assert stored["password"].startswith("$2")
