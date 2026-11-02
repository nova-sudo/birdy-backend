"""
tests/test_ghl_tick_failure_backoff.py
--------------------------------------
A failed GHL sync leaves last_ghl_refresh untouched, and the tick claims the
oldest due groups first. Without a back-off, two groups that always fail
(Oct 2026: locations GHL had deactivated) took both slots on every tick and
starved every other group for a week. A failed group now waits
GHL_FAILURE_BACKOFF_HOURS before it can be claimed again.
"""
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import core.mongo_client
import routers.cron as cron
import services.ghl_service as ghl_service
from core.database import DB_NAME

CRON_SECRET = "test-cron-secret"
AUTH = {"Authorization": f"Bearer {CRON_SECRET}"}
DEAD = {"loc_dead_1", "loc_dead_2"}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", CRON_SECRET)
    client = AsyncMongoMockClient()
    core.mongo_client._client = client
    synced = []

    async def fake_fetch(*, group_id, ghl_location_id, **kw):
        if ghl_location_id in DEAD:
            raise RuntimeError(f"No GHL token for location {ghl_location_id}; reconnect GoHighLevel")
        synced.append(group_id)
        return {"mode": "FULL LOAD", "new": 0, "updated": 0, "pruned": 0}
    monkeypatch.setattr(ghl_service, "fetch_and_cache_ghl_data_optimized", fake_fetch)

    app = FastAPI()
    app.include_router(cron.router)
    yield TestClient(app), client[DB_NAME], synced
    core.mongo_client._client = None


async def _seed(db):
    long_ago = datetime.utcnow() - timedelta(days=6)
    await db["users"].insert_one({"user_id": "agency@x.com"})
    # The two failing groups are the oldest, so they sort first.
    for i, (gid, loc) in enumerate([("dead1", "loc_dead_1"), ("dead2", "loc_dead_2"),
                                     ("ok1", "loc_ok_1"), ("ok2", "loc_ok_2")]):
        await db["client_groups"].insert_one({
            "id": gid, "user_id": "agency@x.com", "name": gid, "ghl_location_id": loc,
            "ghl_refresh_status": "complete",
            "last_ghl_refresh": long_ago + timedelta(minutes=i),
            "last_ghl_full_refresh": long_ago + timedelta(minutes=i),
        })


async def test_failing_groups_do_not_starve_the_rest(env):
    tc, db, synced = env
    await _seed(db)

    first = tc.get("/api/cron/ghl-tick", headers=AUTH).json()
    assert first["claimed"] == 2 and first["succeeded"] == 0

    second = tc.get("/api/cron/ghl-tick", headers=AUTH).json()
    assert second["claimed"] == 2 and second["succeeded"] == 2
    assert sorted(synced) == ["ok1", "ok2"]

    dead = await db["client_groups"].find_one({"id": "dead1"})
    assert dead["ghl_refresh_status"] == "error"
    assert dead["_ghl_retry_after"] > datetime.utcnow() + timedelta(
        hours=cron.GHL_FAILURE_BACKOFF_HOURS - 1)


async def test_failed_group_is_retried_after_the_back_off(env):
    tc, db, synced = env
    await _seed(db)
    tc.get("/api/cron/ghl-tick", headers=AUTH)
    tc.get("/api/cron/ghl-tick", headers=AUTH)

    # Back-off over, and the location works again.
    await db["client_groups"].update_one(
        {"id": "dead1"},
        {"$set": {"_ghl_retry_after": datetime.utcnow() - timedelta(minutes=1),
                  "ghl_location_id": "loc_back_1"}},
    )
    third = tc.get("/api/cron/ghl-tick", headers=AUTH).json()
    assert third["claimed"] == 1 and third["succeeded"] == 1

    healed = await db["client_groups"].find_one({"id": "dead1"})
    assert healed["ghl_refresh_status"] == "complete"
    assert "_ghl_retry_after" not in healed
