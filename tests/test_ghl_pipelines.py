from datetime import datetime, timedelta, timezone

import pytest

from services import ghl_pipelines

PIPELINES = [{"id": "p1", "name": "Sales", "stages": [{"id": "s1", "name": "Spicy Leads"}]}]
EXPECTED = {"s1": {"stage_name": "Spicy Leads", "pipeline_id": "p1", "pipeline_name": "Sales"}}


@pytest.fixture
def patched(monkeypatch):
    calls = {"n": 0, "result": (True, PIPELINES)}

    async def fake_fetch(location_id, access_token):
        calls["n"] += 1
        return calls["result"]

    async def fake_tokens(user_id, mongo_client):
        return {"loc1": {"access_token": "tok"}}

    monkeypatch.setattr(ghl_pipelines.ghl_integration, "fetch_pipelines", fake_fetch)
    monkeypatch.setattr(ghl_pipelines, "get_subaccount_tokens", fake_tokens)
    return calls


async def test_stage_lookup_maps_stage_ids(mock_db, patched):
    lookup = await ghl_pipelines.stage_lookup(mock_db, "u1", "loc1")
    assert lookup == EXPECTED


async def test_stage_lookup_uses_fresh_cache(mock_db, patched):
    await ghl_pipelines.stage_lookup(mock_db, "u1", "loc1")
    again = await ghl_pipelines.stage_lookup(mock_db, "u1", "loc1")
    assert again == EXPECTED
    assert patched["n"] == 1


async def test_pipeline_failure_is_not_fatal(mock_db, patched):
    patched["result"] = (False, [])
    assert await ghl_pipelines.stage_lookup(mock_db, "u1", "loc1") == {}
    assert await mock_db["ghl_pipelines"].count_documents({}) == 0


async def test_stale_cache_used_when_fetch_fails(mock_db, patched):
    await mock_db["ghl_pipelines"].insert_one({
        "location_id": "loc1", "user_id": "u1", "pipelines": PIPELINES,
        "fetched_at": datetime.now(timezone.utc) - timedelta(hours=7),
    })
    patched["result"] = (False, [])
    assert await ghl_pipelines.stage_lookup(mock_db, "u1", "loc1") == EXPECTED
    assert patched["n"] == 1
