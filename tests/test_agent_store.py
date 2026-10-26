import pytest

from ai.agents.defaults import DEFAULT_PARAMS, RULE_KINDS
from ai.agents.store import (
    AgentExists,
    RULE_VERSIONS,
    add_memory,
    create_agent,
    create_agent_indexes,
    deactivate_memory,
    get_agent,
    list_memory,
    list_rules,
    update_agent,
    update_rule,
)


async def test_create_agent_seeds_four_rules_with_defaults(mock_db):
    agent = await create_agent(mock_db, "u1")
    assert agent["status"] == "draft"
    rules = await list_rules(mock_db, agent["_id"])
    assert {r["kind"] for r in rules} == set(RULE_KINDS)
    pause = next(r for r in rules if r["kind"] == "pause_over_target")
    assert pause["params"] == DEFAULT_PARAMS["pause_over_target"]
    assert pause["mode"] == "ask"


async def test_one_agent_per_user_per_type(mock_db):
    await create_agent(mock_db, "u1")
    with pytest.raises(AgentExists):
        await create_agent(mock_db, "u1")
    await create_agent(mock_db, "u2")


async def test_get_agent_is_owner_scoped(mock_db):
    agent = await create_agent(mock_db, "u1")
    assert (await get_agent(mock_db, "u1", agent["_id"]))["_id"] == agent["_id"]
    assert await get_agent(mock_db, "u2", agent["_id"]) is None


async def test_update_rule_versions_params(mock_db):
    agent = await create_agent(mock_db, "u1")
    rule = (await list_rules(mock_db, agent["_id"]))[0]
    new = await update_rule(mock_db, agent["_id"], rule["_id"], params={**rule["params"], "x": 1})
    assert new["version"] == 2
    versions = await mock_db[RULE_VERSIONS].find({}).to_list(length=None)
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["params"] == rule["params"]


async def test_update_rule_mode_only_does_not_bump_version(mock_db):
    agent = await create_agent(mock_db, "u1")
    rule = (await list_rules(mock_db, agent["_id"]))[0]
    new = await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")
    assert new["version"] == 1 and new["mode"] == "auto"
    assert await mock_db[RULE_VERSIONS].count_documents({}) == 0


async def test_update_agent_whitelists_keys(mock_db):
    agent = await create_agent(mock_db, "u1")
    out = await update_agent(mock_db, "u1", agent["_id"], {"status": "active"})
    assert out["status"] == "active"
    with pytest.raises(ValueError):
        await update_agent(mock_db, "u1", agent["_id"], {"user_id": "evil"})


async def test_memory_add_list_deactivate(mock_db):
    agent = await create_agent(mock_db, "u1")
    m = await add_memory(mock_db, agent["_id"], text="keep X", kind="note", source="user")
    assert len(await list_memory(mock_db, agent["_id"])) == 1
    assert await deactivate_memory(mock_db, agent["_id"], m["_id"]) is True
    assert await list_memory(mock_db, agent["_id"]) == []
    assert len(await list_memory(mock_db, agent["_id"], active_only=False)) == 1


async def test_create_agent_indexes(mock_mongo_client):
    await create_agent_indexes(mock_mongo_client)
