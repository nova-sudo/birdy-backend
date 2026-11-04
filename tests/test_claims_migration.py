"""The claims migration runs once per agent: moves, turn-offs and removals stay as a person left them."""
from datetime import datetime

import pytest

from ai.agents import claims, store
from ai.agents.store import AGENTS, create_agent
from core.indexes import ensure_indexes
from test_stock_router import api  # noqa: F401 - api is a fixture
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture

MARKER = "claims_migrated_at"


async def _legacy_custom(db, *gids, created=datetime(2026, 1, 1), name="Old rules"):
    """An agent from before claims existed: active, watching its clients, no marker."""
    a = await create_agent(db, USER, type="custom", name=name)
    await db[AGENTS].update_one({"_id": a["_id"]}, {"$set": {
        "status": "active", "created_at": created, "scope": {"client_group_ids": list(gids)}}})
    return a["_id"]


async def _ensure(client):
    """The pass the nightly cron and the deploy script both run."""
    result = await ensure_indexes(client)
    assert "create_agent_indexes" not in result["failed"], result["failed"]


async def _stock_holding(api, db, client, *gids):
    """An older custom agent is migrated onto gids, then the stock agent turns on and moves them over."""
    stock = await _setup(db, *gids, claim=False)
    aid = stock["_id"]
    await db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    custom = await _legacy_custom(db, *gids)
    await _ensure(client)
    assert await claims.holder(db, USER, gids[0]) == custom
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    assert api.post(f"/api/agents/{aid}/activate").status_code == 409
    r = api.post(f"/api/agents/{aid}/activate", json={"move_claims": True})
    assert r.status_code == 200, r.text
    assert await claims.holder(db, USER, gids[0]) == aid
    return aid, custom


async def test_a_moved_client_stays_off_the_old_agent_after_the_stock_agent_turns_off(api, mock_db, mock_mongo_client):
    aid, _ = await _stock_holding(api, mock_db, mock_mongo_client, "g1")
    assert api.post(f"/api/agents/{aid}/deactivate").status_code == 200
    assert await claims.holder(mock_db, USER, "g1") is None
    for _ in range(2):
        await _ensure(mock_mongo_client)
        assert await claims.holder(mock_db, USER, "g1") is None


async def test_a_client_removed_from_the_stock_agents_scope_stays_unclaimed(api, mock_db, mock_mongo_client):
    aid, _ = await _stock_holding(api, mock_db, mock_mongo_client, "g1", "g2")
    r = api.patch(f"/api/agents/{aid}/settings", json={"client_group_ids": ["g2"]})
    assert r.status_code == 200, r.text
    assert await claims.holder(mock_db, USER, "g1") is None
    for _ in range(2):
        await _ensure(mock_mongo_client)
        assert await claims.holder(mock_db, USER, "g1") is None
    assert await claims.holder(mock_db, USER, "g2") == aid


async def test_deleting_a_draft_that_held_a_client_doesnt_hand_it_back(api, mock_db, mock_mongo_client):
    aid, _ = await _stock_holding(api, mock_db, mock_mongo_client, "g1")
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})  # a draft that still holds a claim
    assert api.delete(f"/api/agents/{aid}").status_code == 200
    assert await claims.holder(mock_db, USER, "g1") is None
    for _ in range(2):
        await _ensure(mock_mongo_client)
        assert await claims.holder(mock_db, USER, "g1") is None


async def test_an_unmarked_agent_gets_its_claims_once_and_is_marked(mock_db):
    aid = await _legacy_custom(mock_db, "g1", "g2")
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g1") == aid
    assert await claims.holder(mock_db, USER, "g2") == aid
    assert (await mock_db[AGENTS].find_one({"_id": aid}))[MARKER]

    await claims.release_clients(mock_db, USER, aid, ["g1"])
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g1") is None
    assert await claims.holder(mock_db, USER, "g2") == aid


async def test_a_watcher_marked_by_the_migration_doesnt_inherit_when_the_holder_lets_go(mock_db):
    first = await _legacy_custom(mock_db, "g1", name="First")
    second = await _legacy_custom(mock_db, "g1", created=datetime(2026, 2, 1), name="Second")
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g1") == first
    assert (await mock_db[AGENTS].find_one({"_id": second}))[MARKER]
    await claims.release_clients(mock_db, USER, first)
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g1") is None


async def test_a_failed_pass_leaves_the_agent_unmarked_so_the_next_pass_finishes_it(mock_db, monkeypatch):
    aid = await _legacy_custom(mock_db, "g1", "g2")
    real = claims.claim_clients

    async def fails_on_g2(db, user_id, agent_id, gids, **kw):
        if gids == ["g2"]:
            raise RuntimeError("mongo went away")
        return await real(db, user_id, agent_id, gids, **kw)

    monkeypatch.setattr(claims, "claim_clients", fails_on_g2)
    with pytest.raises(RuntimeError):
        await claims.migrate_claims(mock_db)
    assert MARKER not in await mock_db[AGENTS].find_one({"_id": aid})

    monkeypatch.setattr(claims, "claim_clients", real)
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g2") == aid
    assert (await mock_db[AGENTS].find_one({"_id": aid}))[MARKER]


async def test_a_draft_isnt_marked_and_still_claims_when_it_turns_on(mock_db):
    a = await create_agent(mock_db, USER, type="custom", name="Draft")
    await mock_db[AGENTS].update_one({"_id": a["_id"]}, {"$set": {"scope": {"client_group_ids": ["g1"]}}})
    await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, USER, "g1") is None
    assert MARKER not in await mock_db[AGENTS].find_one({"_id": a["_id"]})


async def test_turning_on_through_the_normal_path_marks_a_new_agent(api, mock_db, mock_mongo_client):
    stock = await _setup(mock_db, "g1", "g2", claim=False)
    aid = stock["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 200, r.text
    assert (await mock_db[AGENTS].find_one({"_id": aid}))[MARKER]
    assert MARKER not in r.json()  # bookkeeping, not part of the API

    await claims.release_clients(mock_db, USER, aid, ["g1"])  # so a migration pass would show itself
    await _ensure(mock_mongo_client)
    assert await claims.holder(mock_db, USER, "g1") is None


async def test_a_refused_claim_leaves_no_marker(api, mock_db):
    stock = await _setup(mock_db, "g1", claim=False)
    aid = stock["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    assert api.post(f"/api/agents/{aid}/activate").status_code == 409
    assert MARKER not in await mock_db[AGENTS].find_one({"_id": aid})


async def test_a_scope_save_on_a_live_unmarked_agent_counts_as_its_claim(api, mock_db):
    await mock_db.client_groups.insert_one({"id": "g1", "user_id": USER, "name": "g1"})
    aid = await _legacy_custom(mock_db)
    r = api.patch(f"/api/agents/{aid}", json={"scope": {"client_group_ids": ["g1"]}})
    assert r.status_code == 200, r.text
    assert await claims.holder(mock_db, USER, "g1") == aid
    assert (await mock_db[AGENTS].find_one({"_id": aid}))[MARKER]


async def test_drop_of_a_missing_old_index_is_fine_but_a_lost_connection_is_not(mock_mongo_client, monkeypatch):
    """Only the server saying no may be ignored; anything else must reach the deploy script."""
    from pymongo.errors import ServerSelectionTimeoutError

    await store.create_agent_indexes(mock_mongo_client)  # the old index isn't there: no error
    await store.create_agent_indexes(mock_mongo_client)

    coll = type(mock_mongo_client[store.DB_NAME][AGENTS])

    async def gone(self, name):
        raise ServerSelectionTimeoutError("no primary")

    monkeypatch.setattr(coll, "drop_index", gone)
    with pytest.raises(ServerSelectionTimeoutError):
        await store.create_agent_indexes(mock_mongo_client)
