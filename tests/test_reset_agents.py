"""scripts/reset_agents: wipes one account's agents, backs them up, and can put them back."""
import pytest
from mongomock_motor import AsyncMongoMockClient

from scripts import reset_agents as ra


async def _seed(db):
    await db["users"].insert_many([{"user_id": "u1", ra.DEFAULT_FIELD: "a1"}, {"user_id": "u2"}])
    await db["agents"].insert_many([
        {"_id": "a1", "user_id": "u1", "type": "stock_media_buying", "name": "MB", "status": "active"},
        {"_id": "a9", "user_id": "u2", "type": "custom", "name": "Other", "status": "draft"},
    ])
    await db["sub_agents"].insert_one({"_id": "s1", "user_id": "u1", "parent": "media_buyer"})
    await db["team_agents"].insert_one({"_id": "sales_coach:u1", "user_id": "u1", "key": "sales_coach"})
    await db["agent_client_claims"].insert_one({"user_id": "u1", "client_group_id": "g1", "agent_id": "a1"})
    await db["agent_memory"].insert_many([
        {"_id": "m1", "agent_id": "a1"}, {"_id": "m2", "agent_id": "s1"},
        {"_id": "m3", "agent_id": "sales_coach:u1"}, {"_id": "m9", "agent_id": "a9"},
    ])
    await db["agent_runs"].insert_one({"_id": "r1", "agent_id": "a1"})
    await db["client_groups"].insert_one({"id": "g1", "user_id": "u1", "targets": {"cpl": 30}})


async def _counts(db):
    return {c: await db[c].count_documents({}) for c in
            ("agents", "sub_agents", "team_agents", "agent_client_claims", "agent_memory", "agent_runs", "client_groups")}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ra, "BACKUP_DIR", str(tmp_path))
    return AsyncMongoMockClient()["t"]


async def test_dry_run_changes_nothing(db):
    await _seed(db)
    before = await _counts(db)
    assert await ra.reset(db, "u1", keep_history=False, dry_run=True) == 8
    assert await _counts(db) == before


async def test_reset_removes_only_that_account_and_restore_puts_it_back(db, tmp_path):
    await _seed(db)
    before = await _counts(db)
    await ra.reset(db, "u1", keep_history=False, dry_run=False)
    assert await _counts(db) == {"agents": 1, "sub_agents": 0, "team_agents": 0, "agent_client_claims": 0,
                                 "agent_memory": 1, "agent_runs": 0, "client_groups": 1}
    assert await db["agents"].find_one({"_id": "a9"})
    assert ra.DEFAULT_FIELD not in await db["users"].find_one({"user_id": "u1"})

    (backup,) = list(tmp_path.iterdir())
    await ra.restore(db, str(backup))
    assert await _counts(db) == before
    assert (await db["users"].find_one({"user_id": "u1"}))[ra.DEFAULT_FIELD] == "a1"


async def test_keep_history_leaves_runs(db):
    await _seed(db)
    await ra.reset(db, "u1", keep_history=True, dry_run=False)
    assert await db["agent_runs"].count_documents({}) == 1
    assert await db["agent_memory"].count_documents({}) == 1
