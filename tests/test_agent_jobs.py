"""jobs/agent_jobs.py: the agents-tick cron (due logic, freshness, token handling)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ai.agents import runner
from ai.agents.meta_state import MetaTokenError
from ai.agents.store import create_agent, update_agent
from jobs import agent_jobs

USER = "u1"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def _local(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=NY).astimezone(UTC)


async def _seed(db, gids=("cg1",), time="07:00", tz="America/New_York", refreshed=None, last_run_at=None):
    for gid in gids:
        await db.client_groups.insert_one({
            "id": gid, "user_id": USER, "name": gid, "meta_ad_account_id": f"act_{gid}",
            "last_meta_refresh": refreshed,
        })
    agent = await create_agent(db, USER)
    agent = await update_agent(db, USER, agent["_id"], {
        "status": "active",
        "scope": {"client_group_ids": list(gids), "schedule": {"time": time, "tz": tz}},
    })
    if last_run_at is not None:
        await db.agents.update_one({"_id": agent["_id"]}, {"$set": {"last_run_at": last_run_at}})
    return agent


@pytest.fixture
def runs(monkeypatch):
    calls = []
    state = {"result": None, "raise": None}

    async def fake_run(db, mongo_client, agent, *, as_of, mode="live", near_miss=False, force_stale=False):
        calls.append({"as_of": as_of, "force_stale": force_stale})
        if state["raise"]:
            raise state["raise"]
        return state["result"] or runner.RunResult(created=2, stale=force_stale)

    monkeypatch.setattr(agent_jobs, "run_agent", fake_run)
    return calls, state


async def _agent_doc(db, agent):
    return await db.agents.find_one({"_id": agent["_id"]})


async def test_due_at_local_7am_new_york(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    now_ok = _local(2026, 10, 26, 7, 0)
    await _seed(mock_db, refreshed=now_ok.replace(tzinfo=None) - timedelta(minutes=30))
    early = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=_local(2026, 10, 26, 6, 45))
    assert early["ran"] == 0 and calls == []
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=now_ok)
    assert res == {"ran": 1, "skipped": 0, "refreshed": 0, "errors": 0}
    assert calls[0]["as_of"].isoformat() == "2026-10-26" and calls[0]["force_stale"] is False


async def test_not_due_twice_same_local_day(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    t = _local(2026, 10, 26, 7, 0)
    await _seed(mock_db, refreshed=t.replace(tzinfo=None))
    await agent_jobs.run_due_agents(mock_mongo_client, now_utc=t)
    # the fake runner does not stamp last_run_at; emulate the real one
    await mock_db.agents.update_many({}, {"$set": {"last_run_at": t.replace(tzinfo=None)}})
    later = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=_local(2026, 10, 26, 9, 0))
    assert later["ran"] == 0 and len(calls) == 1
    # next local day it is due again
    nxt = _local(2026, 10, 27, 7, 0)
    await mock_db.client_groups.update_many({}, {"$set": {"last_meta_refresh": nxt.replace(tzinfo=None)}})
    assert (await agent_jobs.run_due_agents(mock_mongo_client, now_utc=nxt))["ran"] == 1


async def test_dst_spring_forward_still_runs_once(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    await _seed(mock_db, time="02:30", refreshed=_utc(2027, 3, 14, 6).replace(tzinfo=None))
    # 02:30 does not exist on 2027-03-14 in New York; 01:59 EST is 06:59 UTC
    assert (await agent_jobs.run_due_agents(mock_mongo_client, now_utc=_utc(2027, 3, 14, 6, 59)))["ran"] == 0
    refreshed = _utc(2027, 3, 14, 7).replace(tzinfo=None)
    await mock_db.client_groups.update_many({}, {"$set": {"last_meta_refresh": refreshed}})
    first = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=_utc(2027, 3, 14, 7, 0))
    assert first["ran"] == 1
    await mock_db.agents.update_many({}, {"$set": {"last_run_at": _utc(2027, 3, 14, 7).replace(tzinfo=None)}})
    for h in (8, 12, 20):
        again = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=_utc(2027, 3, 14, h))
        assert again["ran"] == 0
    assert len(calls) == 1


async def test_stale_data_enqueues_refresh_then_runs_stale_after_2h(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    sched = _local(2026, 10, 26, 7, 0)
    await _seed(mock_db, gids=("cg1", "cg2"), refreshed=sched.replace(tzinfo=None) - timedelta(hours=5))
    # 07:30: data 5.5h old, inside the 2h grace -> enqueue a refresh, skip
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=sched + timedelta(minutes=30))
    assert res["ran"] == 0 and res["skipped"] == 1 and res["refreshed"] == 2 and calls == []
    jobs = await mock_db.meta_refresh_jobs.find({}).to_list(length=None)
    assert {j["group_id"] for j in jobs} == {"cg1", "cg2"} and all(j["status"] == "pending" for j in jobs)
    # next tick must not queue duplicates
    again = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=sched + timedelta(minutes=45))
    assert again["refreshed"] == 0 and await mock_db.meta_refresh_jobs.count_documents({}) == 2
    # past the 2h grace: run, forced stale
    late = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=sched + timedelta(hours=2, minutes=1))
    assert late["ran"] == 1 and calls[0]["force_stale"] is True


async def test_token_error_skips_without_items(mock_db, mock_mongo_client, monkeypatch):
    """Real runner: every client hits a token error -> no items, skipped_token."""
    t = _local(2026, 10, 26, 7, 0)
    agent = await _seed(mock_db, gids=("cg1", "cg2"), refreshed=t.replace(tzinfo=None))

    async def boom(*a, **k):
        raise MetaTokenError("expired")

    monkeypatch.setattr(runner, "refresh_meta_state", boom)
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=t)
    assert res["ran"] == 1 and res["errors"] == 0
    doc = await _agent_doc(mock_db, agent)
    assert doc["last_run"]["status"] == "skipped_token" and "at" in doc["last_run"]
    assert await mock_db.suggestions.count_documents({}) == 0


async def test_partial_ok_and_error_statuses(mock_db, mock_mongo_client, runs):
    calls, state = runs
    t = _local(2026, 10, 26, 7, 0)
    agent = await _seed(mock_db, gids=("cg1", "cg2"), refreshed=t.replace(tzinfo=None))
    state["result"] = runner.RunResult(created=3, auto_applied=1, blocked=0, skipped=["meta_token:cg2"])
    await agent_jobs.run_due_agents(mock_mongo_client, now_utc=t)
    lr = (await _agent_doc(mock_db, agent))["last_run"]
    assert lr["status"] == "partial" and lr["skipped"] == ["meta_token:cg2"]

    await mock_db.agents.update_one({"_id": agent["_id"]}, {"$set": {"last_run_at": None, "run_claimed_for": None}})
    state["result"] = runner.RunResult(created=3, auto_applied=1, blocked=2)
    await agent_jobs.run_due_agents(mock_mongo_client, now_utc=t)
    lr = (await _agent_doc(mock_db, agent))["last_run"]
    assert lr["status"] == "ok" and (lr["created"], lr["auto_applied"], lr["blocked"], lr["stale"]) == (3, 1, 2, False)


async def test_one_agent_failing_does_not_stop_others(mock_db, mock_mongo_client, monkeypatch):
    t = _local(2026, 10, 26, 7, 0)
    a1 = await _seed(mock_db, refreshed=t.replace(tzinfo=None))
    a2 = await create_agent(mock_db, "u2")
    await mock_db.client_groups.insert_one({"id": "cgx", "user_id": "u2", "meta_ad_account_id": "act_x",
                                            "last_meta_refresh": t.replace(tzinfo=None)})
    await update_agent(mock_db, "u2", a2["_id"], {
        "status": "active", "scope": {"client_group_ids": ["cgx"], "schedule": {"time": "07:00", "tz": "America/New_York"}}})
    n = {"i": 0}

    async def flaky(db, mc, agent, *, as_of, mode="live", near_miss=False, force_stale=False):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("kaboom")
        return runner.RunResult()

    monkeypatch.setattr(agent_jobs, "run_agent", flaky)
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=t)
    assert res["errors"] == 1 and res["ran"] == 1
    statuses = {d["user_id"]: d.get("last_run", {}).get("status") for d in await mock_db.agents.find({}).to_list(None)}
    assert sorted(statuses.values()) == ["error", "ok"]


async def test_concurrent_ticks_run_agent_once(mock_db, mock_mongo_client, runs):
    import asyncio
    calls, _ = runs
    t = _local(2026, 10, 26, 7, 0)
    await _seed(mock_db, refreshed=t.replace(tzinfo=None))
    results = await asyncio.gather(
        agent_jobs.run_due_agents(mock_mongo_client, now_utc=t),
        agent_jobs.run_due_agents(mock_mongo_client, now_utc=t),
    )
    assert len(calls) == 1
    assert sum(r["ran"] for r in results) == 1


async def test_freshness_skip_does_not_consume_the_day(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    sched = _local(2026, 10, 26, 7, 0)
    await _seed(mock_db, refreshed=sched.replace(tzinfo=None) - timedelta(hours=6))
    first = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=sched + timedelta(minutes=15))
    assert first["ran"] == 0 and calls == []
    later = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=sched + timedelta(hours=2, minutes=1))
    assert later["ran"] == 1 and len(calls) == 1


async def test_bad_stored_schedule_is_skipped_not_run(mock_db, mock_mongo_client, runs):
    calls, _ = runs
    now_ok = _local(2026, 10, 26, 7, 0)
    bad = await _seed(mock_db, tz="America/New_York", refreshed=now_ok.replace(tzinfo=None))
    await mock_db.agents.update_one({"_id": bad["_id"]}, {"$set": {"scope.schedule.tz": "Nope/Zone"}})
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=now_ok)
    assert res["ran"] == 0 and res["skipped"] == 1 and res["errors"] == 0 and calls == []
    assert "last_run" not in await _agent_doc(mock_db, bad)
