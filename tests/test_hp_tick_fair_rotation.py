"""
tests/test_hp_tick_fair_rotation.py
-----------------------------------
hp-tick syncs one client per account at a time and claims the stalest first.
A client whose sync never finishes keeps no last_hp_refresh, so it stayed the
stalest and took the account's only slot on every tick. In Nov 2026 a newly
added client (Sculpt Clinic) was killed by the function timeout on every run,
and its 24 siblings went 12 days without a refresh.

Pinned here:
  * the tick claims the client it tried least recently, so a client that
    never finishes takes turns with the rest instead of starving them
  * a client whose sync fails waits HP_FAILURE_BACKOFF_HOURS before a retry
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routers.cron as cron
import services.hp_service as hp_service

AUTH = {"Authorization": "Bearer s"}
USER = "agency@x.com"
LONG_AGO = datetime.utcnow() - timedelta(days=12)


@pytest.fixture
def tick(mock_db, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s")
    app = FastAPI()
    app.include_router(cron.router)
    tc = TestClient(app)
    return lambda: tc.get("/api/cron/hp-tick", headers=AUTH).json()


async def _seed(db, *, stuck_fields=None):
    # "stuck" never finished a sync, so it has no last_hp_refresh and sorts first.
    await db["client_groups"].insert_one({
        "id": "stuck", "user_id": USER, "ghl_location_id": "loc_stuck",
        "call_log_provider": "hotprospector", "last_hp_refresh": None,
        **(stuck_fields or {}),
    })
    for i in range(3):
        await db["client_groups"].insert_one({
            "id": f"ok{i}", "user_id": USER, "ghl_location_id": f"loc_ok{i}",
            "call_log_provider": "hotprospector", "hp_backfill_status": "complete",
            "hp_refresh_status": "complete", "last_hp_refresh": LONG_AGO + timedelta(minutes=i),
        })


async def test_a_client_killed_mid_sync_does_not_starve_its_siblings(mock_db, monkeypatch, tick):
    synced = []

    async def fake_sync(user_id, loc, mongo, **kw):
        synced.append(loc)
        return {"success": True, "complete": True, "progress": {}, "total_leads": 0, "total_calls": 0}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", fake_sync)
    # The last run on "stuck" was killed by the function timeout: its claim
    # went stale without the tick ever writing a result.
    killed_at = datetime.utcnow() - timedelta(minutes=30)
    await _seed(mock_db, stuck_fields={
        "hp_refresh_status": "running", "_hp_claimed_at": killed_at, "_hp_last_attempt_at": killed_at,
    })

    for _ in range(3):
        tick()

    assert synced == ["loc_ok0", "loc_ok1", "loc_ok2"]


async def test_a_client_that_never_finishes_takes_turns(mock_db, monkeypatch, tick):
    synced = []

    async def fake_sync(user_id, loc, mongo, **kw):
        synced.append(loc)
        if loc == "loc_stuck":  # cut short every time
            return {"success": True, "complete": False, "progress": {"leads_offset": 100},
                    "total_leads": 0, "total_calls": 0}
        return {"success": True, "complete": True, "progress": {}, "total_leads": 0, "total_calls": 0}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", fake_sync)
    await _seed(mock_db)

    for _ in range(5):
        tick()

    # Every sibling got a turn; "stuck" resumes once they are done.
    assert synced[:4] == ["loc_stuck", "loc_ok0", "loc_ok1", "loc_ok2"]
    assert synced[4] == "loc_stuck"


async def test_a_failed_client_backs_off(mock_db, monkeypatch, tick):
    synced = []

    async def fake_sync(user_id, loc, mongo, **kw):
        synced.append(loc)
        if loc == "loc_stuck":
            return {"success": False, "error": "boom", "complete": False, "progress": {}}
        return {"success": True, "complete": True, "progress": {}, "total_leads": 0, "total_calls": 0}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", fake_sync)
    await _seed(mock_db)

    for _ in range(6):
        tick()

    assert synced.count("loc_stuck") == 1
    stuck = await mock_db["client_groups"].find_one({"id": "stuck"})
    assert stuck["hp_refresh_status"] == "error"
    assert stuck["_hp_retry_after"] > datetime.utcnow() + timedelta(hours=cron.HP_FAILURE_BACKOFF_HOURS - 1)

    # Back-off over and the sync works again: retried, and the back-off cleared.
    async def ok_sync(user_id, loc, mongo, **kw):
        synced.append(loc)
        return {"success": True, "complete": True, "progress": {}, "total_leads": 0, "total_calls": 0}

    monkeypatch.setattr(hp_service, "fetch_and_cache_hp_call_center", ok_sync)
    await mock_db["client_groups"].update_one(
        {"id": "stuck"}, {"$set": {"_hp_retry_after": datetime.utcnow() - timedelta(minutes=1)}})
    tick()

    stuck = await mock_db["client_groups"].find_one({"id": "stuck"})
    assert stuck["hp_refresh_status"] == "complete"
    assert "_hp_retry_after" not in stuck
