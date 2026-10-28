"""
tests/test_lead_journey.py
--------------------------
services/lead_journey.py: live opportunities, and the journey timeline built
from stored data plus live GHL opportunities/appointments. GHLIntegration is
mocked; no network.
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

import services.ghl_live as gl
import services.lead_journey as lj
from services.ghl_live import live_opportunities
from services.lead_journey import build_lead_journey

USER = "u1"
LOC = "loc1"
KEYS = ["email:ann@x.com", "phone:7393421825"]


@pytest.fixture
def ghl(monkeypatch):
    opps = AsyncMock(return_value=(200, []))
    appts = AsyncMock(return_value=(200, []))
    monkeypatch.setattr(gl.ghl_integration, "search_contact_opportunities", opps)
    monkeypatch.setattr(gl.ghl_integration, "fetch_contact_appointments", appts)

    async def tokens(user_id, client):
        return {LOC: {"access_token": "tok"}}

    async def no_stages(db, user_id, location_id):
        return {}

    monkeypatch.setattr(gl, "get_subaccount_tokens", tokens)
    monkeypatch.setattr(lj, "stage_lookup", no_stages)
    return opps, appts


async def _seed(db, contact=None, user_id=USER, group="g1", **extra):
    contact = {"id": "c1", "contactName": "Ann Lee", "email": "ann@x.com", "phone": "07393421825",
               "dateAdded": "2026-07-10T09:00:00Z", **(contact or {})}
    await db["ghl_contacts"].insert_one({
        "user_id": user_id, "location_id": LOC, "client_group_id": group,
        "contact_data": contact, "match_keys": KEYS, **extra})


def _by_kind(journey, kind):
    return [e for e in journey["events"] if e["kind"] == kind]


# --- live_opportunities -----------------------------------------------------

async def test_live_opportunities_ok(mock_db, ghl):
    opps, _ = ghl
    opps.return_value = (200, [{"id": "o1"}, "junk"])
    assert await live_opportunities(mock_db, USER, LOC, "c1") == ("ok", [{"id": "o1"}])
    opps.assert_awaited_once_with(LOC, "tok", "c1")


@pytest.mark.parametrize("code,expected", [(401, "reconnect"), (403, "reconnect"),
                                           (500, "unavailable"), (0, "unavailable"), (404, "unavailable")])
async def test_live_opportunities_status_mapping(mock_db, ghl, code, expected):
    ghl[0].return_value = (code, [])
    assert await live_opportunities(mock_db, USER, LOC, "c1") == (expected, [])


async def test_live_opportunities_no_token_or_location_or_error(mock_db, ghl, monkeypatch):
    assert await live_opportunities(mock_db, USER, None, "c1") == ("unavailable", [])
    assert await live_opportunities(mock_db, USER, "other-loc", "c1") == ("unavailable", [])

    async def boom(user_id, client):
        raise RuntimeError("db down")

    monkeypatch.setattr(gl, "get_subaccount_tokens", boom)
    assert await live_opportunities(mock_db, USER, LOC, "c1") == ("unavailable", [])


# --- ownership ----------------------------------------------------------------

async def test_other_users_contact_is_none(mock_db, ghl):
    await _seed(mock_db, user_id="other")
    assert await build_lead_journey(mock_db, USER, "c1") is None


# --- event kinds, exact copy ---------------------------------------------------

async def test_lead_event_copy_and_fallbacks(mock_db, ghl):
    await _seed(mock_db, {"source": "Facebook form"})
    j = await build_lead_journey(mock_db, USER, "c1")
    assert j == {"events": [{"id": "lead:c1", "kind": "lead", "title": "Lead created",
                             "detail": "Facebook form", "at": "2026-07-10T09:00:00+00:00"}], "ghl": "ok"}


@pytest.mark.parametrize("contact,detail", [
    ({"attributionSource": {"utmSource": "facebook", "campaign": "c"}}, "facebook"),
    ({"attributionSource": {"campaign": "Summer"}}, "Summer"),
    ({}, "GoHighLevel"),
])
async def test_lead_event_detail_fallback(mock_db, ghl, contact, detail):
    await _seed(mock_db, contact)
    assert (await build_lead_journey(mock_db, USER, "c1"))["events"][0]["detail"] == detail


async def test_meta_form_events(mock_db, ghl):
    await _seed(mock_db)
    await mock_db["facebook_leads"].insert_many([
        {"user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
         "lead_data": {"ad_name": "Ad One", "platform": "fb", "created_time": "2026-07-10T08:00:00+0000"}},
        {"user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
         "lead_data": {"ad_name": "Reel", "platform": "ig", "created_time": "2026-07-09T08:00:00+0000"}},
        {"user_id": USER, "client_group_id": "g2", "match_keys": ["email:ann@x.com"],
         "lead_data": {"ad_name": "Other client", "created_time": "2026-07-08T08:00:00+0000"}},
        {"user_id": "other", "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
         "lead_data": {"ad_name": "Other user", "created_time": "2026-07-08T08:00:00+0000"}},
    ])
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "conversion")
    assert [(e["title"], e["detail"], e["at"]) for e in events] == [
        ("Meta lead form submitted", "Meta Ads · Ad One", "2026-07-10T08:00:00+00:00"),
        ("Meta lead form submitted", "Instagram · Reel", "2026-07-09T08:00:00+00:00"),
    ]


async def test_website_form_events_scoped_to_group_and_user(mock_db, ghl):
    await _seed(mock_db)
    base = {"match_keys": ["email:ann@x.com"], "created_at": datetime(2026, 7, 10, 9, 30)}
    await mock_db["tracked_leads"].insert_many([
        {**base, "user_id": USER, "client_group_id": "g1",
         "landing_page": "https://site.com/quote?x=1", "utm_source": "facebook"},
        {**base, "user_id": USER, "client_group_id": "g1", "landing_page": "https://site.com/"},
        {**base, "user_id": USER, "client_group_id": "g2", "landing_page": "https://other.com/x"},
        {**base, "user_id": "other", "client_group_id": "g1", "landing_page": "https://site.com/y"},
    ])
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "conversion")
    assert sorted((e["title"], e["detail"], e["at"]) for e in events) == [
        ("Website form submitted", "/", "2026-07-10T09:30:00+00:00"),
        ("Website form submitted", "/quote · facebook", "2026-07-10T09:30:00+00:00"),
    ]


async def test_visit_events_first_and_paid_touch(mock_db, ghl):
    await _seed(mock_db)
    first = {"ts": datetime(2026, 7, 9, 12, 0), "landing_page": "https://site.com/pricing",
             "referrer": "https://www.google.com/search?q=x"}
    paid = {"ts": datetime(2026, 7, 10, 8, 0), "landing_page": "https://site.com/offer", "utm_source": "facebook"}
    await mock_db["attribution_visitors"].insert_many([
        {"_id": "v1", "user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
         "first_touch": first, "last_paid_touch": paid},
        # no match_keys, joined through attribution_matches; paid == first so only one event
        {"_id": "v2", "user_id": USER, "client_group_id": "g1", "match_keys": [],
         "first_touch": {"ts": datetime(2026, 7, 8, 1, 0), "landing_page": "https://site.com/"},
         "last_paid_touch": {"ts": datetime(2026, 7, 8, 1, 0), "landing_page": "https://site.com/"}},
    ])
    await mock_db["attribution_matches"].insert_one(
        {"user_id": USER, "client_group_id": "g1", "ghl_contact_id": "c1", "visitor_id": "v2"})
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "visit")
    assert [(e["title"], e["detail"], e["at"]) for e in events] == [
        ("Visited /offer", "From facebook", "2026-07-10T08:00:00+00:00"),
        ("Visited /pricing", "From google.com", "2026-07-09T12:00:00+00:00"),
        ("Visited /", "From Direct", "2026-07-08T01:00:00+00:00"),
    ]


async def test_other_clients_visitor_with_same_email_not_shown(mock_db, ghl):
    await _seed(mock_db)
    touch = {"ts": datetime(2026, 7, 9, 12, 0), "landing_page": "https://site.com/pricing"}
    await mock_db["attribution_visitors"].insert_many([
        {"_id": "other-group", "user_id": USER, "client_group_id": "g2",
         "match_keys": ["email:ann@x.com"], "first_touch": touch},
        {"_id": "other-user", "user_id": "other", "client_group_id": "g1",
         "match_keys": ["email:ann@x.com"], "first_touch": touch},
    ])
    await mock_db["attribution_matches"].insert_one(
        {"user_id": USER, "client_group_id": "g2", "ghl_contact_id": "c1", "visitor_id": "other-group"})
    assert _by_kind(await build_lead_journey(mock_db, USER, "c1"), "visit") == []


async def test_contact_without_group_gets_no_site_joins(mock_db, ghl):
    await _seed(mock_db, group=None)
    await mock_db["attribution_visitors"].insert_one(
        {"_id": "v", "user_id": USER, "client_group_id": "g2", "match_keys": ["email:ann@x.com"],
         "first_touch": {"ts": datetime(2026, 7, 9), "landing_page": "https://site.com/"}})
    await mock_db["tracked_leads"].insert_one(
        {"user_id": USER, "client_group_id": "g2", "match_keys": ["email:ann@x.com"],
         "created_at": datetime(2026, 7, 9)})
    assert [e["kind"] for e in (await build_lead_journey(mock_db, USER, "c1"))["events"]] == ["lead"]


async def test_call_events(mock_db, ghl):
    await _seed(mock_db)
    await mock_db["call_logs"].insert_many([
        {"user_id": USER, "contact_id": "c1", "direction": "outbound", "status": "completed",
         "duration_seconds": 125, "started_at": datetime(2026, 7, 11, 10, 0)},
        {"user_id": USER, "contact_id": "c1", "direction": "inbound", "status": "no-answer",
         "duration_seconds": 0, "started_at": datetime(2026, 7, 12, 10, 0)},
    ])
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "call")
    assert [(e["title"], e["detail"], e["at"], e.get("duration_seconds")) for e in events] == [
        ("Inbound no answer", "Call", "2026-07-12T10:00:00+00:00", None),
        ("Outbound completed · 2:05", "Call", "2026-07-11T10:00:00+00:00", 125),
    ]


async def test_appointment_events(mock_db, ghl):
    await _seed(mock_db)
    ghl[1].return_value = (200, [
        {"id": "a1", "title": "Site visit", "appointmentStatus": "confirmed",
         "startTime": "2026-07-20 10:00:00", "dateAdded": "2026-07-11T08:00:00Z"},
        {"id": "a2", "title": "Call back", "appointmentStatus": "cancelled",
         "startTime": "2026-07-21T10:00:00Z", "dateAdded": "2026-07-11T09:00:00Z",
         "dateUpdated": "2026-07-12T09:00:00Z"},
        {"id": "a3", "title": "Survey", "appointmentStatus": "noshow",
         "startTime": "2026-07-22T10:00:00Z", "dateAdded": "2026-07-11T10:00:00Z"},
    ])
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "appointment")
    assert [(e["title"], e["detail"], e["at"], e["starts_at"], e["starts_at_local"]) for e in events] == [
        ("Appointment cancelled", "Call back", "2026-07-12T09:00:00+00:00", "2026-07-21T10:00:00+00:00", False),
        ("Appointment no-show", "Survey", "2026-07-11T10:00:00+00:00", "2026-07-22T10:00:00+00:00", False),
        ("Appointment booked", "Site visit", "2026-07-11T08:00:00+00:00", "2026-07-20T10:00:00", True),
    ]


async def test_stage_events(mock_db, ghl, monkeypatch):
    await _seed(mock_db)
    async def stages(db, user_id, location_id):
        return {"s1": {"stage_name": "Booked", "pipeline_id": "p1", "pipeline_name": "Sales"}}

    monkeypatch.setattr(lj, "stage_lookup", stages)
    ghl[0].return_value = (200, [
        {"id": "o1", "name": "Ann deal", "status": "won", "pipelineId": "p1", "pipelineStageId": "s1",
         "createdAt": "2026-07-11T00:00:00Z", "lastStatusChangeAt": "2026-07-15T00:00:00Z",
         "lastStageChangeAt": "2026-07-13T00:00:00Z"},
        {"id": "o2", "status": "open", "pipelineStageId": "s1",
         "createdAt": "2026-07-12T00:00:00Z", "lastStatusChangeAt": "2026-07-12T00:00:00Z",
         "lastStageChangeAt": "2026-07-12T00:00:00Z"},
    ])
    events = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "stage")
    assert [(e["title"], e["detail"], e["at"]) for e in events] == [
        ("Opportunity won", "Ann deal", "2026-07-15T00:00:00+00:00"),
        ("Moved to Booked", "Sales", "2026-07-13T00:00:00+00:00"),
        ("Opportunity created", "Sales · Booked", "2026-07-12T00:00:00+00:00"),
        ("Opportunity created", "Sales · Booked", "2026-07-11T00:00:00+00:00"),
    ]


# --- ordering, dropping, degradation -------------------------------------------

async def test_sorted_desc_and_unparseable_dates_dropped(mock_db, ghl):
    await _seed(mock_db)
    ghl[1].return_value = (200, [
        {"id": "bad", "title": "x", "dateAdded": "not a date"},
        {"id": "none", "title": "y"},
        {"id": "ok", "title": "z", "dateAdded": "2026-07-12T00:00:00Z"}])
    await mock_db["call_logs"].insert_one(
        {"user_id": USER, "contact_id": "c1", "direction": "outbound", "status": "completed",
         "started_at": "garbage"})
    j = await build_lead_journey(mock_db, USER, "c1")
    assert [e["id"] for e in j["events"]] == ["appt:ok", "lead:c1"]
    assert all(e["at"].endswith("+00:00") and not e["at"].startswith("1970") for e in j["events"])


async def test_ghl_status_is_the_worse_of_the_two_calls(mock_db, ghl):
    await _seed(mock_db)
    ghl[0].return_value = (500, [])
    assert (await build_lead_journey(mock_db, USER, "c1"))["ghl"] == "unavailable"
    ghl[1].return_value = (401, [])
    assert (await build_lead_journey(mock_db, USER, "c1"))["ghl"] == "reconnect"
    ghl[0].return_value = (403, [])
    ghl[1].return_value = (500, [])
    assert (await build_lead_journey(mock_db, USER, "c1"))["ghl"] == "reconnect"


async def test_live_failure_keeps_stored_events(mock_db, ghl):
    await _seed(mock_db)
    ghl[0].return_value = (0, [])
    ghl[1].return_value = (0, [])
    await mock_db["call_logs"].insert_one(
        {"user_id": USER, "contact_id": "c1", "direction": "outbound", "status": "completed",
         "started_at": datetime(2026, 7, 11, 10, 0)})
    j = await build_lead_journey(mock_db, USER, "c1")
    assert j["ghl"] == "unavailable"
    assert [e["kind"] for e in j["events"]] == ["call", "lead"]


async def test_no_location_means_unavailable(mock_db, ghl):
    await _seed(mock_db, location_id=None)
    j = await build_lead_journey(mock_db, USER, "c1")
    assert j["ghl"] == "unavailable"
    ghl[0].assert_not_awaited()
    ghl[1].assert_not_awaited()


@pytest.mark.parametrize("start,expected", [
    ("2026-07-20 10:00:00", ("2026-07-20T10:00:00", True)),
    ("2026-07-20T10:00:00", ("2026-07-20T10:00:00", True)),
    ("2026-07-20T10:00:00Z", ("2026-07-20T10:00:00+00:00", False)),
    ("2026-07-20T10:00:00+01:00", ("2026-07-20T10:00:00+01:00", False)),
])
async def test_appointment_start_wall_clock(mock_db, ghl, start, expected):
    await _seed(mock_db)
    ghl[1].return_value = (200, [{"id": "a", "title": "t", "startTime": start, "dateAdded": "2026-07-11 08:00:00"}])
    (e,) = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "appointment")
    assert (e["starts_at"], e["starts_at_local"]) == expected
    assert e["at"] == "2026-07-11T08:00:00+00:00"  # naive dateAdded is read as UTC


@pytest.mark.parametrize("start", ["garbage", None, ""])
async def test_appointment_bad_start_omits_fields(mock_db, ghl, start):
    await _seed(mock_db)
    ghl[1].return_value = (200, [{"id": "a", "title": "t", "startTime": start, "dateAdded": "2026-07-11T08:00:00Z"}])
    (e,) = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "appointment")
    assert "starts_at" not in e and "starts_at_local" not in e


async def test_appointment_without_id_gets_index_ids(mock_db, ghl):
    await _seed(mock_db)
    ghl[1].return_value = (200, [{"title": "a", "dateAdded": "2026-07-11T08:00:00Z"},
                                 {"title": "b", "dateAdded": "2026-07-12T08:00:00Z"}])
    ids = [e["id"] for e in _by_kind(await build_lead_journey(mock_db, USER, "c1"), "appointment")]
    assert sorted(ids) == ["appt:0", "appt:1"]


async def test_moved_event_needs_a_real_gap(mock_db, ghl, monkeypatch):
    await _seed(mock_db)
    ghl[0].return_value = (200, [{"id": "o", "status": "open", "createdAt": "2026-07-11T00:00:00Z",
                                  "lastStageChangeAt": "2026-07-11T00:00:30Z"}])
    titles = [e["title"] for e in _by_kind(await build_lead_journey(mock_db, USER, "c1"), "stage")]
    assert titles == ["Opportunity created"]


async def test_call_status_labels(mock_db, ghl):
    await _seed(mock_db)
    await mock_db["call_logs"].insert_many([
        {"user_id": USER, "contact_id": "c1", "direction": "outbound", "status": s,
         "started_at": datetime(2026, 7, 11, 10, i)} for i, s in enumerate(["no-answer", "busy", "failed", "canceled", "completed"])])
    titles = [e["title"] for e in _by_kind(await build_lead_journey(mock_db, USER, "c1"), "call")]
    assert titles == ["Outbound completed", "Outbound cancelled", "Outbound failed", "Outbound busy", "Outbound no answer"]


async def test_schemeless_landing_page_path(mock_db, ghl):
    await _seed(mock_db)
    await mock_db["tracked_leads"].insert_one(
        {"user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
         "created_at": datetime(2026, 7, 10, 9, 30), "landing_page": "site.com/quote"})
    (e,) = _by_kind(await build_lead_journey(mock_db, USER, "c1"), "conversion")
    assert e["detail"] == "/quote"


async def test_token_fetched_once_for_both_live_calls(mock_db, ghl, monkeypatch):
    await _seed(mock_db)
    calls = []

    async def tokens(user_id, client):
        calls.append(1)
        return {LOC: {"access_token": "tok"}}

    monkeypatch.setattr(gl, "get_subaccount_tokens", tokens)
    await build_lead_journey(mock_db, USER, "c1")
    assert len(calls) == 1
    ghl[0].assert_awaited_once()
    ghl[1].assert_awaited_once()
