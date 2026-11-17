"""
tests/test_hp_sync_writes_only_changes.py
-----------------------------------------
A HotProspector sync used to re-write every stored call (call_logs) and every
lead (hotprospector_leads) on each run, even when it fetched nothing new.
For clients with thousands of calls that took the run past Vercel's 300s, so
it was killed before it saved anything (Nov 2026: The Bodi Genie, 4.6k calls,
Sculpt Clinic, 7.3k). Pinned here: a run writes only what changed.
"""
import pytest

import services.hp_service as hp_service
from tests.test_hp_sync_resume import FakeHP, _group, _raw_call, _raw_lead, _sync


@pytest.fixture
def writes(monkeypatch):
    log = {"leads": [], "calls": []}
    save_leads = hp_service.save_hotprospector_leads_to_collection
    persist_calls = hp_service._persist_hp_calls_to_call_logs

    async def spy_leads(user_id, loc, leads, mongo, **kw):
        log["leads"].append(sorted(str(l["id"]) for l in leads))
        return await save_leads(user_id, loc, leads, mongo, **kw)

    async def spy_calls(calls, **kw):
        log["calls"].append(sorted(c.get("recording_id") for c in calls))
        return await persist_calls(calls, **kw)

    monkeypatch.setattr(hp_service, "save_hotprospector_leads_to_collection", spy_leads)
    monkeypatch.setattr(hp_service, "_persist_hp_calls_to_call_logs", spy_calls)
    return log


LEADS = [_raw_lead(1), _raw_lead(2), _raw_lead(3)]


async def test_a_repeat_sync_with_nothing_new_writes_nothing(mock_mongo_client, mock_db, writes):
    await _group(mock_db)
    await _sync(mock_mongo_client, FakeHP(LEADS, [_raw_call(1, 1), _raw_call(2, 2)]))
    assert writes["leads"][-1] == ["L1", "L2", "L3"]
    assert writes["calls"][-1] == ["R1", "R2"]

    out = await _sync(mock_mongo_client, FakeHP(LEADS, [_raw_call(1, 1), _raw_call(2, 2)]))

    assert out["total_calls"] == 2
    assert writes["leads"][1:] in ([], [[]])
    assert writes["calls"][1:] in ([], [[]])
    assert await mock_db["call_logs"].count_documents({}) == 2


async def test_a_new_call_writes_only_that_call_and_its_lead(mock_mongo_client, mock_db, writes):
    await _group(mock_db)
    await _sync(mock_mongo_client, FakeHP(LEADS, [_raw_call(1, 1)]))

    out = await _sync(mock_mongo_client, FakeHP(LEADS, [_raw_call(1, 1), _raw_call(2, 2)]))

    assert out["total_calls"] == 2
    assert writes["leads"][-1] == ["L2"]
    assert writes["calls"][-1] == ["R2"]
    l2 = await mock_db["hotprospector_leads"].find_one({"lead_data.id": "L2"})
    assert l2["lead_data"]["call_logs_count"] == 1


async def test_a_new_lead_claims_an_unmatched_call(mock_mongo_client, mock_db, writes):
    await _group(mock_db)
    # Call R9 is for lead 9, which HotProspector hasn't returned yet.
    await _sync(mock_mongo_client, FakeHP(LEADS, [_raw_call(1, 1), _raw_call(9, 9)]))
    row = await mock_db["call_logs"].find_one({"raw_payload.recording_id": "R9"})
    assert row["contact_id"] is None

    await _sync(mock_mongo_client, FakeHP(LEADS + [_raw_lead(9)], [_raw_call(1, 1), _raw_call(9, 9)]))

    assert writes["calls"][-1] == ["R9"]                 # re-written with its new contact
    assert "L9" in writes["leads"][-1] and "L1" not in writes["leads"][-1]
    row = await mock_db["call_logs"].find_one({"raw_payload.recording_id": "R9"})
    assert row["contact_id"] == "L9"


async def test_a_changed_lead_is_written(mock_mongo_client, mock_db, writes):
    await _group(mock_db)
    await _sync(mock_mongo_client, FakeHP(LEADS, []))

    renamed = [dict(_raw_lead(1), Firstname="Renamed"), _raw_lead(2), _raw_lead(3)]
    await _sync(mock_mongo_client, FakeHP(renamed, []))

    assert writes["leads"][-1] == ["L1"]
    l1 = await mock_db["hotprospector_leads"].find_one({"lead_data.id": "L1"})
    assert l1["lead_data"]["first_name"] == "Renamed"
