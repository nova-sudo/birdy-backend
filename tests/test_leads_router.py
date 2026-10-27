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
