from datetime import datetime, timezone

import pytest

from services import lead_detail
from services.lead_detail import build_lead_detail

USER = "u1"


@pytest.fixture(autouse=True)
def _no_ghl(monkeypatch):
    async def _empty(db, user_id, location_id):
        return {}

    async def _no_live(db, user_id, location_id, contact_id):
        return "unavailable", []

    monkeypatch.setattr("services.lead_detail.stage_lookup", _empty)
    monkeypatch.setattr("services.lead_detail.live_opportunities", _no_live)


async def _seed_contact(db, contact=None, user_id=USER, **extra):
    contact = {"id": "c1", "contactName": "Ann Lee", "email": "ann@x.com", "phone": "07393421825", **(contact or {})}
    doc = {"user_id": user_id, "location_id": "loc1", "client_group_id": "g1",
           "client_group_name": "Group One", "contact_data": contact,
           "match_keys": ["email:ann@x.com", "phone:7393421825"], **extra}
    await db["ghl_contacts"].insert_one(doc)


@pytest.mark.asyncio
async def test_returns_none_for_other_users_contact(mock_db):
    await _seed_contact(mock_db, user_id="other")
    assert await build_lead_detail(mock_db, USER, "c1") is None


@pytest.mark.asyncio
async def test_bare_contact_renders_nulls(mock_db):
    await _seed_contact(mock_db)
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["contact"]["name"] == "Ann Lee"
    assert isinstance(r["ghl"], str)  # opportunities source status
    assert r["contact"]["group_name"] == "Group One"
    assert r["opportunities"] == []
    assert r["attribution"] is None
    assert r["calls"] == []
    assert r["engagement"] == {"dials": 0, "calls": 0, "answered": 0,
                               "time_to_close_seconds": None, "touchpoints": 0}


@pytest.mark.asyncio
async def test_stage_name_resolved(mock_db, monkeypatch):
    async def _lookup(db, user_id, location_id):
        return {"s1": {"stage_name": "Spicy Leads", "pipeline_id": "p1", "pipeline_name": "Main"}}

    monkeypatch.setattr("services.lead_detail.stage_lookup", _lookup)
    await _seed_contact(mock_db, {"opportunities": [
        {"id": "o1", "pipelineId": "p1", "pipelineStageId": "s1", "status": "open", "monetaryValue": 5}]})
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["opportunities"][0]["stage_name"] == "Spicy Leads"
    assert r["opportunities"][0]["pipeline_name"] == "Main"


@pytest.mark.asyncio
async def test_meta_attribution_with_creative_and_form_answers(mock_db):
    await _seed_contact(mock_db)
    await mock_db["facebook_leads"].insert_one({
        "user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
        "lead_data": {"ad_id": "a1", "ad_name": "Ad One", "adset_id": "as1", "adset_name": "Set",
                      "campaign_id": "c9", "campaign_name": "Camp", "created_time": "2026-07-17T10:00:00+0000",
                      "platform": "fb", "is_organic": False,
                      "field_data": {"budget": "£100–150"}}})
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": USER,
        "facebook_cache": {"entities": {"ads": [
            {"id": "a1", "name": "Ad One", "creative_title": "T", "creative_body": "Body text",
             "creative_image": "img", "creative_thumbnail": "thumb"}]}}})
    a = (await build_lead_detail(mock_db, USER, "c1"))["attribution"]
    assert a["ad_name"] == "Ad One"
    assert a["campaign_name"] == "Camp"
    assert a["creative_body"] == "Body text"
    assert a["creative_thumbnail"] == "thumb"
    assert a["form_answers"] == [{"question": "budget", "answer": "£100–150"}]
    assert a["submitted_at"] .endswith("+00:00")
    assert a["submitted_at"].startswith("2026-07-17T10:00:00")


@pytest.mark.asyncio
async def test_attribution_falls_back_to_attribution_source(mock_db):
    await _seed_contact(mock_db, {"attributionSource": {"adId": "a9", "campaign": "C"}})
    a = (await build_lead_detail(mock_db, USER, "c1"))["attribution"]
    assert a["ad_id"] == "a9"
    assert a["campaign_name"] == "C"
    assert a["form_answers"] == []


@pytest.mark.asyncio
async def test_calls_match_by_id_phone_or_email(mock_db):
    base = {"user_id": USER, "source": "ghl", "direction": "outbound", "status": "completed", "duration_seconds": 30}
    await mock_db["call_logs"].insert_many([
        {**base, "contact_id": "c1", "started_at": datetime(2026, 7, 1, tzinfo=timezone.utc)},
        {**base, "contact_phone": "+44 7393 421825", "direction": "inbound", "status": "missed",
         "started_at": datetime(2026, 7, 3, tzinfo=timezone.utc), "transcript": "hi"},
        {**base, "contact_email": "ANN@x.com", "started_at": datetime(2026, 7, 2, tzinfo=timezone.utc)},
        {**base, "contact_phone": "07000000000", "started_at": datetime(2026, 7, 4, tzinfo=timezone.utc)},
        {**base, "user_id": "other", "contact_id": "c1", "started_at": datetime(2026, 7, 5, tzinfo=timezone.utc)},
    ])
    await _seed_contact(mock_db)
    r = await build_lead_detail(mock_db, USER, "c1")
    assert len(r["calls"]) == 3
    assert [c["started_at"][:10] for c in r["calls"]] == ["2026-07-03", "2026-07-02", "2026-07-01"]
    assert r["calls"][0]["transcript"] == "hi"
    assert "transcript" not in r["calls"][1]
    assert r["engagement"] == {"dials": 2, "calls": 3, "answered": 2,
                               "time_to_close_seconds": None, "touchpoints": 3}


@pytest.mark.asyncio
async def test_time_to_close_for_won_opportunity(mock_db):
    await _seed_contact(mock_db, {"dateAdded": "2026-07-17T12:00:00Z", "opportunities": [
        {"id": "o1", "status": "won", "lastStatusChangeAt": "2026-07-17T14:34:00Z"}]})
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["engagement"]["time_to_close_seconds"] == 9240


@pytest.mark.asyncio
async def test_time_to_close_none_without_contact_date_added(mock_db):
    await _seed_contact(mock_db, {"opportunities": [
        {"id": "o1", "status": "won", "dateAdded": "2026-07-17T12:00:00Z",
         "lastStatusChangeAt": "2026-07-17T14:34:00Z"}]})
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["engagement"]["time_to_close_seconds"] is None


@pytest.mark.asyncio
async def test_naive_started_at_serialised_as_utc(mock_db):
    await mock_db["call_logs"].insert_one({
        "user_id": USER, "contact_id": "c1", "direction": "outbound", "status": "completed",
        "started_at": datetime(2026, 7, 1, 9, 30)})
    await _seed_contact(mock_db)
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["calls"][0]["started_at"].endswith("+00:00")


async def _seed_lead(db, **over):
    doc = {"user_id": USER, "client_group_id": "g1", "match_keys": ["email:ann@x.com"],
           "lead_data": {"ad_id": "a1", "created_time": "2026-07-17T10:00:00+0000"}}
    doc.update(over)
    await db["facebook_leads"].insert_one(doc)


@pytest.mark.asyncio
async def test_field_data_list_shape_still_supported(mock_db):
    await _seed_contact(mock_db)
    await _seed_lead(mock_db, lead_data={"ad_id": "a1", "created_time": "2026-07-17T10:00:00+0000",
                                         "field_data": [{"name": "budget", "values": ["100"]}]})
    a = (await build_lead_detail(mock_db, USER, "c1"))["attribution"]
    assert a["form_answers"] == [{"question": "budget", "answer": "100"}]


@pytest.mark.asyncio
async def test_names_resolved_from_ad_cache_over_utm(mock_db):
    await _seed_contact(mock_db, {"attributionSource": {"utmContent": "utm-ad", "utmMedium": "utm-set",
                                                        "campaign": "utm-camp"}})
    await _seed_lead(mock_db, lead_data={"ad_id": 123, "ad_name": "", "adset_name": "", "campaign_name": "",
                                         "created_time": "2026-07-17T10:00:00+0000"})
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": USER,
        "facebook_cache": {"entities": {
            "ads": [{"id": "123", "name": "Cached Ad", "adset_id": "s1", "campaign_id": "k1"}],
            "adsets": [{"id": "s1", "name": "Cached Set", "campaign_id": "k1"}]},
            "maximum": {"campaigns": [{"id": "k1", "name": "Cached Camp"}]}}})
    a = (await build_lead_detail(mock_db, USER, "c1"))["attribution"]
    assert a["ad_name"] == "Cached Ad"
    assert a["adset_name"] == "Cached Set"
    assert a["campaign_name"] == "Cached Camp"
    assert a["campaign_id"] == "k1"


@pytest.mark.asyncio
async def test_meta_lead_in_other_client_group_ignored(mock_db):
    await _seed_contact(mock_db)
    await _seed_lead(mock_db, client_group_id="g2",
                     lead_data={"ad_id": "zz", "created_time": "2026-07-17T10:00:00+0000"})
    assert (await build_lead_detail(mock_db, USER, "c1"))["attribution"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("group", ["missing", None, ""])
async def test_ungrouped_meta_lead_still_matches(mock_db, group):
    await _seed_contact(mock_db)
    doc = {"user_id": USER, "match_keys": ["email:ann@x.com"],
           "lead_data": {"ad_id": "loose", "created_time": "2026-07-17T10:00:00+0000"}}
    if group != "missing":
        doc["client_group_id"] = group
    await mock_db["facebook_leads"].insert_one(doc)
    assert (await build_lead_detail(mock_db, USER, "c1"))["attribution"]["ad_id"] == "loose"


@pytest.mark.asyncio
async def test_newest_meta_lead_wins(mock_db):
    await _seed_contact(mock_db)
    await _seed_lead(mock_db, lead_data={"ad_id": "old", "created_time": "2026-07-01T10:00:00+0000"})
    await _seed_lead(mock_db, lead_data={"ad_id": "new", "created_time": "2026-07-20T10:00:00+0000"})
    assert (await build_lead_detail(mock_db, USER, "c1"))["attribution"]["ad_id"] == "new"


@pytest.mark.asyncio
async def test_no_stage_lookup_without_location(mock_db, monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("stage_lookup must not be called")

    monkeypatch.setattr("services.lead_detail.stage_lookup", _boom)
    await _seed_contact(mock_db)
    await mock_db["ghl_contacts"].update_one({"user_id": USER}, {"$unset": {"location_id": ""}})
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["opportunities"] == []


@pytest.mark.asyncio
async def test_slow_ghl_falls_back_to_stored_opportunities(mock_db, monkeypatch):
    import asyncio

    async def _slow(*a, **k):
        await asyncio.sleep(5)
        return "ok", [{"id": "live"}]

    async def _slow_stages(*a, **k):
        await asyncio.sleep(5)
        return {"s1": {"stage_name": "X", "pipeline_id": "p", "pipeline_name": "P"}}

    monkeypatch.setattr(lead_detail, "GHL_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr("services.lead_detail.live_opportunities", _slow)
    monkeypatch.setattr("services.lead_detail.stage_lookup", _slow_stages)
    await _seed_contact(mock_db, {"opportunities": [
        {"id": "stored", "pipelineStageId": "s1", "status": "open", "monetaryValue": 5}]})
    r = await asyncio.wait_for(build_lead_detail(mock_db, USER, "c1"), 2)
    assert [o["id"] for o in r["opportunities"]] == ["stored"]


@pytest.mark.asyncio
async def test_parse_dt_out_of_range_number_is_none():
    assert lead_detail._parse_dt(1e18) is None
    assert lead_detail._parse_dt(float("inf")) is None


def _live(monkeypatch, status, rows=()):
    async def _fn(db, user_id, location_id, contact_id):
        return status, list(rows)

    monkeypatch.setattr("services.lead_detail.live_opportunities", _fn)


STORED = {"opportunities": [{"id": "old", "pipelineStageId": "s1", "status": "won", "monetaryValue": 9}],
          "dateAdded": "2026-07-01T00:00:00Z"}


@pytest.mark.asyncio
async def test_live_opportunities_used_with_stage_names_and_time_to_close(mock_db, monkeypatch):
    async def _lookup(db, user_id, location_id):
        return {"s2": {"stage_name": "Won Deals", "pipeline_id": "p1", "pipeline_name": "Main"}}

    monkeypatch.setattr("services.lead_detail.stage_lookup", _lookup)
    _live(monkeypatch, "ok", [{
        "id": "o1", "name": "Ann deal", "status": "won", "pipelineId": "p1", "pipelineStageId": "s2",
        "monetaryValue": 500, "createdAt": "2026-07-02T00:00:00Z",
        "lastStatusChangeAt": "2026-07-04T00:00:00Z", "lastStageChangeAt": "2026-07-03T00:00:00Z"}])
    await _seed_contact(mock_db, STORED)
    r = await build_lead_detail(mock_db, USER, "c1")
    assert [o["id"] for o in r["opportunities"]] == ["o1"]
    o = r["opportunities"][0]
    assert (o["name"], o["stage_name"], o["pipeline_name"], o["monetary_value"]) == ("Ann deal", "Won Deals", "Main", 500)
    assert o["last_stage_change_at"] == "2026-07-03T00:00:00Z"
    assert r["engagement"]["time_to_close_seconds"] == 3 * 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "reconnect"])
async def test_live_failure_falls_back_to_stored(mock_db, monkeypatch, status):
    async def _lookup(db, user_id, location_id):
        return {"s1": {"stage_name": "Spicy Leads", "pipeline_id": "p1", "pipeline_name": "Main"}}

    monkeypatch.setattr("services.lead_detail.stage_lookup", _lookup)
    _live(monkeypatch, status)
    await _seed_contact(mock_db, STORED)
    r = await build_lead_detail(mock_db, USER, "c1")
    assert [o["id"] for o in r["opportunities"]] == ["old"]
    assert r["opportunities"][0]["stage_name"] == "Spicy Leads"
    assert r["engagement"]["time_to_close_seconds"] is None


@pytest.mark.asyncio
async def test_earliest_won_wins(mock_db, monkeypatch):
    _live(monkeypatch, "ok", [
        {"id": "a", "status": "won", "lastStatusChangeAt": "2026-07-10T00:00:00Z"},
        {"id": "b", "status": "won", "lastStatusChangeAt": "2026-07-03T00:00:00Z"},
        {"id": "c", "status": "lost", "lastStatusChangeAt": "2026-07-02T00:00:00Z"}])
    await _seed_contact(mock_db, {"dateAdded": "2026-07-01T00:00:00Z"})
    assert (await build_lead_detail(mock_db, USER, "c1"))["engagement"]["time_to_close_seconds"] == 2 * 86400


@pytest.mark.asyncio
@pytest.mark.parametrize("opp,added", [
    ({"status": "won", "lastStatusChangeAt": "2026-06-01T00:00:00Z"}, "2026-07-01T00:00:00Z"),  # negative
    ({"status": "won"}, "2026-07-01T00:00:00Z"),  # no date
    ({"status": "won", "lastStatusChangeAt": "garbage"}, "2026-07-01T00:00:00Z"),
    ({"status": "won", "lastStatusChangeAt": "2026-07-03T00:00:00Z"}, None),  # contact has no dateAdded
    ({"status": "open", "lastStatusChangeAt": "2026-07-03T00:00:00Z"}, "2026-07-01T00:00:00Z"),
])
async def test_time_to_close_null_cases(mock_db, monkeypatch, opp, added):
    _live(monkeypatch, "ok", [{"id": "a", **opp}])
    await _seed_contact(mock_db, {"dateAdded": added} if added else {})
    assert (await build_lead_detail(mock_db, USER, "c1"))["engagement"]["time_to_close_seconds"] is None


@pytest.mark.asyncio
async def test_stage_lookup_called_for_live_opportunities(mock_db, monkeypatch):
    called = []

    async def _lookup(db, user_id, location_id):
        called.append(location_id)
        return {}

    monkeypatch.setattr("services.lead_detail.stage_lookup", _lookup)
    _live(monkeypatch, "ok", [{"id": "a", "status": "open"}])
    await _seed_contact(mock_db)
    await build_lead_detail(mock_db, USER, "c1")
    assert called == ["loc1"]


@pytest.mark.asyncio
async def test_live_ok_but_empty_replaces_stored_opportunities(mock_db, monkeypatch):
    _live(monkeypatch, "ok", [])  # GHL is the source of truth
    await _seed_contact(mock_db, STORED)
    assert (await build_lead_detail(mock_db, USER, "c1"))["opportunities"] == []
