import contextlib

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
from ai.agents import store
from dependencies import get_current_user
from main import app


async def test_migration_renames_and_is_idempotent(mock_db):
    await mock_db.agents.insert_one({"_id": "a", "user_id": "u", "type": "media_buying", "status": "active",
                                     "scope": {"client_group_ids": ["g1"]}})
    await store.migrate_agent_types(mock_db)
    await store.migrate_agent_types(mock_db)
    doc = await mock_db.agents.find_one({"_id": "a"})
    assert (doc["type"], doc["name"]) == ("custom", "Pause and revive rules")


async def test_custom_agents_can_repeat_stock_cannot(mock_db):
    await store.create_agent(mock_db, "u", type="custom", name="One")
    await store.create_agent(mock_db, "u", type="custom", name="Two")
    await store.create_agent(mock_db, "u", type=store.TYPE_STOCK)
    with pytest.raises(store.AgentExists):
        await store.create_agent(mock_db, "u", type=store.TYPE_STOCK)


async def test_stock_agent_has_no_rules(mock_db):
    a = await store.create_agent(mock_db, "u", type=store.TYPE_STOCK)
    assert a["name"] == "Birdy Media Buying Agent"
    assert await store.list_rules(mock_db, a["_id"]) == []


async def test_custom_agent_keeps_rules_and_name(mock_db):
    a = await store.create_agent(mock_db, "u", type="custom", name="Mine")
    assert a["name"] == "Mine"
    assert await store.list_rules(mock_db, a["_id"])


async def test_name_and_settings_are_patchable(mock_db):
    a = await store.create_agent(mock_db, "u", type="custom", name="Mine")
    out = await store.update_agent(mock_db, "u", a["_id"], {"name": "Renamed", "settings": {"x": 1}})
    assert (out["name"], out["settings"]) == ("Renamed", {"x": 1})


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: "u1@example.com"
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("name", ["", "   ", "x" * 61, None])
def test_custom_requires_valid_name(api, name):
    body = {"type": "custom"} if name is None else {"type": "custom", "name": name}
    assert api.post("/api/agents", json=body).status_code == 400


def test_router_creates_custom_and_stock(api):
    r = api.post("/api/agents", json={"type": "custom", "name": " Weekend "})
    assert r.status_code == 200 and r.json()["name"] == "Weekend" and r.json()["type"] == "custom"
    assert api.post("/api/agents", json={"type": "stock_media_buying"}).status_code == 200
    assert api.post("/api/agents", json={"type": "stock_media_buying"}).status_code == 409
    assert api.post("/api/agents", json={"type": "bogus", "name": "x"}).status_code == 400
