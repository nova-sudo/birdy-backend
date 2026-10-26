"""
tests/test_agent_replay.py
--------------------------
Dry run (what the agent would have done over the last 30 days) and practice
case sampling, plus the dry-run job routes. All data is built here through
mock_db inserts; no network.
"""

import asyncio
import contextlib
import json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
from ai.agents import replay
from ai.agents.snapshot import AdDay, AdState
from ai.agents.store import JOBS, create_agent, update_agent
from dependencies import get_current_user
from main import app

USER = "u1"
OTHER = "u2"
END = date(2026, 10, 25)
START = END - timedelta(days=29)  # 2026-09-26


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _row(gid, ad_id, adset_id, campaign_id, day, spend, leads):
    return {
        "client_group_id": gid, "ad_id": ad_id, "adset_id": adset_id, "campaign_id": campaign_id,
        "ad_name": f"Ad {ad_id}", "campaign_name": f"Camp {campaign_id}", "date_start": day.isoformat(),
        "updated_at": datetime(2026, 10, 25),
        "insight_data": {"spend": str(spend), "actions": [{"action_type": "lead", "value": str(leads)}]},
    }


def _days(first: date, last: date):
    d = first
    while d <= last:
        yield d
        d += timedelta(days=1)


async def _group(db, gid, *, target=20, name=None, user=USER):
    await db.client_groups.insert_one({
        "id": gid, "user_id": user, "name": name or f"Client {gid}", "ad_account_currency": "GBP",
        "meta_ad_account_id": f"act_{gid}", "targets": {"cpl": target},
    })


async def _ad(db, gid, ad_id, adset_id, campaign_id, *, status="ACTIVE", created=datetime(2026, 9, 1),
              paused_at=None):
    doc = {"client_group_id": gid, "ad_id": ad_id, "adset_id": adset_id, "campaign_id": campaign_id,
           "name": f"Ad {ad_id}", "effective_status": status, "created_time": created}
    if paused_at is not None:
        doc["paused_at"] = paused_at
    await db.meta_ad_state.insert_one(doc)


async def _adset(db, gid, adset_id, campaign_id, *, status="ACTIVE", budget=50.0):
    await db.meta_adset_state.insert_one({
        "client_group_id": gid, "adset_id": adset_id, "campaign_id": campaign_id, "name": f"Set {adset_id}",
        "effective_status": status, "campaign_status": "ACTIVE", "daily_budget": budget, "lifetime_budget": 0,
    })


async def _spend(db, gid, ad_id, adset_id, campaign_id, first, last, spend, leads):
    await db.facebook_ad_insights.insert_many(
        [_row(gid, ad_id, adset_id, campaign_id, d, spend, leads) for d in _days(first, last)])


async def _agent(db, gids, user=USER):
    agent = await create_agent(db, user)
    return await update_agent(db, user, agent["_id"], {
        "scope": {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "Europe/London"}},
    })


async def _fixture_account(db, gid="g1"):
    """30-day fixture: 2 pauses, 1 revive, 1 no-spend, plus one near miss.

    A1  paused 2026-10-05 (today PAUSED), £30/day 0 leads until 10-04 → pause fires 09-26.
        Saved = 09-27..10-04 = 8 × £30 = £240, 0 leads.
    A2  created 10-10, £10/day 0 leads → £70 per 7 days, never reaches £80: near miss only.
    A3  created 10-15, £50/day 1 lead/day → CPL £50 > £30, patience met 10-16 → fires 10-16.
        Saved = 10-17..10-25 = 9 × £50 = £450, 9 leads.
    R1  paused 09-01, created 08-01, 10 leads at £10 CPL in August → revive fires 10-01.
    N1  in ad set S5 (live, £50 budget), £5/day 1 lead/day until 10-19 → no-spend fires 10-20.
    """
    await _group(db, gid)
    await _adset(db, gid, "S1", "C1", status="PAUSED")
    await _adset(db, gid, "S2", "C2")
    await _adset(db, gid, "S3", "C3")
    await _adset(db, gid, "S4", "C4")
    await _adset(db, gid, "S5", "C5")
    await _ad(db, gid, "A1", "S1", "C1", status="PAUSED", paused_at=datetime(2026, 10, 5))
    await _ad(db, gid, "A2", "S2", "C2", created=datetime(2026, 10, 10))
    await _ad(db, gid, "A3", "S3", "C3", created=datetime(2026, 10, 15))
    await _ad(db, gid, "R1", "S4", "C4", status="PAUSED", created=datetime(2026, 8, 1),
              paused_at=datetime(2026, 9, 1))
    await _ad(db, gid, "N1", "S5", "C5")
    await _spend(db, gid, "A1", "S1", "C1", date(2026, 9, 1), date(2026, 10, 4), 30, 0)
    await _spend(db, gid, "A2", "S2", "C2", date(2026, 10, 10), END, 10, 0)
    await _spend(db, gid, "A3", "S3", "C3", date(2026, 10, 15), END, 50, 1)
    await _spend(db, gid, "R1", "S4", "C4", date(2026, 8, 1), date(2026, 8, 10), 10, 1)
    await _spend(db, gid, "N1", "S5", "C5", date(2026, 9, 1), date(2026, 10, 19), 5, 1)


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------

async def test_dry_run_counts_once_per_target(mock_db, mock_mongo_client):
    await _group(mock_db, "g1")
    await _adset(mock_db, "g1", "S1", "C1")
    await _ad(mock_db, "g1", "A1", "S1", "C1")
    await _spend(mock_db, "g1", "A1", "S1", "C1", date(2026, 9, 1), END, 30, 0)  # fires every day
    agent = await _agent(mock_db, ["g1"])

    res = await replay.dry_run(mock_db, mock_mongo_client, agent, end=END, days=30)

    assert res.actions == 1
    assert res.by_rule["pause_over_target"] == 1
    assert len(res.examples) == 1
    ex = res.examples[0]
    assert ex["day"] == START.isoformat()
    assert ex["rule_kind"] == "pause_over_target"
    assert ex["client_name"] == "Client g1"
    assert ex["title"] and ex["why"]


async def test_spend_saved_sums_after_fire_day(mock_db, mock_mongo_client):
    await _group(mock_db, "g1")
    # Single ad: created 10-15, CPL £50 vs £30 limit, fires 10-16.
    await _adset(mock_db, "g1", "S3", "C3")
    await _ad(mock_db, "g1", "A3", "S3", "C3", created=datetime(2026, 10, 15))
    await _spend(mock_db, "g1", "A3", "S3", "C3", date(2026, 10, 15), END, 50, 1)
    # Two-ad ad set, both 0 leads from 10-20 → ad-set target, fires 10-21 (£80 each, patience £60).
    await _adset(mock_db, "g1", "S9", "C9")
    for ad in ("B1", "B2"):
        await _ad(mock_db, "g1", ad, "S9", "C9", created=datetime(2026, 10, 20))
        await _spend(mock_db, "g1", ad, "S9", "C9", date(2026, 10, 20), END, 40, 0)
    agent = await _agent(mock_db, ["g1"])

    res = await replay.dry_run(mock_db, mock_mongo_client, agent, end=END, days=30)

    assert res.actions == 2
    days = {e["title"]: e["day"] for e in res.examples}
    assert days["Pause “Ad A3”"] == "2026-10-16"
    assert days["Pause “Set S9”"] == "2026-10-21"
    # A3: 10-17..10-25 = 9 × £50, 9 leads. S9: 10-22..10-25 = 4 days × 2 ads × £40.
    assert res.spend_saved == pytest.approx(450 + 320)
    assert res.leads_at_risk == 9


async def test_ad_then_its_adset_fire_no_double_count(mock_db, mock_mongo_client):
    """B1 fires alone on 10-03 and is sim-paused from 10-04. B2 fires on 10-07: with
    B1 simulated off, the ad set has one live ad, so B2 is an ad target — not an
    ad-set target that would re-count B1's spend."""
    await _group(mock_db, "g1")
    await _adset(mock_db, "g1", "S9", "C9")
    await _ad(mock_db, "g1", "B1", "S9", "C9", created=datetime(2026, 10, 1))
    await _ad(mock_db, "g1", "B2", "S9", "C9", created=datetime(2026, 10, 1))
    await _spend(mock_db, "g1", "B1", "S9", "C9", date(2026, 10, 1), END, 30, 0)  # £90 by 10-03
    await _spend(mock_db, "g1", "B2", "S9", "C9", date(2026, 10, 1), END, 12, 0)  # £84 by 10-07
    agent = await _agent(mock_db, ["g1"])

    res = await replay.dry_run(mock_db, mock_mongo_client, agent, end=END, days=30)

    assert res.actions == 2
    assert {(e["title"], e["day"]) for e in res.examples} == {
        ("Pause “Ad B1”", "2026-10-03"), ("Pause “Ad B2”", "2026-10-07")}
    # B1: 10-04..10-25 = 22 × £30. B2: 10-08..10-25 = 18 × £12. Each ad once.
    assert res.spend_saved == pytest.approx(660 + 216)
    assert res.leads_at_risk == 0


def test_simulated_status_layers_over_history():
    base = {"a": AdState(ad_id="a", adset_id="s", campaign_id="c", name="a", effective_status="ACTIVE",
                         created_time=None, paused_at=None),
            "r": AdState(ad_id="r", adset_id="s", campaign_id="c", name="r", effective_status="PAUSED",
                         created_time=None, paused_at=date(2026, 9, 1))}
    sim = {"a": ("PAUSED", date(2026, 10, 5)), "r": ("ACTIVE", date(2026, 10, 5))}
    before = replay.apply_sim(base, sim, date(2026, 10, 4))
    after = replay.apply_sim(base, sim, date(2026, 10, 5))
    assert before == base
    assert after["a"].effective_status == "PAUSED" and after["a"].paused_at == date(2026, 10, 5)
    assert after["r"].effective_status == "ACTIVE" and after["r"].paused_at is None


async def test_dry_run_on_fixture_account_known_totals(mock_db, mock_mongo_client):
    await _fixture_account(mock_db)
    agent = await _agent(mock_db, ["g1"])

    res = await replay.dry_run(mock_db, mock_mongo_client, agent, end=END, days=30)

    assert res.actions == 4
    assert res.by_rule == {"pause_over_target": 2, "revive_winner": 1, "flag_no_spend": 1, "flag_declining": 0}
    assert res.spend_saved == pytest.approx(240 + 450)
    assert res.leads_at_risk == 9
    assert [e["day"] for e in res.examples] == ["2026-10-20", "2026-10-16", "2026-10-01", "2026-09-26"]
    assert [e["rule_kind"] for e in res.examples] == [
        "flag_no_spend", "pause_over_target", "revive_winner", "pause_over_target"]
    json.dumps(res.to_dict())


async def test_dry_run_examples_capped_at_20(mock_db, mock_mongo_client):
    await _group(mock_db, "g1")
    await _adset(mock_db, "g1", "S1", "C1")
    for i in range(25):
        await _ad(mock_db, "g1", f"A{i}", f"S{i}", "C1")
        await _spend(mock_db, "g1", f"A{i}", f"S{i}", "C1", date(2026, 9, 1), END, 30, 0)
    agent = await _agent(mock_db, ["g1"])
    res = await replay.dry_run(mock_db, mock_mongo_client, agent, end=END, days=30)
    assert res.actions == 25 and len(res.examples) == 20


def test_ads_on_day_status_approximation():
    def ad(ad_id, status, paused_at=None):
        return AdState(ad_id=ad_id, adset_id="s", campaign_id="c", name=ad_id, effective_status=status,
                       created_time=None, paused_at=paused_at)

    def row(ad_id, day, spend):
        return AdDay(ad_id=ad_id, adset_id="s", campaign_id="c", ad_name=ad_id, day=day, spend=spend, leads=0)

    d = date(2026, 10, 10)
    ads = {
        "revived": ad("revived", "ACTIVE", paused_at=date(2026, 10, 1)),       # paused_at <= d → PAUSED
        "later": ad("later", "PAUSED", paused_at=date(2026, 10, 20)),          # spent d-1 → ACTIVE
        "unknown": ad("unknown", "PAUSED"),                                     # no spend → stays PAUSED
        "live": ad("live", "ACTIVE"),
    }
    ad_days = {"later": [row("later", d - timedelta(days=1), 5)], "unknown": [row("unknown", d - timedelta(days=5), 5)]}
    out = replay.ads_on_day(ads, ad_days, d)
    assert out["revived"].effective_status == "PAUSED" and out["revived"].paused_at == date(2026, 10, 1)
    assert out["later"].effective_status == "ACTIVE"
    assert out["unknown"].effective_status == "PAUSED"
    assert out["live"] == ads["live"]


# ---------------------------------------------------------------------------
# sample_cases
# ---------------------------------------------------------------------------

def _ads_of(case):
    raw = case["snapshot"]["raw"]
    return set(raw.get("ad_ids") or [case["snapshot"]["target"]["object_id"]])


async def test_sample_cases_deterministic_and_unique_ads(mock_db, mock_mongo_client):
    await _fixture_account(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"])

    a = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=3, seed=7, exclude_ad_ids=set())
    b = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=3, seed=7, exclude_ad_ids=set())
    assert a == b and len(a) == 3
    json.dumps(a)

    every = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=50, seed=1,
                                      exclude_ad_ids=set())
    seen: set = set()
    for c in every:
        assert not (_ads_of(c) & seen)
        seen |= _ads_of(c)
    assert len({c["case_id"] for c in every}) == len(every)
    # Round-robin: the first cases cover distinct rule kinds.
    assert len({c["rule_kind"] for c in every[:3]}) == 3

    excluded = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=50, seed=1,
                                         exclude_ad_ids={"A1", "A3"})
    assert not any(_ads_of(c) & {"A1", "A3"} for c in excluded)

    for c in every:
        assert set(c) == {"case_id", "rule_kind", "client_name", "title", "facts", "options",
                          "birdy_answer", "why", "snapshot"}
        assert set(c["snapshot"]) == {"rule_kind", "client_group_id", "target", "day", "raw", "eval"}
        assert all(set(f) == {"label", "value"} for f in c["facts"])
        if c["rule_kind"] == "revive_winner":
            assert c["options"] == ["Revive", "Leave it"] and c["birdy_answer"] == "Revive"
        if c["rule_kind"] in ("flag_no_spend", "flag_declining"):
            assert c["options"] == ["Flag", "Leave it"] and c["birdy_answer"] == "Flag"


async def test_sample_cases_include_near_miss_with_leave_it(mock_db, mock_mongo_client):
    await _fixture_account(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"])
    cases = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=50, seed=3,
                                      exclude_ad_ids=set())
    near = [c for c in cases if c["snapshot"]["target"]["object_id"] == "A2"]
    assert len(near) == 1
    assert near[0]["birdy_answer"] == "Leave it"
    assert near[0]["options"] == ["Pause", "Leave it", "Flag"]
    fired = [c for c in cases if c["snapshot"]["target"]["object_id"] == "A3"]
    assert fired and fired[0]["birdy_answer"] == "Pause"
    assert fired[0]["snapshot"]["day"] == END.isoformat()  # latest occurrence


# ---------------------------------------------------------------------------
# Job routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    spawned = []
    monkeypatch.setattr(agents_router, "_spawn", lambda coro: spawned.append(coro))
    state = {"user": USER}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    client.spawned = spawned
    yield client
    for coro in spawned:
        coro.close()
    app.dependency_overrides.pop(get_current_user, None)


async def test_dry_run_job_lifecycle(api, mock_db, monkeypatch):
    await _fixture_account(mock_db)
    agent = await _agent(mock_db, ["g1"])
    monkeypatch.setattr(agents_router, "_local_today", lambda agent: END + timedelta(days=1))

    r = api.post(f"/api/agents/{agent['_id']}/dry-run")
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    assert set(r.json()) == {"job_id"}

    r = api.get(f"/api/agents/{agent['_id']}/jobs/{job_id}")
    assert r.status_code == 200 and r.json() == {"status": "queued"}

    # A second POST while it is queued returns the same job and starts nothing.
    assert api.post(f"/api/agents/{agent['_id']}/dry-run").json() == {"job_id": job_id}
    assert len(api.spawned) == 1

    await api.spawned.pop()  # the background task, run to completion here

    body = api.get(f"/api/agents/{agent['_id']}/jobs/{job_id}").json()
    assert body["status"] == "done"
    assert body["result"]["actions"] == 4
    assert set(body["result"]) == {"actions", "by_rule", "spend_saved", "leads_at_risk", "examples"}

    api.state["user"] = OTHER
    assert api.get(f"/api/agents/{agent['_id']}/jobs/{job_id}").status_code == 404
    assert api.post(f"/api/agents/{agent['_id']}/dry-run").status_code == 404


async def test_dry_run_job_failure_recorded(api, mock_db, monkeypatch):
    agent = await _agent(mock_db, [])

    async def boom(*a, **kw):
        raise RuntimeError("kaput")

    monkeypatch.setattr(agents_router.replay, "dry_run", boom)
    job_id = api.post(f"/api/agents/{agent['_id']}/dry-run").json()["job_id"]
    await api.spawned.pop()
    body = api.get(f"/api/agents/{agent['_id']}/jobs/{job_id}").json()
    assert body["status"] == "failed" and body["error"]


async def test_stuck_job_reported_timed_out(api, mock_db):
    agent = await _agent(mock_db, [])
    await mock_db[JOBS].insert_one({"_id": "j1", "agent_id": agent["_id"], "user_id": USER, "kind": "dry_run",
                                    "status": "running", "created_at": datetime.utcnow() - timedelta(minutes=11)})
    body = api.get(f"/api/agents/{agent['_id']}/jobs/j1").json()
    assert body == {"status": "failed", "error": "timed_out"}
    assert (await mock_db[JOBS].find_one({"_id": "j1"}))["status"] == "failed"
    # A job of another agent is a 404 even for the same user.
    await mock_db[JOBS].insert_one({"_id": "j2", "agent_id": "other", "user_id": USER, "kind": "dry_run",
                                    "status": "queued", "created_at": datetime.utcnow()})
    assert api.get(f"/api/agents/{agent['_id']}/jobs/j2").status_code == 404


async def test_post_dry_run_reuses_active_job_but_not_timed_out(api, mock_db):
    agent = await _agent(mock_db, [])
    await mock_db[JOBS].insert_one({"_id": "old", "agent_id": agent["_id"], "user_id": USER, "kind": "dry_run",
                                    "status": "running", "created_at": datetime.utcnow() - timedelta(minutes=11)})
    first = api.post(f"/api/agents/{agent['_id']}/dry-run").json()["job_id"]
    assert first != "old" and len(api.spawned) == 1
    assert api.post(f"/api/agents/{agent['_id']}/dry-run").json()["job_id"] == first
    assert len(api.spawned) == 1


async def test_post_dry_run_starts_new_job_after_rule_edit(api, mock_db):
    agent = await _agent(mock_db, [])
    url = f"/api/agents/{agent['_id']}"
    first = api.post(f"{url}/dry-run").json()["job_id"]
    stored = (await mock_db[JOBS].find_one({"_id": first}))["rules_sig"]
    assert stored and api.post(f"{url}/dry-run").json()["job_id"] == first
    # Editing a threshold while the first job runs: the old answer is stale.
    rid = api.get(f"{url}/rules").json()[0]["id"]
    assert api.patch(f"{url}/rules/{rid}", json={"enabled": False}).status_code == 200
    second = api.post(f"{url}/dry-run").json()["job_id"]
    assert second != first and len(api.spawned) == 2
    assert (await mock_db[JOBS].find_one({"_id": second}))["rules_sig"] != stored
    # The same edited rules rejoin the new job.
    assert api.post(f"{url}/dry-run").json()["job_id"] == second
    assert len(api.spawned) == 2


async def test_dry_run_job_uses_local_yesterday(api, mock_db, monkeypatch):
    agent = await _agent(mock_db, [])
    seen = {}

    async def fake(db, mongo_client, agent, *, end, days=30):
        seen["end"] = end
        return replay.DryRunResult()

    monkeypatch.setattr(agents_router.replay, "dry_run", fake)
    monkeypatch.setattr(agents_router, "_local_today", lambda agent: date(2026, 10, 26))
    api.post(f"/api/agents/{agent['_id']}/dry-run")
    await api.spawned.pop()
    assert seen["end"] == date(2026, 10, 25)


async def test_spawn_keeps_reference_until_done():
    done = []

    async def work():
        done.append(1)

    task = agents_router._spawn(work())
    assert task in agents_router._BACKGROUND
    await task
    await asyncio.sleep(0)  # let the done-callback run
    assert done == [1] and task not in agents_router._BACKGROUND


async def test_case_snapshot_bounded_to_target_neighbourhood(mock_db, mock_mongo_client):
    """A case for an ad in ad set SA stores nothing from ad set SB, keeps at
    most EXACT_DAYS + 1 rows per ad, and still re-scores correctly."""
    await _group(mock_db, "g1")
    await _adset(mock_db, "g1", "SA", "C1")
    await _adset(mock_db, "g1", "SB", "C2")
    await _ad(mock_db, "g1", "A1", "SA", "C1", created=datetime(2026, 4, 1))
    await _ad(mock_db, "g1", "A2", "SA", "C1", created=datetime(2026, 4, 1))
    await _ad(mock_db, "g1", "B1", "SB", "C2", created=datetime(2026, 4, 1))
    await _ad(mock_db, "g1", "B2", "SB", "C2", created=datetime(2026, 4, 1))
    first = date(2026, 4, 1)
    await _spend(mock_db, "g1", "A1", "SA", "C1", first, END, 50, 1)   # £50 CPL > £30: fires
    await _spend(mock_db, "g1", "A2", "SA", "C1", first, END, 10, 1)   # £10 CPL: fine
    await _spend(mock_db, "g1", "B1", "SB", "C2", first, END, 10, 1)
    await _spend(mock_db, "g1", "B2", "SB", "C2", first, END, 10, 1)
    agent = await _agent(mock_db, ["g1"])

    cases = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=20, seed=1,
                                      exclude_ad_ids=set())
    case = next(c for c in cases if c["title"] == "Pause “Ad A1”")
    ev = case["snapshot"]["eval"]
    assert {a["ad_id"] for a in ev["ads"]} == {"A1", "A2"}
    assert {s["adset_id"] for s in ev["adsets"]} == {"SA"}
    assert {r[0] for r in ev["ad_days"]} == {"A1", "A2"}
    assert not any(r[1] == "SB" for r in ev["ad_days"])
    assert all(r[5] <= END.isoformat() for r in ev["ad_days"])
    a1 = [r for r in ev["ad_days"] if r[0] == "A1"]
    assert len(a1) == replay.EXACT_DAYS + 1  # 208 days of history, compacted
    assert a1[0][5] == first.isoformat()  # first spend day survives
    lifetime = (END - first).days + 1
    assert sum(r[6] for r in a1) == 50 * lifetime and sum(r[7] for r in a1) == lifetime
    json.dumps(case)

    from ai.agents.store import list_memory, list_rules
    rule = next(r for r in await list_rules(mock_db, agent["_id"]) if r["kind"] == "pause_over_target")
    mems = await list_memory(mock_db, agent["_id"])
    assert replay.rescore_case(agent, rule, mems, case) == "Pause"
    assert replay.rescore_case(agent, {**rule, "params": {**rule["params"], "cpl_multiple": 3}}, mems,
                               case) == "Leave it"
