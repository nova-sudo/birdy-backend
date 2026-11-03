"""One acting agent per client: claims, guardrail check, migration, router 409s, runner flagging."""
import asyncio
import contextlib
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
from ai.agents import claims, store
from ai.agents.guardrails import check_guardrails
from dependencies import get_current_user
from main import app

U = "u1"


async def _agent(db, name="A", status="active", gids=("g1",), type=store.TYPE_CUSTOM, created=None):
    a = await store.create_agent(db, U, type=type, name=name)
    patch = {"status": status, "scope": {"client_group_ids": list(gids)}}
    await store.update_agent(db, U, a["_id"], patch)
    if created:
        await db.agents.update_one({"_id": a["_id"]}, {"$set": {"created_at": created}})
    return await db.agents.find_one({"_id": a["_id"]})


async def test_conflict_names_holder_and_claims_nothing(mock_db):
    a, b = await _agent(mock_db, "Alpha"), await _agent(mock_db, "Beta")
    assert await claims.claim_clients(mock_db, U, a["_id"], ["g1"]) == []
    conflicts = await claims.claim_clients(mock_db, U, b["_id"], ["g2", "g1"])
    assert conflicts == [{"client_group_id": "g1", "holder_id": a["_id"], "holder_name": "Alpha"}]
    assert await claims.holder(mock_db, U, "g2") is None  # no partial claim
    assert await claims.holder(mock_db, U, "g1") == a["_id"]


async def test_claiming_own_client_again_is_fine(mock_db):
    a = await _agent(mock_db)
    await claims.claim_clients(mock_db, U, a["_id"], ["g1"])
    assert await claims.claim_clients(mock_db, U, a["_id"], ["g1"]) == []


async def test_move_transfers(mock_db):
    a, b = await _agent(mock_db, "Alpha"), await _agent(mock_db, "Beta")
    await claims.claim_clients(mock_db, U, a["_id"], ["g1"])
    assert await claims.claim_clients(mock_db, U, b["_id"], ["g1"], move=True) == []
    assert await claims.holder(mock_db, U, "g1") == b["_id"]
    assert (await claims.acting_agent(mock_db, U, "g1"))["_id"] == b["_id"]
    assert await claims.acting_agent(mock_db, U, "nope") is None


async def test_release_all_or_some(mock_db):
    a, b = await _agent(mock_db), await _agent(mock_db)
    await claims.claim_clients(mock_db, U, a["_id"], ["g1", "g2"])
    await claims.release_clients(mock_db, U, b["_id"])  # not the holder: nothing happens
    assert await claims.holder(mock_db, U, "g1") == a["_id"]
    await claims.release_clients(mock_db, U, a["_id"], ["g1"])
    assert await claims.holder(mock_db, U, "g1") is None
    assert await claims.holder(mock_db, U, "g2") == a["_id"]
    await claims.release_clients(mock_db, U, a["_id"])
    assert await claims.holder(mock_db, U, "g2") is None


async def test_delete_draft_and_turning_off_release(mock_db):
    d = await _agent(mock_db, status="draft")
    await claims.claim_clients(mock_db, U, d["_id"], ["g1"])
    await store.delete_draft_agent(mock_db, U, d["_id"])
    assert await claims.holder(mock_db, U, "g1") is None

    a = await _agent(mock_db)
    await claims.claim_clients(mock_db, U, a["_id"], ["g2"])
    await store.update_agent(mock_db, U, a["_id"], {"status": "paused"})
    assert await claims.holder(mock_db, U, "g2") is None


async def test_concurrent_claims_yield_one_holder(mock_db):
    await claims.ensure_indexes(mock_db)
    a, b = await _agent(mock_db), await _agent(mock_db)
    res = await asyncio.gather(claims.claim_clients(mock_db, U, a["_id"], ["g1"]),
                               claims.claim_clients(mock_db, U, b["_id"], ["g1"]))
    assert sorted(len(r) for r in res) == [0, 1]
    assert await mock_db[claims.CLAIMS].count_documents({"client_group_id": "g1"}) == 1
    winner = a["_id"] if res[0] == [] else b["_id"]
    assert await claims.holder(mock_db, U, "g1") == winner


def _doc(gid="g1"):
    return {"_id": "s", "client_group_id": gid, "action": {"type": "enable_ads", "targets": []}}


async def test_guardrails_block_non_acting_agent(mock_db):
    a, b = await _agent(mock_db), await _agent(mock_db)
    await claims.claim_clients(mock_db, U, a["_id"], ["g1"])
    r = await check_guardrails(mock_db, b, _doc(), now_utc=datetime(2026, 10, 20))
    assert not r.ok and r.reason == "not_acting_agent"
    assert (await check_guardrails(mock_db, a, _doc(), now_utc=datetime(2026, 10, 20))).ok
    # an unclaimed client does not block
    assert (await check_guardrails(mock_db, b, _doc("g9"), now_utc=datetime(2026, 10, 20))).ok


async def test_migration_claims_active_agents_first_created_wins_idempotent(mock_db, mock_mongo_client):
    first = await _agent(mock_db, "First", gids=("g1", "g2"), created=datetime(2026, 1, 1))
    second = await _agent(mock_db, "Second", gids=("g2", "g3"), created=datetime(2026, 2, 1))
    await _agent(mock_db, "Draft", status="draft", gids=("g4",))
    await claims.ensure_indexes(mock_db)
    for _ in range(2):  # mongomock can't build the partial stock-agent index over two custom agents
        await claims.migrate_claims(mock_db)
    assert await claims.holder(mock_db, U, "g1") == first["_id"]
    assert await claims.holder(mock_db, U, "g2") == first["_id"]  # shared client: first keeps it
    assert await claims.holder(mock_db, U, "g3") == second["_id"]
    assert await claims.holder(mock_db, U, "g4") is None
    assert await mock_db[claims.CLAIMS].count_documents({}) == 3


async def test_startup_runs_the_claims_migration(mock_db, mock_mongo_client):
    a = await _agent(mock_db, gids=("g1",))
    await store.create_agent_indexes(mock_mongo_client)
    assert await claims.holder(mock_db, U, "g1") == a["_id"]


# --- router -----------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    app.dependency_overrides[get_current_user] = lambda: U
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


async def _groups(db, *gids):
    for g in gids:
        await db.client_groups.insert_one({"id": g, "user_id": U, "name": g})


async def test_patch_active_scope_409_then_move(api, mock_db):
    await _groups(mock_db, "g1", "g2")
    a, b = await _agent(mock_db, "Alpha"), await _agent(mock_db, "Beta", gids=())
    await claims.claim_clients(mock_db, U, a["_id"], ["g1"])
    r = api.patch(f"/api/agents/{b['_id']}", json={"scope": {"client_group_ids": ["g1", "g2"]}})
    assert r.status_code == 409
    assert r.json()["conflicts"] == [{"client_group_id": "g1", "holder_id": a["_id"], "holder_name": "Alpha"}]
    assert (await mock_db.agents.find_one({"_id": b["_id"]}))["scope"]["client_group_ids"] == []
    r = api.patch(f"/api/agents/{b['_id']}", json={"scope": {"client_group_ids": ["g1", "g2"]}, "move_claims": True})
    assert r.status_code == 200
    assert await claims.holder(mock_db, U, "g1") == b["_id"]
    assert await claims.holder(mock_db, U, "g2") == b["_id"]


async def test_patch_scope_removing_client_releases_it(api, mock_db):
    await _groups(mock_db, "g1", "g2")
    a = await _agent(mock_db, gids=("g1", "g2"))
    await claims.claim_clients(mock_db, U, a["_id"], ["g1", "g2"])
    assert api.patch(f"/api/agents/{a['_id']}", json={"scope": {"client_group_ids": ["g1"]}}).status_code == 200
    assert await claims.holder(mock_db, U, "g2") is None
    assert await claims.holder(mock_db, U, "g1") == a["_id"]


async def test_patch_draft_scope_claims_nothing(api, mock_db):
    await _groups(mock_db, "g1")
    d = await _agent(mock_db, status="draft", gids=())
    assert api.patch(f"/api/agents/{d['_id']}", json={"scope": {"client_group_ids": ["g1"]}}).status_code == 200
    assert await claims.holder(mock_db, U, "g1") is None


async def test_activate_claims_or_409(api, mock_db, monkeypatch):
    await _groups(mock_db, "g1")
    a = await _agent(mock_db, "Alpha")
    await claims.claim_clients(mock_db, U, a["_id"], ["g1"])
    b = await _agent(mock_db, "Beta", status="draft")

    async def ready(db, agent):
        return {"ready": True, "missing": [], "score": 90}

    monkeypatch.setattr(agents_router, "_confidence", ready)
    r = api.post(f"/api/agents/{b['_id']}/activate")
    assert r.status_code == 409 and r.json()["conflicts"][0]["holder_name"] == "Alpha"
    assert (await mock_db.agents.find_one({"_id": b["_id"]}))["status"] == "draft"
    r = api.post(f"/api/agents/{b['_id']}/activate", json={"move_claims": True})
    assert r.status_code == 200
    assert await claims.holder(mock_db, U, "g1") == b["_id"]


async def test_activate_scope_that_stays_watching_without_claim(api, mock_db, monkeypatch):
    """'Only flag': the user drops the client from claims by moving nothing; scope keeps it only via
    the other agent. Covered by patch 409 leaving scope unchanged; here activation of an unclaimed scope claims it."""
    await _groups(mock_db, "g1")
    b = await _agent(mock_db, "Beta", status="draft")

    async def ready(db, agent):
        return {"ready": True, "missing": [], "score": 90}

    monkeypatch.setattr(agents_router, "_confidence", ready)
    assert api.post(f"/api/agents/{b['_id']}/activate").status_code == 200
    assert await claims.holder(mock_db, U, "g1") == b["_id"]


async def test_runner_forces_flag_for_client_held_by_another_agent(mock_db, mock_mongo_client, monkeypatch):
    import test_agent_runner as tr
    from ai.agents import runner
    from ai.suggestions import store as sstore

    monkeypatch.setattr(runner, "refresh_meta_state", lambda *a, **k: _async({"ads": 0, "adsets": 0}))
    monkeypatch.setattr(runner, "backfill_paused_at", lambda *a, **k: _async(0))

    async def quiet(db, user_id, docs):
        return 0

    monkeypatch.setattr("services.slack_suggestion_notifier.post_new_suggestions", quiet)
    await tr._seed_client(mock_db, "cg1")
    agent = await tr._agent(mock_db, ["cg1"])
    other = await _agent(mock_db, "Other", gids=())
    await claims.claim_clients(mock_db, tr.USER, other["_id"], ["cg1"])
    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=tr.AS_OF)
    assert res.created
    docs = await mock_db[sstore.SUGGESTIONS].find({"rule_mode": {"$exists": True}}).to_list(None)
    assert docs and {d["rule_mode"] for d in docs} == {"flag"}


async def _async(v):
    return v
