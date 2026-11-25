"""A sub-agent's own runs (ai/team/scheduled.py, schedule.py, the run routes and the tick). The nested
runner with scripted providers and stub tools: no model calls, mongomock only."""

import contextlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import credits
from ai.agents.store import add_memory
from ai.agents.sub_agents import core as sub
from ai.agents.sub_agents import schedule
from ai.team import scheduled
from tests.test_agent_team import TeamProvider, call, isolate, registry, say  # noqa: F401 (fixtures)

U = "owner@example.com"
G1, G2 = "grp-aura", "grp-beaulea"


async def _setup(db, clients=("all",), **over):
    await db["client_groups"].insert_many([{"user_id": U, "id": G1, "name": "Aura"},
                                           {"user_id": U, "id": G2, "name": "Beaulea"}])
    await db["users"].insert_one({"user_id": U, "credits": {**credits._effective_credits({})[0],
                                                             "topup_balance": 1000.0}})
    s = await sub.propose_sub_agent(
        db, U, parent="media_buyer", name="Fatigue", purpose="Spots tired ads",
        instructions="Look at frequency over 7 days.", tools=["get_ad_insights", "get_campaign_insights"],
        clients="all" if clients == ("all",) else list(clients),
        schedule={"time": "08:00", "timezone": "UTC"}, **over)
    await sub.approve_sub_agent(db, U, s["id"])
    return await sub.get_sub_agent(db, U, s["id"])


def _stub(registry, name):
    seen = registry.seen.setdefault(name, [])

    async def run(db, user_id, mongo_client=None, **kw):
        seen.append(kw)
        return {"ads": []}
    registry.register(name=name, description="stub", parameters={"type": "object", "properties": {}}, executor=run)


async def test_run_one_is_a_nested_run_with_its_own_memory_and_charge(mock_db, mock_mongo_client, registry):
    s = await _setup(mock_db, clients=(G1,))
    await add_memory(mock_db, s["id"], text="Ignore ads under $20 spend", kind="lesson", source="chat")
    _stub(registry, "get_ad_insights")
    p = TeamProvider({"sub:Fatigue": [
        call("get_ad_insights", group_ids=[G2]),
        call("propose_action", title="Pause 'Spring' in Aura", detail="Frequency 6", kind="other"),
        say("Two ads are tired.\nSUMMARY: 12 ads checked")]})
    out = await scheduled.run_one(mock_db, mock_mongo_client, s, local_date="2026-11-20", deadline=None,
                                  provider=p, tool_registry=registry)
    assert out["status"] == "done" and out["answer"] == "Two ads are tired." and out["summary"] == "12 ads checked"
    assert out["clients"] == [G1]
    assert registry.seen["get_ad_insights"][-1]["group_ids"] == [G1]      # pinned to its client
    system = p.calls[0][2][0]["content"]
    assert "Ignore ads under $20 spend" in system and "Look at frequency over 7 days." in system
    assert 'Aura" (group ID: grp-aura)' in system
    # its own tools (cut to Marketing Guru's reads) and propose_action; never a write
    assert set(p.offered("sub:Fatigue")) == {"get_ad_insights", "get_campaign_insights", "propose_action"}
    [prop] = out["proposals"]
    assert prop["status"] == "pending" and prop["origin"] == "sub_agent_run" and prop["sub_agent_id"] == s["id"]
    assert prop["agent"] == "media_buyer" and prop["client_group_id"] == G1
    # no chat session, no archive; one ledger row of its own
    assert await mock_db["ai_chat_sessions"].count_documents({}) == 0
    assert await mock_db["ai_conversation_log"].count_documents({}) == 0
    [row] = await mock_db["ai_usage"].find({}).to_list(None)
    assert row["feature"] == "sub_agent_run" and row["agent"] == f"sub:{s['id']}" and row["model_calls"] == 3
    assert out["credits"] == row["credits"] > 0


async def test_run_one_runs_once_per_client_each_pinned(mock_db, mock_mongo_client, registry):
    s = await _setup(mock_db, clients=(G1, G2))
    _stub(registry, "get_ad_insights")
    p = TeamProvider({"sub:Fatigue": [call("get_ad_insights"), say("Aura fine."),
                                      call("get_ad_insights"), say("Beaulea tired.")]})
    out = await scheduled.run_one(mock_db, mock_mongo_client, s, local_date="2026-11-20", deadline=None,
                                  provider=p, tool_registry=registry)
    assert [c["group_ids"] for c in registry.seen["get_ad_insights"]] == [[G1], [G2]]
    assert out["summary"] == "2 of 2 clients checked"
    assert "**Aura**\nAura fine." in out["answer"] and "**Beaulea**\nBeaulea tired." in out["answer"]


async def test_run_one_respects_the_credits_block(mock_db, mock_mongo_client, registry, monkeypatch):
    s = await _setup(mock_db)

    async def blocked(db, user_id):
        return True
    monkeypatch.setattr(credits, "is_blocked", blocked)
    p = TeamProvider({})
    out = await scheduled.run_one(mock_db, mock_mongo_client, s, local_date="2026-11-20", deadline=None,
                                  provider=p, tool_registry=registry)
    assert out["status"] == "skipped" and out["reason"] == "out_of_credits" and p.calls == []


async def test_run_one_is_bounded_by_the_deadline(mock_db, mock_mongo_client, registry):
    s = await _setup(mock_db)
    p = TeamProvider({})
    out = await scheduled.run_one(mock_db, mock_mongo_client, s, local_date="2026-11-20",
                                  deadline=time.monotonic() + 5, provider=p, tool_registry=registry)
    assert out["status"] == "skipped" and out["reason"] == "no_time" and p.calls == []


async def test_the_tick_runs_due_sub_agents_inside_its_deadline(mock_mongo_client, mock_db, registry, monkeypatch):
    import jobs.agent_jobs as agent_jobs
    await sub.create_sub_agent_indexes(mock_mongo_client)
    s = await _setup(mock_db)

    class Ctx:
        async def __aenter__(self):
            return mock_mongo_client

        async def __aexit__(self, *a):
            return False

    async def nothing(*a, **k):
        return {}
    seen = []

    async def run_one(db, mongo_client, sub_agent, *, local_date, deadline):
        seen.append((sub_agent["id"], deadline))
        return {"status": "done", "summary": "ok", "credits": 1.5, "answer": "fine",
                "proposals": [{"id": "p_1", "agent": "media_buyer", "title": "t", "status": "pending",
                               "origin": "sub_agent_run", "sub_agent_id": sub_agent["id"]}]}
    monkeypatch.setattr(agent_jobs, "get_mongo_client", lambda: Ctx())
    for name in ("run_due_agents", "run_due_stock_agents", "retry_unposted_runs", "retry_weeklies"):
        monkeypatch.setattr(agent_jobs, name, nothing)
    monkeypatch.setattr(scheduled, "run_one", run_one)
    monkeypatch.setattr(schedule, "datetime", type("D", (datetime,), {
        "now": staticmethod(lambda tz=None: datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc))}))
    monkeypatch.setattr(agent_jobs, "datetime", schedule.datetime)
    out = await agent_jobs.run_agents_tick()
    assert out["sub_agents"]["ran"] == 1 and seen[0][0] == s["id"] and seen[0][1] is not None
    rec = await sub.to_api(mock_db, U, await sub.get_sub_agent(mock_db, U, s["id"]))
    assert {k: rec["last_run"][k] for k in ("status", "summary", "credits")} == {
        "status": "done", "summary": "ok", "credits": 1.5}
    assert rec["last_run"]["at"] is not None
    row = await mock_db[schedule.RUNS].find_one({})
    assert row["proposals"][0]["status"] == "pending" and "proposals" not in row["result"]


async def test_a_short_tick_defers_the_run(mock_mongo_client, mock_db):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    await _setup(mock_db)

    async def run_one(*a, **k):
        raise AssertionError("must not run")
    out = await schedule.run_due_sub_agents(mock_mongo_client, now_utc=datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc),
                                            run_one=run_one, deadline=time.monotonic() + 10)
    assert out["deferred"] == 1 and await mock_db[schedule.RUNS].count_documents({}) == 0


# ── routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(mock_mongo_client, monkeypatch, registry):
    import routers.sub_agents as sub_router
    from ai import provider_factory
    from dependencies import get_current_user
    from main import app

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client
    monkeypatch.setattr(sub_router, "get_mongo_client", fake_client)
    provider = TeamProvider({})

    async def get_provider(user, db):
        return provider
    monkeypatch.setattr(provider_factory, "get_provider_for_user", get_provider)
    import routers.chat as chat_router
    monkeypatch.setattr(chat_router, "registry", registry)
    app.dependency_overrides[get_current_user] = lambda: U
    client = TestClient(app)
    client.provider = provider
    yield client
    app.dependency_overrides.pop(get_current_user, None)


RUN_KEYS = {"id", "status", "trigger", "started_at", "finished_at", "summary", "answer", "credits", "proposals"}


async def test_run_now_and_runs_list(api, mock_db, mock_mongo_client, registry):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    s = await _setup(mock_db)
    _stub(registry, "get_ad_insights")
    api.provider.scripts["sub:Fatigue"] = [
        call("propose_action", title="Pause 'Spring'", detail="tired", kind="other"),
        say("One ad is tired.\nSUMMARY: 8 ads checked")]
    r = api.post(f"/api/agents/sub/{s['id']}/run")
    assert r.status_code == 200, r.text
    run = r.json()
    assert set(run) == RUN_KEYS and run["status"] == "done" and run["trigger"] == "manual"
    assert run["summary"] == "8 ads checked" and run["answer"] == "One ad is tired." and run["credits"] > 0
    assert run["proposals"][0]["status"] == "pending" and run["proposals"][0]["can_undo"] is False
    runs = api.get(f"/api/agents/sub/{s['id']}/runs", params={"limit": 5}).json()["runs"]
    assert [x["id"] for x in runs] == [run["id"]]
    rec = api.get(f"/api/agents/sub/{s['id']}").json()
    assert rec["last_run"]["status"] == "done" and rec["last_run"]["summary"] == "8 ads checked"
    # a manual run is not the day's scheduled run
    due = await schedule.due_sub_agents(mock_db, datetime.now(timezone.utc).replace(hour=23, minute=59))
    assert [d["_id"] for d, _ in due] == [s["id"]]


async def test_run_now_refusals(api, mock_db, monkeypatch):
    s = await _setup(mock_db)
    proposed = await sub.propose_sub_agent(mock_db, U, parent="media_buyer", name="New", purpose="p",
                                           instructions="i", tools=["get_ad_insights"])
    r = api.post(f"/api/agents/sub/{proposed['id']}/run")
    assert (r.status_code, r.json()["code"]) == (409, "wrong_status")
    assert api.post("/api/agents/sub/nope/run").status_code == 404
    assert api.get("/api/agents/sub/nope/runs").status_code == 404
    await mock_db[schedule.RUNS].insert_one({"_id": "r1", "sub_agent_id": s["id"], "user_id": U, "status": "claimed",
                                             "claimed_at": datetime.utcnow(), "local_date": "x"})
    assert api.post(f"/api/agents/sub/{s['id']}/run").json()["code"] == "already_running"
    assert api.get(f"/api/agents/sub/{s['id']}/runs").json()["runs"][0]["status"] == "running"
    await mock_db[schedule.RUNS].delete_many({})

    async def blocked(db, user_id):
        return True
    monkeypatch.setattr(credits, "is_blocked", blocked)
    r = api.post(f"/api/agents/sub/{s['id']}/run")
    assert (r.status_code, r.json()["code"]) == (402, "out_of_credits")
    assert json.loads(r.text)["detail"]
