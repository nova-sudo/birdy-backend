from datetime import datetime, timezone

import pytest

from services import lead_detail
from services.lead_detail import build_lead_detail

USER = "u1"


@pytest.fixture(autouse=True)
def _no_ghl(monkeypatch):
    async def _empty(db, user_id, location_id):
        return {}

    monkeypatch.setattr("services.lead_detail.stage_lookup", _empty)


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
        "user_id": USER, "match_keys": ["email:ann@x.com"],
        "lead_data": {"ad_id": "a1", "ad_name": "Ad One", "adset_id": "as1", "adset_name": "Set",
                      "campaign_id": "c9", "campaign_name": "Camp", "created_time": "2026-07-17T10:00:00+0000",
                      "platform": "fb", "is_organic": False,
                      "field_data": [{"name": "budget", "values": ["£100–150"]}]}})
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
    assert a["submitted_at"] == "2026-07-17T10:00:00+0000"


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
    await _seed_contact(mock_db, {"opportunities": [
        {"id": "o1", "status": "won", "dateAdded": "2026-07-17T12:00:00Z",
         "lastStatusChangeAt": "2026-07-17T14:34:00Z"}]})
    r = await build_lead_detail(mock_db, USER, "c1")
    assert r["engagement"]["time_to_close_seconds"] == 9240
