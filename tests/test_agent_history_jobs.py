"""
tests/test_agent_history_jobs.py
--------------------------------
The ad-history sync pass that meta-tick runs: newest-first backfill in 30-day
chunks, the time budget, the 28-day top-up, campaign-scope resets, per-client
error isolation, overlapping ticks, and the meta-tick wiring.
"""

import asyncio
import types
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient

import jobs.agent_history_jobs as jobs_mod
from ai.agents import history
from integrations.facebook_utils.meta_ad_daily import MetaPermissionError

TODAY = date(2026, 10, 27)
D = lambda s: date.fromisoformat(s)  # noqa: E731


class FakeMeta:
    """Stands in for fetch_ad_daily / refresh_meta_state / get_facebook_token."""

    def __init__(self, monkeypatch, *, rows_for=None, fail_for=(), tokens=None, clock_step=0.0):
        self.fetches = []          # (ad_account_id, since, until, campaign_ids)
        self.refreshed = []
        self.clock = [0.0]
        self.clock_step = clock_step
        self.rows_for = rows_for or (lambda acct, since, until: [
            {"ad_id": f"{acct}_ad", "campaign_id": "c1", "spend": "5.00", "date_start": until.isoformat()}])
        self.fail_for = dict(fail_for) if not isinstance(fail_for, dict) else fail_for
        self.tokens = tokens

        async def fetch(ad_account_id, access_token, since, until, campaign_ids=None):
            self.fetches.append((ad_account_id, since, until, campaign_ids))
            await asyncio.sleep(0)
            self.clock[0] += self.clock_step
            exc = self.fail_for.get(ad_account_id)
            if exc == "none":
                return None
            if exc is not None:
                raise exc
            return self.rows_for(ad_account_id, since, until)

        async def refresh(db, mongo_client, user_id, gid, ad_account_id, *, today):
            self.refreshed.append(gid)
            return {}

        async def token(user_id, mongo_client):
            if self.tokens is not None:
                return self.tokens.get(user_id)
            return {"access_token": "tok"}

        monkeypatch.setattr(jobs_mod, "fetch_ad_daily", fetch)
        monkeypatch.setattr(jobs_mod, "refresh_meta_state", refresh)
        monkeypatch.setattr(jobs_mod, "get_facebook_token", token)
        monkeypatch.setattr(jobs_mod, "time", types.SimpleNamespace(monotonic=lambda: self.clock[0]))

    def windows(self, acct=None):
        return [(s, u) for a, s, u, _ in self.fetches if acct is None or a == acct]


async def _watch(db, *groups):
    """Seed client groups and an agent watching all of them."""
    await db.client_groups.insert_many([dict(g) for g in groups])
    await db.agents.insert_one({"_id": "a1", "user_id": "u1", "status": "active",
                                "scope": {"client_group_ids": [g["id"] for g in groups]}})


def _group(gid, acct, user="u1", campaigns=None):
    g = {"id": gid, "user_id": user, "meta_ad_account_id": acct}
    if campaigns is not None:
        g["meta_campaign_ids"] = campaigns
    return g


async def _doc(db, gid):
    return await db[history.HISTORY].find_one({"_id": gid})


async def test_backfill_walks_newest_first_in_30_day_chunks_until_395_days(mock_db, mock_mongo_client,
                                                                          monkeypatch):
    meta = FakeMeta(monkeypatch)
    await _watch(mock_db, _group("g1", "act_1"))

    out = await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)

    w = meta.windows()
    assert len(w) == 14
    assert w[0] == (D("2026-09-28"), D("2026-10-27"))
    assert w[1][1] == D("2026-09-27")
    assert w[-1][0] == D("2025-09-28")
    for (s1, _), (_, u2) in zip(w, w[1:]):
        assert u2 == s1 - timedelta(days=1)      # contiguous, newest first, no gaps
    assert all((u - s).days <= 29 for s, u in w)

    doc = await _doc(mock_db, "g1")
    assert doc["status"] == "ready"
    assert doc["oldest_done"] == "2025-09-28"
    assert doc["newest_done"] == "2026-10-27"
    assert doc["last_topup_at"] is not None
    assert doc["claimed_at"] is None
    assert doc["state_refreshed_at"] is not None and meta.refreshed == ["g1"]
    assert out == {"claimed": 1, "chunks": 14, "topups": 0, "ready": 1, "errors": 0}
    assert await mock_db.facebook_ad_insights.count_documents(
        {"client_group_id": "g1", "source": "agent_history"}) == 14

    # Nothing more to do on the next tick (top-up not due, state fresh).
    again = await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)
    assert again["claimed"] == 0 and len(meta.fetches) == 14


async def test_budget_stops_between_chunks_and_next_tick_resumes(mock_db, mock_mongo_client, monkeypatch):
    meta = FakeMeta(monkeypatch, clock_step=100.0)
    await _watch(mock_db, _group("g1", "act_1"))

    out = await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=150, today=TODAY)
    assert out["chunks"] == 2
    doc = await _doc(mock_db, "g1")
    assert doc["status"] == "running"
    assert doc["claimed_at"] is None
    assert doc["oldest_done"] == "2026-08-29" and doc["newest_done"] == "2026-10-27"

    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=150, today=TODAY)
    assert meta.windows()[2][1] == D("2026-08-28")   # oldest_done - 1 day
    assert len(meta.fetches) == 4


async def test_topup_refetches_last_28_days_only_when_due(mock_db, mock_mongo_client, monkeypatch):
    meta = FakeMeta(monkeypatch)
    now = datetime.utcnow()
    await _watch(mock_db, _group("due", "act_due"), _group("fresh", "act_fresh"))
    base = {"user_id": "u1", "campaign_ids": None, "status": "ready", "oldest_done": "2025-09-28",
            "newest_done": "2026-10-20", "state_refreshed_at": now, "error": None, "claimed_at": None}
    await mock_db[history.HISTORY].insert_many([
        {"_id": "due", "ad_account_id": "act_due", **base, "last_topup_at": now - timedelta(hours=6)},
        {"_id": "fresh", "ad_account_id": "act_fresh", **base, "last_topup_at": now - timedelta(hours=1)},
    ])

    out = await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)

    assert meta.windows() == [(D("2026-09-30"), D("2026-10-27"))]
    assert meta.fetches[0][0] == "act_due"
    assert out["claimed"] == 1 and out["topups"] == 1 and out["chunks"] == 0
    doc = await _doc(mock_db, "due")
    assert doc["status"] == "ready" and doc["newest_done"] == "2026-10-27"
    assert doc["last_topup_at"] > now - timedelta(minutes=1)
    assert doc["oldest_done"] == "2025-09-28" and doc["claimed_at"] is None
    assert meta.refreshed == []                       # state was fresh
    fresh = await _doc(mock_db, "fresh")
    assert fresh["last_topup_at"] < now - timedelta(minutes=59)


async def test_campaign_scope_change_resets_history(mock_db, mock_mongo_client, monkeypatch):
    tag = {"ad": "old_ad"}
    meta = FakeMeta(monkeypatch, clock_step=100.0, rows_for=lambda acct, since, until: [
        {"ad_id": tag["ad"], "campaign_id": "c1", "spend": "5.00", "date_start": until.isoformat()}])
    await _watch(mock_db, _group("g1", "act_shared", campaigns=["c1"]))

    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=150, today=TODAY)
    assert all(c == ["c1"] for *_, c in meta.fetches)
    assert (await _doc(mock_db, "g1"))["oldest_done"] == "2026-08-29"
    await mock_db.facebook_ad_insights.insert_many([
        {"client_group_id": "g1", "ad_id": "x", "date_start": "2026-10-01", "source": "meta_cache"},
        {"client_group_id": "g2", "ad_id": "y", "date_start": "2026-10-01", "source": "agent_history"},
    ])

    # The group now also owns c2 on the shared account.
    await mock_db.client_groups.update_one({"id": "g1"}, {"$set": {"meta_campaign_ids": ["c1", "c2"]}})
    meta.fetches.clear()
    tag["ad"] = "new_ad"
    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=150, today=TODAY)

    # Restarted from today with the new filter; only new-scope rows remain for g1.
    assert meta.fetches[0] == ("act_shared", D("2026-09-28"), D("2026-10-27"), ["c1", "c2"])
    assert all(c == ["c1", "c2"] for *_, c in meta.fetches)
    doc = await _doc(mock_db, "g1")
    assert doc["campaign_ids"] == ["c1", "c2"]
    assert doc["oldest_done"] == "2026-08-29" and doc["status"] == "running"
    hist_rows = await mock_db.facebook_ad_insights.find(
        {"client_group_id": "g1", "source": "agent_history"}).to_list(None)
    assert sorted((r["ad_id"], r["date_start"]) for r in hist_rows) == [
        ("new_ad", "2026-09-27"), ("new_ad", "2026-10-27")]                    # no old-scope rows
    assert await mock_db.facebook_ad_insights.count_documents({"source": "meta_cache"}) == 1
    assert await mock_db.facebook_ad_insights.count_documents({"client_group_id": "g2"}) == 1


async def test_one_client_without_access_does_not_stop_others(mock_db, mock_mongo_client, monkeypatch):
    meta = FakeMeta(monkeypatch, fail_for={"act_1": MetaPermissionError("(#200) no"),
                                           "act_3": RuntimeError("boom")},
                    tokens={"u1": {"access_token": "t"}})
    await _watch(mock_db, _group("g1", "act_1"), _group("g2", "act_2"), _group("g3", "act_3"))
    await mock_db.client_groups.insert_one(_group("g4", "act_4", user="u_no_token"))
    await mock_db.agents.update_one({"_id": "a1"}, {"$push": {"scope.client_group_ids": "g4"}})
    monkeypatch.setattr(jobs_mod.history, "AGENT_HISTORY_GROUPS_PER_TICK", 4)

    out = await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)

    g1, g2, g3, g4 = [await _doc(mock_db, g) for g in ("g1", "g2", "g3", "g4")]
    assert (g1["status"], g1["error"]) == ("error", "no_access")
    assert (g3["status"], g3["error"]) == ("error", "failed")
    assert (g4["status"], g4["error"]) == ("error", "no_token")
    assert g2["status"] == "ready" and g2["error"] is None
    assert len(meta.windows("act_2")) == 14
    assert all(d["claimed_at"] is None for d in (g1, g2, g3, g4))
    assert out["errors"] == 3 and out["ready"] == 1


async def test_failure_keeps_progress_and_none_leaves_running(mock_db, mock_mongo_client, monkeypatch):
    calls = {"n": 0}

    def rows(acct, since, until):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("boom")
        return []

    FakeMeta(monkeypatch, rows_for=rows)
    await _watch(mock_db, _group("g1", "act_1"))
    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)
    doc = await _doc(mock_db, "g1")
    assert (doc["status"], doc["error"], doc["oldest_done"]) == ("error", "failed", "2026-08-29")

    meta = FakeMeta(monkeypatch, fail_for={"act_2": "none"})
    await mock_db.client_groups.insert_one(_group("g2", "act_2"))
    await mock_db.agents.update_one({"_id": "a1"}, {"$push": {"scope.client_group_ids": "g2"}})
    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)
    doc = await _doc(mock_db, "g2")
    assert len(meta.windows("act_2")) == 1
    assert doc["status"] == "running" and doc["oldest_done"] is None and doc["claimed_at"] is None


async def test_state_refresh_failure_does_not_stop_history(mock_db, mock_mongo_client, monkeypatch):
    meta = FakeMeta(monkeypatch)

    async def bad_refresh(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr(jobs_mod, "refresh_meta_state", bad_refresh)
    await _watch(mock_db, _group("g1", "act_1"))
    await jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY)
    assert len(meta.fetches) == 14
    assert (await _doc(mock_db, "g1"))["status"] == "ready"


async def test_overlapping_ticks_never_work_the_same_group(mock_db, mock_mongo_client, monkeypatch):
    meta = FakeMeta(monkeypatch)
    await _watch(mock_db, _group("g1", "act_1"), _group("g2", "act_2"), _group("g3", "act_3"),
                 _group("stale", "act_stale"), _group("busy", "act_busy"))
    await history.ensure_docs(mock_db)
    now = datetime.utcnow()
    await mock_db[history.HISTORY].update_one({"_id": "stale"},
                                              {"$set": {"claimed_at": now - timedelta(minutes=11)}})
    await mock_db[history.HISTORY].update_one({"_id": "busy"},
                                              {"$set": {"claimed_at": now - timedelta(minutes=2)}})

    a, b = await asyncio.gather(
        jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY),
        jobs_mod.advance_agent_history(mock_mongo_client, max_seconds=1000, today=TODAY),
    )

    assert a["claimed"] + b["claimed"] == 4
    for acct in ("act_1", "act_2", "act_3", "act_stale"):
        w = meta.windows(acct)
        assert len(w) == 14 and len(set(w)) == 14       # worked once, by one pass
    assert meta.windows("act_busy") == []               # a fresh claim is left alone
    assert (await _doc(mock_db, "stale"))["status"] == "ready"


# ---------------------------------------------------------------------------
# meta-tick wiring
# ---------------------------------------------------------------------------

def _tick_client(monkeypatch, *, history_fn, schedule_fn=None):
    import services.meta_refresh_manager as mrm
    from main import app

    executed = []

    async def schedule(*a, **k):
        return ["x"]

    async def claim(*a, **k):
        return [{"job_id": "j1"}]

    async def execute(job_id, *a, **k):
        executed.append(job_id)

    monkeypatch.setattr(mrm, "schedule_stale_groups", schedule_fn or schedule)
    monkeypatch.setattr(mrm, "claim_next_jobs", claim)
    monkeypatch.setattr(mrm, "execute_refresh", execute)
    monkeypatch.setattr(jobs_mod, "advance_agent_history", history_fn)
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    return TestClient(app), executed


AUTH = {"Authorization": "Bearer s3cret"}


def test_meta_tick_runs_the_history_pass(mock_mongo_client, monkeypatch):
    seen = {}

    async def fake_history(mongo_client, *, max_seconds, today=None):
        seen["max_seconds"] = max_seconds
        return {"claimed": 1, "chunks": 2, "topups": 0, "ready": 0, "errors": 0}

    client, executed = _tick_client(monkeypatch, history_fn=fake_history)
    assert client.get("/api/cron/meta-tick").status_code == 401
    r = client.get("/api/cron/meta-tick", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["agent_history"] == {"claimed": 1, "chunks": 2, "topups": 0, "ready": 0, "errors": 0}
    assert body["worked"] == 1 and body["scheduled"] == 2 and executed == ["j1"]
    assert seen["max_seconds"] == history.AGENT_HISTORY_BUDGET_SECONDS


def test_meta_tick_survives_a_raising_history_pass(mock_mongo_client, monkeypatch):
    async def boom(mongo_client, *, max_seconds, today=None):
        raise RuntimeError("history broke")

    client, executed = _tick_client(monkeypatch, history_fn=boom)
    r = client.get("/api/cron/meta-tick", headers=AUTH)
    assert r.status_code == 200
    assert "history broke" in r.json()["agent_history"]
    assert r.json()["worked"] == 1 and executed == ["j1"]


def test_meta_tick_history_runs_when_refresh_raises(mock_mongo_client, monkeypatch):
    ran = []

    async def fake_history(mongo_client, *, max_seconds, today=None):
        ran.append(1)
        return {"claimed": 0, "chunks": 0, "topups": 0, "ready": 0, "errors": 0}

    async def bad_schedule(*a, **k):
        raise RuntimeError("refresh broke")

    client, _ = _tick_client(monkeypatch, history_fn=fake_history, schedule_fn=bad_schedule)
    r = client.get("/api/cron/meta-tick", headers=AUTH)
    assert r.status_code == 200
    assert ran == [1] and r.json()["agent_history"]["claimed"] == 0
    assert r.json()["worked"] == 0
