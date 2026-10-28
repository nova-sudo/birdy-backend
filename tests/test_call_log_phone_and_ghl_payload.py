"""
tests/test_call_log_phone_and_ghl_payload.py
--------------------------------------------
Call logs must land on the right lead: HP outbound calls store the number
dialled (not the agency's line), and GHL's standard workflow webhook
(location.id + customData) is parsed, owned and timestamped.
"""

from datetime import datetime

from core.database import DB_NAME
from services.call_logs_service import normalize_call_log, save_call_log
from services.hp_service import _hp_lead_number
import scripts.backfill_ghl_call_logs as ghl_backfill

AGENCY, LEAD = "+441234500000", "+447700900123"


def test_hp_outbound_uses_the_number_dialled():
    assert _hp_lead_number({"call_status": "outbound", "from_number": AGENCY, "to_number": LEAD}) == LEAD
    assert _hp_lead_number({"call_status": "Outbound", "from_number": AGENCY, "to_number": ""}) == AGENCY


def test_hp_inbound_uses_the_caller():
    assert _hp_lead_number({"call_status": "inbound", "from_number": LEAD, "to_number": AGENCY}) == LEAD
    assert _hp_lead_number({"from_number": None, "to_number": None}) is None


GHL_STANDARD = {
    "contact_id": "ct1",
    "phone": LEAD,
    "email": "lead@example.com",
    "location": {"id": "loc1", "name": "Clinic"},
    "message": {"type": 1},
    "customData": {
        "recordingUrl": "https://api.twilio.com/rec/RE1",
        "phoneTo": LEAD, "phoneFrom": AGENCY, "direction": "Outbound",
    },
    "workflow": {"id": "wf1", "name": "Call recordings"},
}


def test_ghl_standard_webhook_reads_location_and_custom_data():
    out = normalize_call_log("ghl", GHL_STANDARD)
    assert out["location_id"] == "loc1"
    assert out["contact_id"] == "ct1"
    assert out["contact_phone"] == LEAD
    assert out["direction"] == "outbound"
    assert out["recording_url"] == "https://api.twilio.com/rec/RE1"
    assert out["source_event_id"] is None


def test_ghl_far_end_used_when_contact_phone_missing():
    payload = {**GHL_STANDARD, "phone": "", "customData": {**GHL_STANDARD["customData"], "direction": "inbound",
                                                           "phoneFrom": LEAD, "phoneTo": AGENCY}}
    assert normalize_call_log("ghl", payload)["contact_phone"] == LEAD


def test_ghl_top_level_still_wins():
    out = normalize_call_log("ghl", {**GHL_STANDARD, "locationId": "top-loc", "recordingUrl": "https://x/top"})
    assert out["location_id"] == "top-loc"
    assert out["recording_url"] == "https://x/top"


async def test_save_resolves_owner_and_estimates_time(mock_mongo_client):
    db = mock_mongo_client[DB_NAME]
    await db["client_groups"].insert_one({"user_id": "u1", "ghl_location_id": "loc1"})

    result = await save_call_log("ghl", GHL_STANDARD, {}, mock_mongo_client)

    row = await db["call_logs"].find_one({})
    assert result["user_id"] == "u1"
    assert row["user_id"] == "u1"
    assert row["recording_url"] == "https://api.twilio.com/rec/RE1"
    assert isinstance(row["started_at"], datetime)
    assert row["started_at_estimated"] is True


async def test_ghl_backfill_repairs_orphaned_rows(mock_mongo_client):
    db = mock_mongo_client[DB_NAME]
    await db["client_groups"].insert_one({"user_id": "u1", "ghl_location_id": "loc1"})
    received = datetime(2026, 10, 25, 13, 7, 45)
    await db["call_logs"].insert_many([
        {"source": "ghl", "user_id": None, "raw_payload": GHL_STANDARD, "received_at": received},
        {"source": "ghl", "user_id": None, "raw_payload": {**GHL_STANDARD, "location": {"id": "unknown"}},
         "received_at": received},
    ])

    dry = await ghl_backfill.backfill(mock_mongo_client, apply=False)
    assert dry == {"rows": 2, "owner_found": 1, "with_recording": 1, "updated": 0}
    assert await db["call_logs"].count_documents({"user_id": "u1"}) == 0

    await ghl_backfill.backfill(mock_mongo_client, apply=True)
    row = await db["call_logs"].find_one({"user_id": "u1"})
    assert row["direction"] == "outbound"
    assert row["recording_url"] == "https://api.twilio.com/rec/RE1"
    assert row["started_at"] == received and row["started_at_estimated"] is True
    assert await db["call_logs"].count_documents({"user_id": None}) == 1


# ── one call seen twice: GHL conversation + call_logs ───────────────────────

from services.ghl_messages import drop_logged_calls, is_same_call

GHL_CALL = {"direction": "outbound", "started_at": "2026-10-25T13:00:00+00:00", "duration_seconds": 300}


def test_hp_call_within_a_minute_is_the_same_call():
    assert is_same_call(GHL_CALL, {"direction": "outbound", "started_at": "2026-10-25T13:00:40+00:00"})
    assert not is_same_call(GHL_CALL, {"direction": "outbound", "started_at": "2026-10-25T13:02:00+00:00"})
    assert not is_same_call(GHL_CALL, {"direction": "inbound", "started_at": "2026-10-25T13:00:10+00:00"})


def test_webhook_row_stamped_on_arrival_matches_after_the_call():
    logged = {"direction": "outbound", "started_at": "2026-10-25T13:06:00+00:00", "started_at_estimated": True}
    assert is_same_call(GHL_CALL, logged)  # 6 min later: 5 min call + slack
    assert not is_same_call(GHL_CALL, {**logged, "started_at": "2026-10-25T13:09:00+00:00"})
    assert not is_same_call(GHL_CALL, {**logged, "started_at": "2026-10-25T12:59:00+00:00"})


def test_drop_logged_calls_keeps_unmatched():
    other = {**GHL_CALL, "started_at": "2026-10-26T09:00:00+00:00"}
    logged = [{"direction": "outbound", "started_at": "2026-10-25T13:00:05+00:00"}]
    assert drop_logged_calls([GHL_CALL, other], logged) == [other]


def test_ghl_stamps_the_call_at_its_end():
    # HotProspector logged 13:30 (to the minute), 341s; GHL logged it at 13:36:18.
    hp = {"direction": "outbound", "started_at": "2026-10-27T13:30:00+00:00", "duration_seconds": 341}
    ghl = {"direction": "outbound", "started_at": "2026-10-27T13:36:18+00:00"}
    assert is_same_call(ghl, hp)
    # A call GHL logged well after that one ended is another call.
    assert not is_same_call({**ghl, "started_at": "2026-10-27T13:40:00+00:00"}, hp)
    # Nor one from before it started.
    assert not is_same_call({**ghl, "started_at": "2026-10-27T13:28:30+00:00"}, hp)


# ── HotProspector call times ────────────────────────────────────────────────

from services.hp_service import _hp_started_at, _hp_zone, _persist_hp_calls_to_call_logs


def test_hp_time_is_read_in_the_owner_zone():
    london = _hp_zone(None)
    # British Summer Time: 10:07 in London is 09:07 UTC.
    assert _hp_started_at({"call_time_iso": "2026-10-23T10:07:00"}, london) == datetime(2026, 10, 23, 9, 7)
    # After the clocks go back, London is UTC.
    assert _hp_started_at({"call_time_iso": "2026-10-27T13:30:00"}, london) == datetime(2026, 10, 27, 13, 30)
    assert _hp_started_at({"call_time_iso": "2026-10-23T10:07:00"}, _hp_zone("Africa/Cairo")) == datetime(2026, 10, 23, 7, 7)
    assert _hp_started_at({}, london) is None
    assert _hp_zone("Not/AZone").key == "Europe/London"


async def test_hp_persist_stores_utc(mock_mongo_client):
    db = mock_mongo_client[DB_NAME]
    await db["users"].insert_one({"user_id": "u1", "timezone": "Europe/London"})
    await _persist_hp_calls_to_call_logs(
        [{"recording_id": "r1", "call_time_iso": "2026-10-23T10:07:00", "call_status": "outbound",
          "to_number": LEAD, "duration": 23}],
        user_id="u1", location_id="loc1", mongo_client=mock_mongo_client)
    row = await db["call_logs"].find_one({"source": "hotprospector"})
    assert row["started_at"] == datetime(2026, 10, 23, 9, 7)
