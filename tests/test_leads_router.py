"""
tests/test_leads_router.py
--------------------------
GET /api/leads/contact/{contact_id}: 200 with the assembled lead detail,
404 "Lead not found" for an unknown contact, and the static /unified route
is not shadowed by the contact route.
"""

import contextlib

import pytest
from fastapi.testclient import TestClient

import routers.leads as leads_router
from dependencies import get_current_user
from core.database import DB_NAME
from main import app

USER = "u1"


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(leads_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def _seed_contact(mock_mongo_client, contact_id="c1", user=USER):
    await mock_mongo_client[DB_NAME]["ghl_contacts"].insert_one({
        "user_id": user,
        "contact_data": {"id": contact_id, "firstName": "Ada", "lastName": "Lovelace", "email": "ada@example.com"},
    })


async def test_get_lead_detail_200(api, mock_mongo_client):
    await _seed_contact(mock_mongo_client)

    resp = api.get("/api/leads/contact/c1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["contact"]["id"] == "c1"


async def test_get_lead_detail_404_for_unknown(api, mock_mongo_client):
    await _seed_contact(mock_mongo_client, contact_id="c1")

    resp = api.get("/api/leads/contact/does-not-exist")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Lead not found"}


async def test_get_lead_detail_404_for_other_users_contact(api, mock_mongo_client):
    await _seed_contact(mock_mongo_client, contact_id="c1", user="someone-else")

    resp = api.get("/api/leads/contact/c1")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Lead not found"}


def test_unified_route_not_shadowed(api):
    resp = api.get("/api/leads/unified")

    # It may fail for other reasons under mongomock, but it must not be our
    # handler answering "Lead not found" for a contact called "unified".
    assert not (resp.status_code == 404 and resp.json().get("detail") == "Lead not found")


# --- messages -------------------------------------------------------------

from unittest.mock import AsyncMock

import services.ghl_messages as gm


async def _seed_full(mock_mongo_client, contact_id="c1", user=USER, phone="+15550001111", email="ada@example.com"):
    data = {"id": contact_id, "firstName": "Ada"}
    if phone:
        data["phone"] = phone
    if email:
        data["email"] = email
    await mock_mongo_client[DB_NAME]["ghl_contacts"].insert_one(
        {"user_id": user, "location_id": "loc1", "contact_data": data})


@pytest.fixture
def send(monkeypatch):
    mock = AsyncMock(return_value={"id": "m1", "channel": "SMS", "direction": "outbound", "body": "hi",
                                   "subject": None, "status": "sent", "date": "2026-01-01T00:00:00+00:00",
                                   "attachments": [], "source": "api"})
    monkeypatch.setattr(leads_router, "send_lead_message", mock)
    return mock


async def test_get_messages_404_for_other_user(api, mock_mongo_client):
    await _seed_full(mock_mongo_client, user="someone-else")
    resp = api.get("/api/leads/contact/c1/messages")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Lead not found"}


async def test_get_messages_ok(api, mock_mongo_client, monkeypatch):
    await _seed_full(mock_mongo_client)
    fetch = AsyncMock(return_value={"status": "ok", "messages": []})
    monkeypatch.setattr(leads_router, "fetch_lead_messages", fetch)
    resp = api.get("/api/leads/contact/c1/messages")
    assert resp.status_code == 200 and resp.json() == {"status": "ok", "messages": []}
    assert fetch.call_args.args[1:] == (USER, "loc1", "c1")


async def test_send_sms_ok_returns_201_message(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client)
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "  hi  "})
    assert resp.status_code == 201
    assert resp.json()["message"]["id"] == "m1"
    assert send.call_args.args[1:] == (USER, "loc1", "c1", "SMS", "hi", None)


async def test_send_email_requires_subject(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client)
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "Email", "message": "hi", "subject": " "})
    assert resp.status_code == 422
    send.assert_not_called()


async def test_send_sms_without_phone_422(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client, phone=None)
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "hi"})
    assert resp.status_code == 422
    send.assert_not_called()


async def test_send_email_without_email_422(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client, email=None)
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "Email", "message": "hi", "subject": "s"})
    assert resp.status_code == 422
    send.assert_not_called()


async def test_send_subject_too_long_422(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client)
    resp = api.post("/api/leads/contact/c1/messages",
                    json={"channel": "Email", "message": "hi", "subject": "s" * 301})
    assert resp.status_code == 422
    send.assert_not_called()


async def test_send_404_for_other_user(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client, user="someone-else")
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "hi"})
    assert resp.status_code == 404


@pytest.mark.parametrize("exc", [gm.MessagingReconnect, gm.MessagingUnavailable])
async def test_send_reconnect_409(api, mock_mongo_client, send, exc):
    await _seed_full(mock_mongo_client)
    send.side_effect = exc()
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "hi"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "Reconnect GoHighLevel to send messages"}


async def test_send_failure_502(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client)
    send.side_effect = gm.MessagingFailed()
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "hi"})
    assert resp.status_code == 502
    assert resp.json() == {"detail": "GoHighLevel rejected the message."}


async def test_send_unconfirmed_504(api, mock_mongo_client, send):
    await _seed_full(mock_mongo_client)
    send.side_effect = gm.MessagingUnconfirmed()
    resp = api.post("/api/leads/contact/c1/messages", json={"channel": "SMS", "message": "hi"})
    assert resp.status_code == 504
    assert resp.json() == {
        "detail": "We couldn't confirm the message was sent. Check the thread before sending again."}


async def test_get_lead_journey_200(api, mock_mongo_client):
    await _seed_contact(mock_mongo_client)

    resp = api.get("/api/leads/contact/c1/journey")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ghl"] == "unavailable"  # no location on the seeded contact
    assert [e["kind"] for e in body["events"]] == []  # contact has no dateAdded, so no lead event


async def test_get_lead_journey_404_for_unknown_and_other_users(api, mock_mongo_client):
    await _seed_contact(mock_mongo_client, contact_id="c2", user="someone-else")

    for contact_id in ("does-not-exist", "c2"):
        resp = api.get(f"/api/leads/contact/{contact_id}/journey")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Lead not found"}
