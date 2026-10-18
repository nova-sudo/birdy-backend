"""
tests/test_dashboards_router.py
-------------------------------
CRUD for dashkit dashboard documents: round-trip, listing, schema validation,
per-user isolation, delete, and path/body id agreement.
"""

import copy

import pytest
from fastapi.testclient import TestClient

from dependencies import get_current_user
from main import app


def _doc(dash_id="d1", title="My dashboard"):
    return {
        "id": dash_id,
        "version": 1,
        "title": title,
        "sections": [{"id": "s1", "title": "Overview", "widgets": []}],
        "meta": {
            "createdAt": "2026-10-01T00:00:00Z",
            "updatedAt": "2026-10-01T00:00:00Z",
            "lastAuthor": "user",
        },
    }


@pytest.fixture
def client(mock_db):
    app.dependency_overrides[get_current_user] = lambda: "alice@example.com"
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _as(user):
    app.dependency_overrides[get_current_user] = lambda: user


def test_put_then_get_round_trips(client):
    r = client.put("/api/dashboards/d1", json=_doc())
    assert r.status_code == 200
    assert r.json()["id"] == "d1"

    got = client.get("/api/dashboards/d1")
    assert got.status_code == 200
    body = got.json()
    assert body["title"] == "My dashboard"
    assert body["sections"] == _doc()["sections"]
    assert "_id" not in body


def test_put_refreshes_meta_updated_at(client):
    r = client.put("/api/dashboards/d1", json=_doc())
    assert r.json()["meta"]["updatedAt"] != "2026-10-01T00:00:00Z"
    assert r.json()["meta"]["createdAt"] == "2026-10-01T00:00:00Z"


def test_put_is_an_upsert(client):
    client.put("/api/dashboards/d1", json=_doc())
    client.put("/api/dashboards/d1", json=_doc(title="Renamed"))
    assert client.get("/api/dashboards/d1").json()["title"] == "Renamed"
    assert len(client.get("/api/dashboards").json()["items"]) == 1


def test_list_returns_title_and_updated_at_newest_first(client):
    client.put("/api/dashboards/d1", json=_doc("d1", "First"))
    client.put("/api/dashboards/d2", json=_doc("d2", "Second"))

    items = client.get("/api/dashboards").json()["items"]
    assert [i["id"] for i in items] == ["d2", "d1"]
    assert items[0]["title"] == "Second"
    assert set(items[0]) == {"id", "title", "updatedAt"}
    assert isinstance(items[0]["updatedAt"], str)


def test_invalid_doc_is_422_with_errors(client):
    bad = _doc()
    del bad["title"]
    bad["sections"][0]["widgets"] = "nope"

    r = client.put("/api/dashboards/d1", json=bad)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert isinstance(errors, list) and errors
    assert any(e.startswith("/sections/0/widgets: ") for e in errors)
    assert any(e.startswith("/: ") and "title" in e for e in errors)
    assert client.get("/api/dashboards/d1").status_code == 404


def test_another_users_dashboard_is_404(client):
    client.put("/api/dashboards/d1", json=_doc())
    _as("bob@example.com")
    assert client.get("/api/dashboards/d1").status_code == 404
    assert client.delete("/api/dashboards/d1").status_code == 404
    assert client.get("/api/dashboards").json()["items"] == []

    # Bob saving the same id does not overwrite Alice's copy.
    client.put("/api/dashboards/d1", json=_doc(title="Bob's"))
    _as("alice@example.com")
    assert client.get("/api/dashboards/d1").json()["title"] == "My dashboard"


def test_delete_then_get_is_404(client):
    client.put("/api/dashboards/d1", json=_doc())
    assert client.delete("/api/dashboards/d1").status_code == 204
    assert client.get("/api/dashboards/d1").status_code == 404


def test_id_mismatch_is_400(client):
    r = client.put("/api/dashboards/other", json=_doc("d1"))
    assert r.status_code == 400


def test_put_does_not_mutate_unrelated_fields(client):
    doc = _doc()
    doc["description"] = "hello"
    sent = copy.deepcopy(doc)
    client.put("/api/dashboards/d1", json=doc)
    got = client.get("/api/dashboards/d1").json()
    assert got["description"] == sent["description"]


@pytest.mark.asyncio
async def test_unique_index_on_user_and_id(mock_mongo_client, mock_db):
    from pymongo.errors import DuplicateKeyError
    from routers.dashboards import create_dashboards_indexes

    await create_dashboards_indexes(mock_mongo_client)
    await mock_db["dashboards"].insert_one({"user_id": "u", "id": "d1"})
    await mock_db["dashboards"].insert_one({"user_id": "v", "id": "d1"})
    with pytest.raises(DuplicateKeyError):
        await mock_db["dashboards"].insert_one({"user_id": "u", "id": "d1"})
