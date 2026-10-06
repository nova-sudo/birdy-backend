"""
HotProspector sync under its rate limit (~5 requests a minute per method).

What went wrong for SOUP: a sync cut short by 429s returned 300 of 967 leads
and 0 calls, was stamped as a complete refresh, and the next run (24 hours
later) only fetched the last few days — while a "backfill" replaced the stored
calls with whatever it had fetched. Pinned here:

  * partial runs are not refreshes, and report where to resume
  * a resumed run skips the call windows and lead pages it already has
  * fetched calls are merged into stored ones, never replace them
  * the cron syncs one client per account at a time
"""
from datetime import date

import pytest

from integrations.hotprospector import HotProspectorIntegration
from services.hp_service import fetch_and_cache_hp_call_center

USER = "agency@x.com"
LOC = "loc1"


def _raw_lead(n):
    return {"LeadId": f"L{n}", "Firstname": f"Lead{n}", "Phone": f"+4477000000{n:02d}", "E-Mail": f"l{n}@x.com"}


def _raw_call(n, lead_n, day="Sep 29, 2026 10:00 am"):
    return {"recordingId": f"R{n}", "leadId": f"L{lead_n}", "to_number": f"+4477000000{lead_n:02d}",
            "from_number": "+441111111111", "call_time": day, "duration": 60, "call_type": "Outbound"}


class FakeHP(HotProspectorIntegration):
    """Real normalisers; the two network methods are scripted."""

    def __init__(self, leads, calls, *, leads_truncated_at=None, failed_windows=()):
        super().__init__("uid", "key")
        self._leads, self._calls = leads, calls
        self._truncate = leads_truncated_at
        self._failed = set(failed_windows)
        self.lead_offsets, self.window_calls = [], []

    async def fetch_all_leads_from_ghl_location(self, ghl_location_id, with_meta=False, start_offset=0, deadline=None):
        self.lead_offsets.append(start_offset)
        end = self._truncate if self._truncate is not None else len(self._leads)
        page = self._leads[start_offset:end]
        truncated = end < len(self._leads)
        return True, {"leads": page, "total_count": len(self._leads), "truncated": truncated,
                      "next_offset": end if truncated else None}

    async def fetch_call_logs_for_location(self, ghl_location_id, lookback_days=400, window_days=30,
                                           skip_windows=None, deadline=None, with_meta=False):
        windows = self.call_windows(lookback_days, window_days)
        skip = set(skip_windows or ())
        done, failed, calls = [], [], []
        for i, (key, _f, _t) in enumerate(windows):
            if i > 0 and key in skip:
                continue
            self.window_calls.append(key)
            if key in self._failed:
                failed.append(key)
                continue
            done.append(key)
            if i == 0:
                calls.extend(self._calls)  # all scripted calls are recent
        if with_meta:
            return True, calls, {"done": done, "failed": failed, "complete": not failed}
        return True, calls


async def _sync(mongo, hp, **kw):
    return await fetch_and_cache_hp_call_center(
        USER, LOC, mongo, integration=hp, location_name="Loc", client_group_name="Client", **kw)


async def _group(db):
    await db["client_groups"].insert_one({"id": "g1", "user_id": USER, "ghl_location_id": LOC,
                                          "call_log_provider": "hotprospector", "last_hp_refresh": None})


# ── call windows ─────────────────────────────────────────────────────────────

def test_call_windows_are_stable_and_newest_first():
    a = HotProspectorIntegration.call_windows(today=date(2026, 10, 6))
    b = HotProspectorIntegration.call_windows(today=date(2026, 10, 7))
    assert a[0][2] == "2026-10-06" and a[0][1] <= "2026-10-06"
    assert [k for k, _, _ in a[1:]] == [k for k, _, _ in b[1:len(a)]]   # same keys next day
    assert all(x[1] > y[1] for x, y in zip(a, a[1:]))                 # newest first
    assert all((date.fromisoformat(t) - date.fromisoformat(f)).days < 30 for _, f, t in a)


async def test_real_window_walk_skips_done_but_always_refetches_the_newest(monkeypatch):
    hp = HotProspectorIntegration("u", "k")
    asked = []

    async def fake_fetch(loc, from_date=None, to_date=None, deadline=None, **kw):
        asked.append(from_date)
        return True, {"call_logs": [], "complete": True}

    monkeypatch.setattr(hp, "fetch_user_call_logs", fake_fetch)
    windows = hp.call_windows()
    keys = [k for k, _, _ in windows]
    ok, _calls, meta = await hp.fetch_call_logs_for_location(LOC, skip_windows=keys, with_meta=True)

    assert len(asked) == 1 and asked[0] == windows[0][1]     # only the newest
    assert meta["complete"] is True


async def test_a_partly_paged_window_is_not_done(monkeypatch):
    hp = HotProspectorIntegration("u", "k")

    async def fake_fetch(loc, from_date=None, to_date=None, deadline=None, **kw):
        return True, {"call_logs": [{"x": 1}], "complete": from_date != hp.call_windows()[1][1]}

    monkeypatch.setattr(hp, "fetch_user_call_logs", fake_fetch)
    ok, calls, meta = await hp.fetch_call_logs_for_location(LOC, with_meta=True)

    assert meta["complete"] is False
    assert hp.call_windows()[1][0] in meta["failed"]


# ── the sync ─────────────────────────────────────────────────────────────────

async def test_a_complete_sync_is_stamped_as_a_refresh(mock_mongo_client, mock_db):
    await _group(mock_db)
    hp = FakeHP([_raw_lead(1), _raw_lead(2)], [_raw_call(1, 1)])

    out = await _sync(mock_mongo_client, hp)

    assert out["complete"] is True and out["progress"] == {}
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["last_hp_refresh"] is not None
    lead = await mock_db["hotprospector_leads"].find_one({"lead_data.id": "L1"})
    assert lead["lead_data"]["call_logs_count"] == 1


async def test_a_cut_short_sync_is_not_a_refresh_and_says_where_to_resume(mock_mongo_client, mock_db):
    await _group(mock_db)
    windows = HotProspectorIntegration.call_windows()
    hp = FakeHP([_raw_lead(n) for n in range(5)], [_raw_call(1, 1)],
                leads_truncated_at=3, failed_windows={windows[2][0]})

    out = await _sync(mock_mongo_client, hp)

    assert out["success"] is True and out["complete"] is False
    assert out["progress"]["leads_offset"] == 3
    assert windows[0][0] in out["progress"]["windows_done"]
    assert windows[2][0] not in out["progress"]["windows_done"]
    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["last_hp_refresh"] is None                       # still stale → retried
    assert g["hotprospector_cache"]["metrics"]["total_calls"] == 1   # but progress is visible


async def test_a_resumed_sync_skips_what_it_has(mock_mongo_client, mock_db):
    await _group(mock_db)
    windows = HotProspectorIntegration.call_windows()
    done = [k for k, _, _ in windows[:-1]]
    hp = FakeHP([_raw_lead(n) for n in range(5)], [])

    await _sync(mock_mongo_client, hp, resume={"leads_offset": 3, "windows_done": done})

    assert hp.lead_offsets == [3]
    assert hp.window_calls == [windows[0][0], windows[-1][0]]  # newest + the one not done


async def test_a_later_partial_run_never_loses_stored_calls(mock_mongo_client, mock_db):
    await _group(mock_db)
    first = FakeHP([_raw_lead(1), _raw_lead(2)], [_raw_call(1, 1), _raw_call(2, 2)])
    await _sync(mock_mongo_client, first)

    # HotProspector rate-limits the next run: no calls come back at all.
    windows = HotProspectorIntegration.call_windows()
    second = FakeHP([_raw_lead(1), _raw_lead(2)], [], failed_windows={windows[0][0]})
    out = await _sync(mock_mongo_client, second)

    assert out["total_calls"] == 2
    l1 = await mock_db["hotprospector_leads"].find_one({"lead_data.id": "L1"})
    assert l1["lead_data"]["call_logs_count"] == 1


async def test_calls_match_leads_saved_by_an_earlier_run(mock_mongo_client, mock_db):
    """A call for a lead fetched last run must not land in 'Unmatched calls'."""
    await _group(mock_db)
    await _sync(mock_mongo_client, FakeHP([_raw_lead(1), _raw_lead(2)], []))

    # This run's lead pages only reach lead 2, but a call for lead 1 arrives.
    hp = FakeHP([_raw_lead(1), _raw_lead(2)], [_raw_call(9, 1)])
    await _sync(mock_mongo_client, hp, resume={"leads_offset": 1})

    l1 = await mock_db["hotprospector_leads"].find_one({"lead_data.id": "L1"})
    assert l1["lead_data"]["call_logs_count"] == 1
    assert await mock_db["hotprospector_leads"].count_documents({"lead_data._is_unmatched_bucket": True}) == 0


# ── the cron ─────────────────────────────────────────────────────────────────

async def test_hp_tick_syncs_one_client_per_account(mock_mongo_client, mock_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.cron as cron
    import services.hp_service as hp_service

    seen = []

    async def fake_sync(user_id, loc, mongo, mode="backfill", resume=None, **kw):
        seen.append((user_id, loc))
        return {"success": True, "complete": True, "progress": {}, "total_leads": 0, "total_calls": 0}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", fake_sync)
    monkeypatch.setenv("CRON_SECRET", "s")
    for user, n in (("a@x.com", 3), ("b@x.com", 1)):
        for i in range(n):
            await mock_db["client_groups"].insert_one({
                "id": f"{user}-{i}", "user_id": user, "ghl_location_id": f"{user}-loc{i}",
                "call_log_provider": "hotprospector", "last_hp_refresh": None})

    app = FastAPI()
    app.include_router(cron.router)
    TestClient(app).get("/api/cron/hp-tick", headers={"Authorization": "Bearer s"})

    users = [u for u, _ in seen]
    assert sorted(users) == ["a@x.com", "b@x.com"]


async def test_hp_tick_records_a_partial_run_for_the_next_tick(mock_mongo_client, mock_db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import routers.cron as cron
    import services.hp_service as hp_service

    async def partial(user_id, loc, mongo, mode="backfill", resume=None, **kw):
        return {"success": True, "complete": False, "progress": {"leads_offset": 300, "windows_done": ["w"]},
                "total_leads": 967, "total_calls": 10}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", partial)
    monkeypatch.setenv("CRON_SECRET", "s")
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": USER, "ghl_location_id": LOC,
        "call_log_provider": "hotprospector", "last_hp_refresh": None})

    app = FastAPI()
    app.include_router(cron.router)
    TestClient(app).get("/api/cron/hp-tick", headers={"Authorization": "Bearer s"})

    g = await mock_db["client_groups"].find_one({"id": "g1"})
    assert g["hp_refresh_status"] == "partial"
    assert g["hp_sync_progress"] == {"leads_offset": 300, "windows_done": ["w"]}
    assert g.get("hp_backfill_status") != "complete"
    assert g["last_hp_refresh"] is None
