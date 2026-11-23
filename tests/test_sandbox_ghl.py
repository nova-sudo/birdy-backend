"""
Sandbox accounts, phase 2: the fake GoHighLevel (LeadConnector) API, driven through the product's
own functions (onboarding location listing, client-group creation, the first sync, opportunity
stats, the ghl-tick / ghl-tokens jobs, lead detail, conversations, sending a message) for a
sandbox account, with mock_db. Nothing may leave the process: the ``outgoing`` fixture fails the
test if a request reaches httpx's real transport.
"""

import time
from datetime import datetime, timedelta, timezone

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import sandbox
from core.config import JWT_ALGORITHM, JWT_SECRET
from integrations import gohighlevel as gh
from integrations.gohighlevel import ghl_integration
from sandbox import ghl_data, make_credential, state
from sandbox.world import world_for

SBX = "tester@sandbox.birdy.test"
OTHER = "second@sandbox.birdy.test"
BASE = "https://services.leadconnectorhq.com"
V = {"Version": "2021-07-28"}
UTC = timezone.utc


@pytest.fixture(autouse=True)
def _installed(mock_db, monkeypatch):
    sandbox.install()
    ghl_data.clear_cache()
    yield
    ghl_data.clear_cache()


@pytest.fixture(autouse=True)
def outgoing(monkeypatch):
    """Fails the test if any request reaches httpx's real transport (it would have left the process)."""
    seen = []

    async def record(self, request):
        seen.append(str(request.url))
        raise AssertionError(f"left the process: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record)
    yield seen
    assert seen == [], f"requests left the process: {seen}"


@pytest.fixture
def no_sleep(monkeypatch):
    """The sync waits 0.2 s between pages against the real API; not needed in-process."""
    import services.ghl_service as svc

    async def _instant(_s):
        return None

    monkeypatch.setattr(svc.asyncio, "sleep", _instant)


def _world():
    return world_for(SBX)


def _small_client(w):
    """The client with the fewest contacts that rings its leads from GHL (fast to sync, has calls)."""
    candidates = [c for c in w.clients if not c.call_centre]
    return min(candidates, key=lambda c: len(w.leads(c, w.history_start, w.today)))


def _http(token: str):
    return {"Authorization": f"Bearer {token}", **V}


async def _connect(mock_mongo_client):
    """The GHL connect flow's exchange + save (the product functions the callback runs)."""
    ok, tokens = await ghl_integration.exchange_code_for_token(make_credential(SBX, "ghl_code"))
    assert ok
    await gh.save_agency_token(SBX, tokens, mock_mongo_client)
    return tokens


async def _location_token(w, location_id):
    ok, tokens = await ghl_integration.generate_location_token(
        w.agency.ghl_company_id, location_id, make_credential(SBX, "ghl"))
    assert ok
    return tokens["access_token"]


def _expected_contacts(w, c):
    now = datetime.now(UTC)
    return [x for x in w.contacts(c, w.history_start, w.today) if x.date_added <= now]


def _app_client(user=SBX):
    from main import app

    token = pyjwt.encode({"sub": user, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client


async def _synced_group(mock_mongo_client, mock_db, w, c):
    """A client group for ``c``, its location token minted and its first sync done (product code)."""
    from services.ghl_service import fetch_and_cache_ghl_data_optimized

    await _connect(mock_mongo_client)
    saved = await gh.mint_location_token(SBX, c.ghl_location_id, mock_mongo_client)
    assert saved and saved["name"] == c.name
    group_id = f"{SBX}_g{c.index}"
    await mock_db["client_groups"].insert_one({"id": group_id, "user_id": SBX, "name": c.name,
                                               "ghl_location_id": c.ghl_location_id})
    summary = await fetch_and_cache_ghl_data_optimized(group_id, c.ghl_location_id, SBX, mock_mongo_client,
                                                       is_initial_load=True)
    return group_id, summary


# ---------------------------------------------------------------------------
# Onboarding: locations, location token, location details, contact count
# ---------------------------------------------------------------------------
async def test_subaccount_locations_endpoint_lists_the_world(mock_mongo_client):
    await _connect(mock_mongo_client)
    r = _app_client().get("/api/subaccount/locations")
    assert r.status_code == 200, r.text
    w = _world()
    locations = r.json()["locations"]
    assert [(x["id"], x["name"]) for x in locations] == [(c.ghl_location_id, c.name) for c in w.clients]
    assert all(x["isInstalled"] and x["locationId"] == x["id"] for x in locations)


async def test_location_listings_page_to_the_end():
    w = _world()
    agency = make_credential(SBX, "ghl")
    async with httpx.AsyncClient() as client:
        seen, skip = [], 0
        while True:
            r = await client.get(f"{BASE}/locations/search", headers=_http(agency),
                                 params={"companyId": w.agency.ghl_company_id, "limit": 3, "skip": skip})
            chunk = r.json()["locations"]
            seen += [x["id"] for x in chunk]
            if len(chunk) < 3:
                break
            skip += 3
        assert seen == [c.ghl_location_id for c in w.clients]
        r = await client.get(f"{BASE}/oauth/installedLocations", headers=_http(agency),
                             params={"companyId": w.agency.ghl_company_id, "isInstalled": "false", "limit": 500})
        assert r.json()["locations"] == []
        r = await client.get(f"{BASE}/locations/search", headers=_http(agency),
                             params={"companyId": "someoneelse"})
        assert r.status_code == 400 and r.json()["statusCode"] == 400


async def test_creating_a_client_group_mints_a_location_token_and_counts_contacts(mock_mongo_client, mock_db,
                                                                                  monkeypatch):
    import routers.client_groups as cg

    async def no_initial_load(*_a, **_k):   # the first sync is covered on its own below
        return None

    monkeypatch.setattr(cg, "_initial_client_data_load", no_initial_load)
    await _connect(mock_mongo_client)
    await mock_db["users"].update_one({"user_id": SBX}, {"$set": {"client_limit_override": {"limit": 20}}})
    w = _world()
    c = w.clients[2]
    r = _app_client().post("/api/client-groups", json={"name": c.name, "ghl_location_id": c.ghl_location_id,
                                                       "meta_ad_account_id": None, "hotprospector_group_id": None,
                                                       "ad_account_currency": None})
    assert r.status_code == 200, r.text
    sub = (await mock_db["users"].find_one({"user_id": SBX}))["integrations"]["gohighlevel"]["subaccounts"]
    saved = sub[c.ghl_location_id]
    assert sandbox.parse_credential(saved["access_token"]).kind == f"ghl_location:{c.ghl_location_id}"
    assert saved["name"] == c.name and saved["isInstalled"] is True
    assert abs(saved["contact_count"] - len(_expected_contacts(w, c))) <= 2   # leads keep arriving


async def test_onboarding_activity_probe(mock_mongo_client):
    from routers.onboarding import _latest_contact

    w = _world()
    c = max(w.clients, key=lambda c: len(w.leads(c, w.today - timedelta(days=29), w.today)))
    token = await _location_token(w, c.ghl_location_id)
    probe = await _latest_contact(c.ghl_location_id, token)
    expected = _expected_contacts(w, c)
    assert abs(probe["contact_count"] - len(expected)) <= 2
    assert probe["last_lead_at"] is not None and probe["leads_30d"] == 50 and probe["leads_30d_capped"]


# ---------------------------------------------------------------------------
# The first sync, opportunity stats, the crons
# ---------------------------------------------------------------------------
async def test_first_sync_stores_every_contact_and_opportunity(mock_mongo_client, mock_db, no_sleep):
    w = _world()
    c = _small_client(w)
    group_id, summary = await _synced_group(mock_mongo_client, mock_db, w, c)
    rows = await mock_db["ghl_contacts"].find({"location_id": c.ghl_location_id}).to_list(None)
    expected = {x.id: x for x in _expected_contacts(w, c)}
    stored = {r["contact_id"]: r for r in rows}
    assert len(stored) == len(rows) and abs(len(stored) - len(expected)) <= 2
    assert summary["mode"] == "FULL LOAD"
    some = next(i for i in stored if i in expected)
    data, lead = stored[some]["contact_data"], w.lead(expected[some].lead_id)
    assert (data["email"], data["phone"]) == (lead.email, lead.phone)            # the Meta lead's person
    assert data["attributionSource"]["adId"] == lead.ad_id
    assert data["attributionSource"]["campaignId"] == lead.campaign_id
    assert stored[some]["lead_type"] == "lead"
    assert all(r["contact_data"]["dateAdded"].endswith("Z") for r in rows)
    now_iso = gh._ghl_iso_utc(datetime.now(UTC))
    assert all(r["contact_data"]["dateUpdated"] <= now_iso for r in rows)

    group = await mock_db["client_groups"].find_one({"id": group_id})
    opps = [o for o in (w.opportunity_for(w.lead(x.lead_id)) for x in expected.values()) if o]
    maximum = group["ghl_opp_cache"]["maximum"]
    assert abs(maximum["total_opportunities"] - len(opps)) <= 2
    won = [o for o in opps if o.status == "won"]
    assert abs(maximum["won"] - len(won)) <= 1
    assert maximum["won_revenue"] == pytest.approx(sum(o.monetary_value for o in won), abs=3000)
    assert group["gohighlevel_cache"]["metrics"]["total_contacts"] == len(rows)
    assert group["gohighlevel_cache"]["metrics"]["tag_breakdown"]["facebook lead"] == len(rows)
    # The embedded opportunity on a contact agrees with the opportunity feed.
    with_opp = next(r for r in rows if r["contact_data"]["opportunities"])
    o = with_opp["contact_data"]["opportunities"][0]
    assert w.opportunity(o["id"]).status == o["status"]


async def test_opportunity_stats_windowed_and_lifetime_agree_with_the_world():
    w = _world()
    c = w.clients[0]
    token = await _location_token(w, c.ghl_location_id)
    ok, opps = await ghl_integration.fetch_all_opportunities(c.ghl_location_id, token)
    assert ok
    world_opps = {o.id: o for o in w.opportunities(c, w.history_start, w.today)}
    assert {o["id"] for o in opps} <= set(world_opps) and len(world_opps) - len(opps) <= 2
    assert len({o["id"] for o in opps}) == len(opps)     # page-number paging to the end, no repeats
    start = (w.today - timedelta(days=29)).isoformat()
    ok, stats = await ghl_integration.fetch_opportunity_stats(c.ghl_location_id, token, start, w.today.isoformat())
    assert ok
    won = [o for o in opps if o["status"] == "won" and o["lastStatusChangeAt"][:10] >= start]
    assert stats["won"] == len(won) and stats["won_revenue"] == round(sum(o["monetaryValue"] for o in won), 2)
    rows = gh.compute_daily_opp_rows(opps)
    assert sum(r["opps_total"] for r in rows) == len(opps)
    assert all(o["monetaryValue"] == 0 for o in opps if o["status"] != "won")
    assert all(o["createdAt"] <= o["updatedAt"] <= gh._ghl_iso_utc(datetime.now(UTC)) for o in opps)
    ok, pipelines = await ghl_integration.fetch_pipelines(c.ghl_location_id, token)
    stage_ids = {s["id"] for p in pipelines for s in p["stages"]}
    assert ok and {o["pipelineStageId"] for o in opps} <= stage_ids


async def test_incremental_tick_picks_up_a_contact_edited_in_ghl(mock_mongo_client, mock_db, no_sleep):
    from services.ghl_service import fetch_and_cache_ghl_data_optimized

    w = _world()
    c = _small_client(w)
    group_id, _ = await _synced_group(mock_mongo_client, mock_db, w, c)
    since = datetime.now(UTC)
    token = (await gh.get_subaccount_tokens(SBX, mock_mongo_client))[c.ghl_location_id]["access_token"]
    target = (await mock_db["ghl_contacts"].find_one({"location_id": c.ghl_location_id},
                                                     sort=[("contact_data.dateAdded", 1)]))["contact_id"]
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{BASE}/contacts/{target}/tags", headers=_http(token), json={"tags": ["VIP"]})
    assert r.status_code == 201 and "vip" in r.json()["tags"]
    ok, page = await ghl_integration.fetch_contacts_search(c.ghl_location_id, token, updated_since=since)
    assert ok and [x["id"] for x in page["contacts"]] == [target]
    summary = await fetch_and_cache_ghl_data_optimized(group_id, c.ghl_location_id, SBX, mock_mongo_client,
                                                       is_initial_load=False, updated_since=since)
    assert summary["mode"] == "INCREMENTAL" and summary["updated"] == 1
    doc = await mock_db["ghl_contacts"].find_one({"contact_id": target})
    assert "vip" in doc["contact_data"]["tags"]


async def test_token_cron_regenerates_location_tokens(mock_mongo_client, mock_db):
    from jobs.ghl_jobs import refresh_tokens_for_all_users

    w = _world()
    await _connect(mock_mongo_client)
    c = w.clients[4]
    await gh.mint_location_token(SBX, c.ghl_location_id, mock_mongo_client)
    # Make the agency token due, so the cron refreshes it as well.
    await mock_db["users"].update_one({"user_id": SBX}, {"$set": {
        "integrations.gohighlevel.agency.expires_at": datetime.now() + timedelta(hours=1)}})
    before = await gh.get_subaccount_tokens(SBX, mock_mongo_client)
    await refresh_tokens_for_all_users()
    user = await mock_db["users"].find_one({"user_id": SBX})
    after = user["integrations"]["gohighlevel"]["subaccounts"][c.ghl_location_id]
    assert after["access_token"] != before[c.ghl_location_id]["access_token"]
    assert sandbox.parse_credential(after["access_token"]).kind == f"ghl_location:{c.ghl_location_id}"
    assert not user["integrations"]["gohighlevel"]["agency"].get("needs_reconnect")
    assert gh.agency_token_expires_within(user["integrations"]["gohighlevel"]["agency"], timedelta(hours=8)) is False
    ok, fresh = await ghl_integration.refresh_location_token(
        c.ghl_location_id, make_credential(SBX, f"ghl_location_refresh:{c.ghl_location_id}"))
    assert ok and fresh["locationId"] == c.ghl_location_id


# ---------------------------------------------------------------------------
# Lead detail, conversation, sending a message, calls without recordings
# ---------------------------------------------------------------------------
async def test_lead_detail_journey_and_conversation(mock_mongo_client, mock_db, no_sleep):
    from services.ghl_messages import fetch_lead_messages
    from services.lead_detail import build_lead_detail
    from services.lead_journey import build_lead_journey

    w = _world()
    c = _small_client(w)
    await _synced_group(mock_mongo_client, mock_db, w, c)
    booked = next(x for x in _expected_contacts(w, c)
                  if (o := w.opportunity_for(w.lead(x.lead_id))) and o.stage.name == "Won")
    detail = await build_lead_detail(mock_db, SBX, booked.id)
    assert detail["ghl"] == "ok"
    assert detail["contact"]["email"] == booked.email
    [opp] = detail["opportunities"]
    assert opp["status"] == "won" and opp["stage_name"] == "Won" and opp["pipeline_name"] == "Lead Pipeline"
    assert detail["attribution"]["ad_id"] == booked.ad_id

    journey = await build_lead_journey(mock_db, SBX, booked.id)
    kinds = [e["kind"] for e in journey["events"]]
    assert "appointment" in kinds and "stage" in kinds

    out = await fetch_lead_messages(mock_db, SBX, c.ghl_location_id, booked.id)
    assert out["status"] == "ok" and not out["partial"]
    assert out["messages"][0]["channel"] == "SMS" and out["messages"][0]["direction"] == "outbound"
    assert any(m["body"].startswith(f"Hi {booked.first_name}, you're booked in") for m in out["messages"])
    assert out["calls"] and all(call["recording_url"] is None for call in out["calls"])
    assert all(m["date"] <= datetime.now(UTC).isoformat() for m in out["messages"])


async def test_send_a_message_then_read_it_back(mock_mongo_client, mock_db, no_sleep, outgoing):
    from services.ghl_messages import fetch_lead_messages, send_lead_message

    w = _world()
    c = _small_client(w)
    await _synced_group(mock_mongo_client, mock_db, w, c)
    contact = _expected_contacts(w, c)[-1]
    sent = await send_lead_message(mock_db, SBX, c.ghl_location_id, contact.id, "SMS", "Hi! Still keen?")
    assert sent["id"] and sent["status"] == "sent"
    mail = await send_lead_message(mock_db, SBX, c.ghl_location_id, contact.id, "Email", "Line 1\nLine 2",
                                   subject="Your consult")
    out = await fetch_lead_messages(mock_db, SBX, c.ghl_location_id, contact.id)
    by_id = {m["id"]: m for m in out["messages"]}
    assert by_id[sent["id"]]["body"] == "Hi! Still keen?" and by_id[sent["id"]]["channel"] == "SMS"
    assert by_id[mail["id"]]["subject"] == "Your consult" and by_id[mail["id"]]["body"] == "Line 1 Line 2"
    assert out["messages"][-1]["id"] in (sent["id"], mail["id"])
    remembered = await state.list(SBX, "ghl_message")
    assert {d["key"] for d in remembered} == {sent["id"], mail["id"]}
    # The message can be read on its own and belongs to that contact.
    code, message = await ghl_integration.get_message(
        (await gh.get_subaccount_tokens(SBX, mock_mongo_client))[c.ghl_location_id]["access_token"], sent["id"])
    assert code == 200 and message["contactId"] == contact.id
    assert outgoing == []


async def test_send_through_the_api_route(mock_mongo_client, mock_db, no_sleep):
    w = _world()
    c = _small_client(w)
    await _synced_group(mock_mongo_client, mock_db, w, c)
    contact = _expected_contacts(w, c)[0]
    client = _app_client()
    r = client.post(f"/api/leads/contact/{contact.id}/messages", json={"channel": "SMS", "message": "Hello there"})
    assert r.status_code == 201, r.text
    r = client.get(f"/api/leads/contact/{contact.id}/messages")
    assert r.status_code == 200 and any(m["body"] == "Hello there" for m in r.json()["messages"])


async def test_calls_have_no_recording_and_degrade_cleanly(mock_mongo_client, mock_db, no_sleep):
    from services.ghl_messages import (RecordingNotFound, fetch_call_recording, fetch_call_transcript,
                                       fetch_lead_messages)

    w = _world()
    c = _small_client(w)
    await _synced_group(mock_mongo_client, mock_db, w, c)
    for contact in reversed(_expected_contacts(w, c)):
        out = await fetch_lead_messages(mock_db, SBX, c.ghl_location_id, contact.id)
        answered = [x for x in out["calls"] if x["status"] == "completed"]
        if answered:
            break
    call = answered[0]
    assert call["duration_seconds"] >= 45
    with pytest.raises(RecordingNotFound):
        await fetch_call_recording(mock_db, SBX, c.ghl_location_id, contact.id, call["ghl_message_id"])
    assert await fetch_call_transcript(mock_db, SBX, c.ghl_location_id, contact.id, call["ghl_message_id"]) is None
    r = _app_client().get(f"/api/leads/contact/{contact.id}/calls/{call['ghl_message_id']}/recording")
    assert r.status_code == 404 and r.json()["detail"] == "Recording not found"


# ---------------------------------------------------------------------------
# Writes: create contact (landing-page push), opportunity, notes, tasks, tags
# ---------------------------------------------------------------------------
async def test_landing_page_push_creates_a_contact_that_reads_back(mock_mongo_client, mock_db):
    from services.ghl_lead_push import push_lead

    w = _world()
    c = w.clients[1]
    token = await _location_token(w, c.ghl_location_id)
    before = await gh.get_contact_count_from_ghl(c.ghl_location_id, token)
    await mock_db["tracked_leads"].insert_one({"_id": "L1", "client_group_id": "g1", "pushed_to_ghl": False})
    lead = {"_id": "L1", "client_group_id": "g1", "name": "Pat Example", "email": "pat.new@example.org",
            "phone": "+447700000001", "match_keys": ["email:pat.new@example.org"]}
    result = await push_lead(lead, c.ghl_location_id, token, mock_db)
    assert result["pushed"] and result["contact_id"]
    assert await gh.get_contact_count_from_ghl(c.ghl_location_id, token) == before + 1
    ok, page = await ghl_integration.fetch_contacts_search(c.ghl_location_id, token, page=1, limit=1)
    assert page["contacts"][0]["id"] == result["contact_id"]          # newest first
    assert page["contacts"][0]["source"] == "Birdy (landing page)"
    async with httpx.AsyncClient() as client:
        dup = await client.post(f"{BASE}/contacts/", headers=_http(token),
                                json={"locationId": c.ghl_location_id, "email": "PAT.NEW@example.org"})
    assert dup.status_code == 400 and dup.json()["meta"]["contactId"] == result["contact_id"]


async def test_opportunity_and_contact_writes_are_reflected_everywhere():
    w = _world()
    c = w.clients[0]
    token = await _location_token(w, c.ghl_location_id)
    ok, opps = await ghl_integration.fetch_all_opportunities(c.ghl_location_id, token)
    target = next(o for o in opps if o["status"] == "open")
    won_stage = c.pipeline.stage("Won").id
    _, before = await ghl_integration.fetch_opportunity_stats(c.ghl_location_id, token)
    async with httpx.AsyncClient() as client:
        r = await client.put(f"{BASE}/opportunities/{target['id']}", headers=_http(token),
                             json={"pipelineStageId": won_stage, "status": "won", "monetaryValue": 1234})
        assert r.status_code == 200 and r.json()["opportunity"]["status"] == "won"
        note = await client.post(f"{BASE}/contacts/{target['contactId']}/notes", headers=_http(token),
                                 json={"body": "Paid in full"})
        assert note.status_code == 201
        notes = (await client.get(f"{BASE}/contacts/{target['contactId']}/notes", headers=_http(token))).json()
        assert notes["notes"][0]["body"] == "Paid in full"
        task = await client.post(f"{BASE}/contacts/{target['contactId']}/tasks", headers=_http(token),
                                 json={"title": "Send aftercare", "dueDate": "2026-12-01T09:00:00Z"})
        assert task.status_code == 201
        tasks = (await client.get(f"{BASE}/contacts/{target['contactId']}/tasks", headers=_http(token))).json()
        assert [t["title"] for t in tasks["tasks"]] == ["Send aftercare"]
        removed = await client.request("DELETE", f"{BASE}/contacts/{target['contactId']}/tags",
                                       headers=_http(token), json={"tags": ["facebook lead"]})
        assert "facebook lead" not in removed.json()["tags"]
        got = (await client.get(f"{BASE}/contacts/{target['contactId']}", headers=_http(token))).json()["contact"]
        assert got["opportunities"][0]["status"] == "won" and "facebook lead" not in got["tags"]
    _, after = await ghl_integration.fetch_opportunity_stats(c.ghl_location_id, token)
    assert after["won"] == before["won"] + 1 and after["open"] == before["open"] - 1
    assert after["won_revenue"] == round(before["won_revenue"] + 1234, 2)
    code, live = await ghl_integration.search_contact_opportunities(c.ghl_location_id, token, target["contactId"])
    assert code == 200 and live[0]["pipelineStageId"] == won_stage
    # Another sandbox account's world is untouched.
    other = world_for(OTHER)
    assert other.clients[0].ghl_location_id != c.ghl_location_id


# ---------------------------------------------------------------------------
# Paging styles, filters, errors
# ---------------------------------------------------------------------------
async def test_every_paging_style_reaches_the_end():
    w = _world()
    c = _small_client(w)
    loc = c.ghl_location_id
    token = await _location_token(w, loc)
    ok, all_contacts = await ghl_integration.fetch_all_contacts_search(loc, token)      # page / pageLimit
    ids = [x["id"] for x in all_contacts]
    assert ok and len(ids) == len(set(ids)) and len(ids) > 500
    ok, legacy = await ghl_integration.fetch_all_location_contacts(loc, token)          # startAfterId
    assert ok and [x["id"] for x in legacy] == ids
    async with httpx.AsyncClient() as client:
        seen, after = [], None                                                         # searchAfter
        while True:
            body = {"locationId": loc, "pageLimit": 250, **({"searchAfter": after} if after else {})}
            page = (await client.post(f"{BASE}/contacts/search", headers=_http(token), json=body)).json()
            if not page["contacts"]:
                break
            seen += [x["id"] for x in page["contacts"]]
            after = page["contacts"][-1]["searchAfter"]
        assert seen == ids
        seen, params = [], {"locationId": loc, "limit": 100}                             # startAfter + startAfterId
        while True:
            page = (await client.get(f"{BASE}/contacts/", headers=_http(token), params=params)).json()
            seen += [x["id"] for x in page["contacts"]]
            if not page["meta"]["startAfterId"]:
                break
            params = {**params, "startAfter": page["meta"]["startAfter"], "startAfterId": page["meta"]["startAfterId"]}
        assert seen == ids and page["meta"]["total"] == len(ids)
        opp_ids, params = [], {"location_id": loc, "limit": 100}                         # opportunities cursor
        while True:
            page = (await client.get(f"{BASE}/opportunities/search", headers=_http(token), params=params)).json()
            opp_ids += [o["id"] for o in page["opportunities"]]
            if not page["meta"]["startAfterId"]:
                break
            params = {**params, "startAfter": page["meta"]["startAfter"], "startAfterId": page["meta"]["startAfterId"]}
        ok, by_page = await ghl_integration.fetch_all_opportunities(loc, token)          # page / meta.nextPage
        assert opp_ids == [o["id"] for o in by_page] and page["meta"]["total"] == len(opp_ids)
        contact = next(x for x in all_contacts if x["opportunities"])
        msgs, cursor = [], None                                                         # message export cursor
        while True:
            params = {"locationId": loc, "contactId": contact["id"], "limit": 1, **({"cursor": cursor} if cursor else {})}
            page = (await client.get(f"{BASE}/conversations/messages/export", headers=_http(token), params=params)).json()
            msgs += page["messages"]
            cursor = page["nextCursor"]
            if not cursor:
                break
        assert len(msgs) == page["total"] >= 1
        conv = (await client.get(f"{BASE}/conversations/search", headers=_http(token),
                                 params={"locationId": loc, "contactId": contact["id"]})).json()["conversations"][0]
        in_conv, last = [], None                                                        # lastMessageId
        while True:
            params = {"limit": 1, **({"lastMessageId": last} if last else {})}
            page = (await client.get(f"{BASE}/conversations/{conv['id']}/messages", headers=_http(token),
                                     params=params)).json()["messages"]
            in_conv += page["messages"]
            last = page["lastMessageId"]
            if not page["nextPage"]:
                break
        assert len(in_conv) >= len(msgs)


async def test_search_filters_and_their_errors():
    w = _world()
    c = w.clients[0]
    token = await _location_token(w, c.ghl_location_id)
    async with httpx.AsyncClient() as client:
        async def search(body):
            return await client.post(f"{BASE}/contacts/search", headers=_http(token),
                                     json={"locationId": c.ghl_location_id, **body})
        r = await search({"filters": [{"field": "dateUpdated", "operator": "gte", "value": "2026-01-01"}]})
        assert r.status_code == 422 and r.json()["statusCode"] == 422
        r = await search({"pageLimit": 501})
        assert r.status_code == 422
        r = await search({"filters": [{"field": "tags", "operator": "eq", "value": ["booked"]}], "pageLimit": 500})
        booked = r.json()
        assert booked["total"] > 0 and all("booked" in x["tags"] for x in booked["contacts"])
        start = (w.today - timedelta(days=6)).isoformat() + "T00:00:00.000Z"
        r = await search({"filters": [{"field": "dateAdded", "operator": "range", "value": {"gte": start}}],
                          "sort": [{"field": "dateAdded", "direction": "asc"}], "pageLimit": 500})
        rows = r.json()["contacts"]
        assert rows and all(x["dateAdded"] >= start for x in rows)
        assert [x["dateAdded"] for x in rows] == sorted(x["dateAdded"] for x in rows)
        r = await search({"query": rows[0]["email"]})
        found = r.json()["contacts"]
        assert rows[0]["id"] in [x["id"] for x in found] and all(rows[0]["email"] in x["email"] for x in found)


async def test_a_location_token_is_refused_for_another_location():
    w = _world()
    a, b = w.clients[0], w.clients[1]
    token = await _location_token(w, a.ghl_location_id)
    ok, err = await ghl_integration.fetch_contacts_search(b.ghl_location_id, token)
    assert not ok and err["status_code"] == 403
    assert await gh.get_contact_count_from_ghl(b.ghl_location_id, token) == 0
    ok, opps = await ghl_integration.fetch_all_opportunities(b.ghl_location_id, token)
    assert not ok and opps == []
    other_contact = w.contact_id(w.leads(b, w.today - timedelta(days=10), w.today)[0].id)
    code, rows = await ghl_integration.fetch_contact_appointments(token, other_contact)
    assert code == 403
    code, _ = await ghl_integration.send_message(token, {"type": "SMS", "contactId": other_contact, "message": "x"})
    assert code == 403
    # The agency token is not a location token either; another account's location token neither.
    ok, err = await ghl_integration.fetch_contacts_search(a.ghl_location_id, make_credential(SBX, "ghl"))
    assert not ok and err["status_code"] == 401
    stranger = make_credential(OTHER, f"ghl_location:{world_for(OTHER).clients[0].ghl_location_id}")
    ok, err = await ghl_integration.fetch_contacts_search(a.ghl_location_id, stranger)
    assert not ok and err["status_code"] == 403


async def test_unknown_endpoint_and_missing_version_answer_in_ghls_shape(caplog):
    w = _world()
    token = await _location_token(w, w.clients[0].ghl_location_id)
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE}/calendars/nope", headers=_http(token))
        assert r.status_code == 404
        assert r.json()["statusCode"] == 404 and r.json()["error"] == "Not Found"
        assert r.json()["message"] == "sandbox: GET /calendars/nope is not implemented"
        r = await client.get(f"{BASE}/contacts/", headers={"Authorization": f"Bearer {token}"},
                             params={"locationId": w.clients[0].ghl_location_id})
        assert r.status_code == 400 and r.json()["message"] == "Version header was not found"
        r = await client.get(f"{BASE}/contacts/doesnotexist", headers=_http(token))
        assert r.status_code == 400 and r.json()["statusCode"] == 400


async def test_location_endpoints_are_cheap_enough_to_poll():
    w = _world()
    agency = make_credential(SBX, "ghl")
    await ghl_integration.fetch_locations(w.agency.ghl_company_id, agency)
    t = time.perf_counter()
    for _ in range(5):
        ok, locations = await ghl_integration.fetch_locations(w.agency.ghl_company_id, agency)
    assert ok and len(locations) == 8
    assert (time.perf_counter() - t) / 5 < 0.5
