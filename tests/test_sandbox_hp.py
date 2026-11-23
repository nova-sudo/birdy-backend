"""
Sandbox accounts, phase 2: the fake Hot Prospector, driven through the REAL product code (the
integration class, the sync service hp-tick runs, the routes), with a sandbox account's stored
credentials and the in-memory database. Nothing may leave the process (`outgoing`).
"""

import time
from datetime import date, datetime, timedelta

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import sandbox
from core.config import JWT_ALGORITHM, JWT_SECRET
from core.database import DB_NAME
from integrations import hotprospector as hp
from integrations.hotprospector import HotProspectorIntegration
from sandbox import hp_rows
from sandbox.credentials import parse_credential
from sandbox.hp_app import sandbox_hp_credentials
from sandbox.world import world_for

SBX = "tester@sandbox.birdy.test"
OTHER = "second@sandbox.birdy.test"


@pytest.fixture(autouse=True)
def _installed(mock_db):
    sandbox.install()
    hp._token_cache.clear()     # tokens are per process: no test may lean on another's
    yield
    hp._token_cache.clear()


@pytest.fixture
def outgoing(monkeypatch):
    """Every request that reached httpx's real transport (none may, for a sandbox account)."""
    seen = []

    async def record(self, request):
        seen.append(str(request.url))
        raise AssertionError(f"left the process: {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record)
    yield seen


def _hp(user=SBX) -> HotProspectorIntegration:
    return HotProspectorIntegration(*sandbox_hp_credentials(user))


def _world(user=SBX):
    return world_for(user)


def _centres(w):
    return [c for c in w.clients if c.call_centre]


def _client(user=SBX) -> TestClient:
    from main import app

    token = pyjwt.encode({"sub": user, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client


async def _connect(mock_db, user=SBX):
    uid, key = sandbox_hp_credentials(user)
    await mock_db["users"].update_one({"user_id": user}, {"$set": {"integrations.hotprospector.credentials": {
        "api_uid": uid, "api_key": key, "connected": True}}}, upsert=True)
    return uid, key


async def _group(mock_db, client, user=SBX, **extra):
    gid = f"grp-{client.key}"
    await mock_db["client_groups"].insert_one({
        "id": gid, "user_id": user, "name": client.name, "ghl_location_id": client.ghl_location_id,
        "call_log_provider": "hotprospector", **extra})
    return gid


# ---------------------------------------------------------------------------
# Leads (SearchByUserInput)
# ---------------------------------------------------------------------------
async def test_leads_page_to_the_end_and_are_the_meta_leads(mock_db, outgoing):
    w = _world()
    for c in _centres(w):
        ok, info = await _hp().fetch_all_leads_from_ghl_location(c.ghl_location_id, with_meta=True)
        people = w.leads(c, w.history_start, w.today)
        assert ok and not info["truncated"] and info["total_count"] == len(people) > 100
        rows = info["leads"]
        assert [r["LeadId"] for r in rows] == [w.hp_lead_id(p.id) for p in people]
        # The same people as the Meta leads and the GHL contacts.
        for r, p in zip(rows, people):
            contact = w.contact_for(p)
            assert (r["E-Mail"], r["Phone"]) == (p.email, p.phone) == (contact.email, contact.phone)
            assert r["GroupId"] == c.hp_group_id and contact.hp_lead_id == r["LeadId"]
    assert outgoing == []


async def test_only_call_centre_clients_have_leads(mock_db, outgoing):
    w = _world()
    others = [c for c in w.clients if not c.call_centre]
    assert len(others) == 3
    for c in others:
        ok, rows = await _hp().fetch_all_leads_from_ghl_location(c.ghl_location_id)
        assert ok and rows == []
    ok, rows = await _hp().fetch_all_leads_from_ghl_location("notALocation000000000")
    assert ok and rows == []


async def test_search_pages_limit_and_filter(mock_db, outgoing):
    c = _centres(_world())[0]
    ok, page = await _hp().search_leads_by_ghl_location(c.ghl_location_id, limit=500, offset=0)
    assert ok and len(page["leads"]) == 100       # HP's max_limit
    total = page["total_count"]
    assert page["total_pages"] == -(-total // 100)
    ok, last = await _hp().search_leads_by_ghl_location(c.ghl_location_id, offset=(total // 100) * 100)
    assert ok and len(last["leads"]) == total % 100
    ok, past = await _hp().search_leads_by_ghl_location(c.ghl_location_id, offset=total + 5)
    assert ok and past["leads"] == []
    email = page["leads"][7]["E-Mail"]
    ok, found = await _hp().search_leads_by_ghl_location(c.ghl_location_id, "email", email.upper())
    assert ok and [r["E-Mail"] for r in found["leads"]] == [email]


# ---------------------------------------------------------------------------
# Call log (FetchUserCallLog): 30-day windows over 400 days
# ---------------------------------------------------------------------------
async def test_call_windows_cover_the_history_and_older_ones_are_empty(mock_db, outgoing):
    w = _world()
    c = _centres(w)[0]
    ok, calls, meta = await _hp().fetch_call_logs_for_location(c.ghl_location_id, with_meta=True)
    windows = HotProspectorIntegration.call_windows(400, 30)
    assert ok and meta["complete"] and len(meta["done"]) == len(windows) >= 13
    expected = w.calls(c, w.history_start, w.today)
    assert sorted(x["call_id"] for x in calls) == sorted(x.id for x in expected)
    assert all(x["recording"] == "" and x["recordingId"] == "" for x in calls)

    # A window entirely before the 120 days of history: empty, not an error.
    old = w.history_start - timedelta(days=45)
    ok, payload = await _hp().fetch_user_call_logs(c.ghl_location_id, from_date=old.isoformat(),
                                                   to_date=(old + timedelta(days=29)).isoformat())
    assert ok and payload["call_logs"] == [] and payload["complete"]


async def test_call_log_pages_and_counts(mock_db, outgoing):
    w = _world()
    c = _centres(w)[0]
    until = w.today
    since = until - timedelta(days=29)
    ok, small = await _hp().fetch_user_call_logs(c.ghl_location_id, from_date=since.isoformat(),
                                                 to_date=until.isoformat(), page_size=37)
    expected = hp_rows.calls(w, [c], since, until)
    assert ok and small["complete"] and len(small["call_logs"]) == len(expected) == small["total_records"]
    assert small["inbound_count"] + small["outbound_count"] == len(expected)
    assert len({x["call_id"] for x in small["call_logs"]}) == len(expected)
    # Newest first, as the product asks (sort_order DESC).
    times = [hp._parse_hp_call_time(x["call_time"]) for x in small["call_logs"]]
    assert times == sorted(times, reverse=True)
    ok, inbound = await _hp().fetch_user_call_logs(c.ghl_location_id, from_date=since.isoformat(),
                                                   to_date=until.isoformat(), call_type="inbound")
    assert ok and {x["call_type"] for x in inbound["call_logs"]} <= {"inbound"}
    assert len(inbound["call_logs"]) == small["inbound_count"]


async def test_call_log_range_over_30_days_is_refused(mock_db, outgoing):
    w = _world()
    ok, err = await _hp().fetch_user_call_logs(None, from_date=(w.today - timedelta(days=60)).isoformat(),
                                               to_date=w.today.isoformat())
    assert not ok and "30 days" in err["error"]


# ---------------------------------------------------------------------------
# Consistency: dashboards = call log; a lead's calls = its rows in the call log
# ---------------------------------------------------------------------------
async def test_member_dashboard_of_a_day_is_that_days_call_log(mock_db, outgoing):
    w = _world()
    for day in (w.today, w.today - timedelta(days=1), w.today - timedelta(days=40)):
        ok, dash = await _hp().get_member_dashboard_data(day.isoformat())
        assert ok and dash["date"] == day.isoformat()
        assert {r["agentId"] for r in dash["results"]} == {m.id for m in w.members()}
        ok, log = await _hp().fetch_user_call_logs(None, from_date=day.isoformat(), to_date=day.isoformat())
        rows = log["call_logs"]
        assert ok and rows
        for agent in dash["results"]:
            mine = [r for r in rows if r["member_id"] == agent["agentId"]]
            assert int(agent["outboundCall"]) == sum(r["call_type"] == "outbound" for r in mine)
            assert int(agent["inboundCall"]) == sum(r["call_type"] == "inbound" for r in mine)
            assert int(agent["answered_calls"]) == sum(int(r["duration"]) > 0 for r in mine)
            assert float(agent["talkMin"]) == pytest.approx(sum(int(r["duration"]) for r in mine) / 60, abs=1e-5)
            assert all(r["caller_name"] == agent["agentName"] for r in mine)
        total = sum(int(a["outboundCall"]) + int(a["inboundCall"]) for a in dash["results"])
        assert total == len(rows)


async def test_dashboard_before_history_or_in_the_future_is_all_zero(mock_db, outgoing):
    w = _world()
    for day in (w.history_start - timedelta(days=3), w.today + timedelta(days=2)):
        ok, dash = await _hp().get_member_dashboard_data(day.isoformat())
        assert ok and len(dash["results"]) == len(w.members())
        assert all(r["outboundCall"] == r["inboundCall"] == "0" for r in dash["results"])


async def test_a_leads_calls_match_its_call_log_rows(mock_db, outgoing):
    w = _world()
    c = _centres(w)[1]
    ok, calls, _ = await _hp().fetch_call_logs_for_location(c.ghl_location_id, with_meta=True)
    by_lead = {}
    for row in calls:
        by_lead.setdefault(row["leadId"], set()).add(row["call_id"])
    sample = sorted(by_lead)[:: max(1, len(by_lead) // 12)]
    assert sample
    for hp_lead_id in sample:
        ok, logs = await _hp().fetch_lead_call_logs(hp_lead_id)
        assert ok and {x["call_id"] for x in logs} == by_lead[hp_lead_id]
        lead = w.hp_lead(hp_lead_id)
        assert all(x["to_number"] == lead.phone or x["from_number"] == lead.phone for x in logs)
    # Normalized as the product's per-lead path does.
    normalized = await _hp().fetch_call_logs_for_leads_batch(sample[:3])
    assert all(log["call_status"] in ("inbound", "outbound") and log["recording_url"] in ("", None)
               for logs in normalized.values() for log in logs)


async def test_another_accounts_lead_is_not_found(mock_db, outgoing):
    w = _world()
    lead = w.leads(_centres(w)[0], w.today - timedelta(days=10), w.today)[0]
    ok, err = await _hp(OTHER).fetch_lead_call_logs(w.hp_lead_id(lead.id))
    assert not ok


# ---------------------------------------------------------------------------
# Envelope, auth, legacy v1
# ---------------------------------------------------------------------------
async def test_unknown_method_answers_hps_error_shape(mock_db, outgoing):
    i = _hp()
    ok, err = await i._make_request({"api_uId": i.api_uid, "api_key": i.api_key, "Method": "DeleteEverything"})
    assert not ok and "DeleteEverything" in err["error"] and "not implemented" in err["error"]
    token = await i._get_access_token()
    async with httpx.AsyncClient() as client:
        r = await client.post("https://service.hookscall.com/glu/api/v2/request", json={"Method": "Nope"},
                              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["response"] == "false" and r.json()["message"]


async def test_token_refresh_and_a_rejected_token(mock_db, outgoing, monkeypatch):
    from sandbox.credentials import make_credential

    i = _hp()
    hp._token_cache.pop(i.api_uid, None)
    refresh = make_credential(SBX, "hp_refresh")
    await mock_db["hp_tokens"].insert_one({"_id": i.api_uid, "access_token": make_credential(SBX, "hp"),
                                           "refresh_token": refresh, "expires_at": time.time() - 10})
    seen = []
    real_refresh = HotProspectorIntegration._refresh_access_token

    async def spy(self, client, token):
        seen.append(token)
        return await real_refresh(self, client, token)

    monkeypatch.setattr(HotProspectorIntegration, "_refresh_access_token", spy)
    token = await i._get_access_token()
    assert seen == [refresh] and parse_credential(token).kind == "hp"

    # A token HP rejects (wrong kind): the product refreshes and retries, and the call succeeds.
    hp._token_cache[i.api_uid] = {"access_token": make_credential(SBX, "media"),
                                  "refresh_token": refresh, "expires_at": time.time() + 3600}
    await mock_db["hp_tokens"].delete_many({})
    ok, members = await i.get_member_users()
    assert ok and len(members) == len(_world().members())
    assert parse_credential(hp._token_cache[i.api_uid]["access_token"]).kind == "hp"


async def test_legacy_v1_path(mock_db, outgoing, monkeypatch):
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "api_version", "v1")
    c = _centres(_world())[0]
    ok, members = await _hp().get_member_users()
    assert ok and members
    ok, page = await _hp().search_leads_by_ghl_location(c.ghl_location_id)
    assert ok and len(page["leads"]) == 100
    # Mismatched uid / key (two accounts) is refused.
    uid, _ = sandbox_hp_credentials(SBX)
    _, key = sandbox_hp_credentials(OTHER)
    ok, err = await HotProspectorIntegration(uid, key).get_member_users()
    assert not ok


async def test_sandbox_calls_skip_the_shared_rate_limit_spacing(mock_db, outgoing, monkeypatch):
    calls = []

    async def throttle(method):
        calls.append(method)

    monkeypatch.setattr(hp, "_throttle_method", throttle)
    await _hp().search_leads_by_ghl_location(_centres(_world())[0].ghl_location_id)
    assert calls == []

    async def fake_execute_post(self, client, url, body, headers):
        return True, {"response": "true", "Results": [], "total_count": 0}, 200

    monkeypatch.setattr(HotProspectorIntegration, "_post_and_parse", fake_execute_post)
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "api_version", "v1")
    await HotProspectorIntegration("real-uid", "real-key").search_leads_by_ghl_location("loc")
    assert calls == ["SearchByUserInput"]   # a normal account is still spaced out


# ---------------------------------------------------------------------------
# The sync the hp-tick cron runs, and the crons themselves
# ---------------------------------------------------------------------------
async def test_call_centre_sync_service(mock_db, mock_mongo_client, outgoing):
    from services.hp_service import fetch_and_cache_hp_call_center

    w = _world()
    c = _centres(w)[0]
    await _connect(mock_db)
    await _group(mock_db, c)
    result = await fetch_and_cache_hp_call_center(SBX, c.ghl_location_id, mock_mongo_client, mode="backfill",
                                                  location_name=c.name, client_group_name=c.name)
    people = w.leads(c, w.history_start, w.today)
    expected = w.calls(c, w.history_start, w.today)
    assert result["success"] and result["complete"]
    assert result["total_leads"] == len(people) and result["total_calls"] == len(expected)
    assert result["inbound"] + result["outbound"] == len(expected)

    leads = await mock_db["hotprospector_leads"].find({"user_id": SBX}).to_list(None)
    # Every call matched its lead by phone: no "Unmatched calls" bucket.
    assert len(leads) == len(people)
    assert not any(d["lead_data"].get("_is_unmatched_bucket") for d in leads)
    by_id = {d["lead_data"]["id"]: d["lead_data"] for d in leads}
    for lead in people[:: max(1, len(people) // 20)]:
        stored = by_id[w.hp_lead_id(lead.id)]
        assert stored["call_logs_count"] == len(w.calls_for(lead))

    logs = await mock_db["call_logs"].find({"user_id": SBX}).to_list(None)
    assert len(logs) == len(expected) and all(d["recording_url"] is None for d in logs)
    group = await mock_db["client_groups"].find_one({"id": f"grp-{c.key}"})
    assert group["hotprospector_cache"]["metrics"]["total_calls"] == len(expected)
    assert sum(r["calls"] for r in group["hp_daily_calls"]) == len(expected)

    # The nightly incremental run adds nothing twice.
    again = await fetch_and_cache_hp_call_center(SBX, c.ghl_location_id, mock_mongo_client, mode="incremental",
                                                 location_name=c.name, client_group_name=c.name)
    assert again["success"] and again["total_calls"] == len(expected)


async def test_hp_tick_cron_syncs_a_sandbox_client(mock_db, outgoing, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s")
    w = _world()
    c = _centres(w)[2]
    await _connect(mock_db)
    gid = await _group(mock_db, c)
    r = _client().get("/api/cron/hp-tick", headers={"Authorization": "Bearer s"})
    assert r.status_code == 200 and r.json()["succeeded"] == 1, r.text
    group = await mock_db["client_groups"].find_one({"id": gid})
    assert group["hp_refresh_status"] == "complete" and group["hp_backfill_status"] == "complete"
    assert group["hotprospector_cache"]["metrics"]["total_leads"] == len(w.leads(c, w.history_start, w.today))
    assert outgoing == []


async def test_hp_members_tick_cron_backfills_from_the_fake(mock_db, outgoing, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "s")
    await _connect(mock_db)
    r = _client().get("/api/cron/hp-members-tick", headers={"Authorization": "Bearer s"})
    assert r.status_code == 200 and r.json()["succeeded"] == 1, r.text
    days = await mock_db["hotprospector_member_daily"].distinct("date", {"user_id": SBX})
    assert len(days) == min(20, (date.today() - date(date.today().year, 1, 1)).days)
    w = _world()
    stored = await mock_db["hotprospector_member_daily"].count_documents({"user_id": SBX})
    assert stored == len(days) * len(w.members())


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------
async def test_call_center_route_and_members_dashboard(mock_db, outgoing):
    w = _world()
    c = _centres(w)[3]
    await _connect(mock_db)
    await _group(mock_db, c)
    client = _client()
    r = client.get(f"/api/hotprospector/call-center?location_id={c.ghl_location_id}&limit=0")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert r.json()["meta"]["total"] == len(w.leads(c, w.history_start, w.today))
    logs = [log for lead in data for log in lead["call_logs"]]
    assert len(logs) == len(w.calls(c, w.history_start, w.today))
    assert all(log["recording_url"] is None for log in logs)      # the Recording column shows "-"
    assert {log["call_status"] for log in logs} <= {"inbound", "outbound"}

    today = w.today.isoformat()
    r = client.get(f"/api/hotprospector/members/dashboard?start_date={today}&end_date={today}")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert len(rows) == len(w.members()) and all(m["first_name"] and m["member_status"] for m in rows)
    ok, log = await _hp().fetch_user_call_logs(None, from_date=today, to_date=today)
    assert sum(m["dashboard"]["outboundCall"] + m["dashboard"]["inboundCall"] for m in rows) == len(log["call_logs"])
    assert outgoing == []


# ---------------------------------------------------------------------------
# No recordings: the call pages and the call-analysis tools degrade cleanly, nothing is transcribed
# ---------------------------------------------------------------------------
@pytest.fixture
def no_openai(monkeypatch):
    """Call analysis may build an OpenAI client, but must never call it for sandbox calls."""
    import openai
    from services import call_analysis_service

    class Refuse:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"OpenAI used: {name}")

    monkeypatch.setattr(openai, "AsyncOpenAI", Refuse)
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "sk-test")


async def test_call_analysis_finds_nothing_to_transcribe(mock_db, mock_mongo_client, outgoing, no_openai):
    from ai.tools import call_analysis_tools
    from services import call_analysis_service
    from services.hp_service import fetch_and_cache_hp_call_center

    w = _world()
    c = _centres(w)[0]
    await _connect(mock_db)
    gid = await _group(mock_db, c)
    await fetch_and_cache_hp_call_center(SBX, c.ghl_location_id, mock_mongo_client, location_name=c.name,
                                         client_group_name=c.name)
    member = c.members[0]

    summary = await call_analysis_tools.get_call_recordings_summary(mock_db, SBX, gid, preset="last_30d")
    assert summary["matching_calls"] > 0 and summary["with_recording"] == 0
    result = await call_analysis_tools.analyze_call_recordings(mock_db, SBX, gid, "why no bookings?",
                                                               confirmed=True, preset="last_30d")
    assert result["calls"] == [] and result["counts"]["eligible_in_window"] == 0

    est = await call_analysis_service.estimate_member_analysis(mock_db, SBX, member.name, 10)
    assert est["calls_found"] == 0 and est["estimated_credits"] == 0 and est["batches"] == 0
    batch = await call_analysis_service.analyze_member_calls(mock_db, SBX, member.name, limit=5)
    assert batch["calls"] == [] and batch["counts"]["eligible"] == 0 and batch["next_before"] is None

    # The single-call button: an HP call without a recording is refused before any download.
    doc = await mock_db["call_logs"].find_one({"user_id": SBX})
    with pytest.raises(ValueError, match="no recording"):
        await call_analysis_service.analyze_single_call(mock_db, SBX, call_id=str(doc["_id"]))
    # The world's recording URL (on the HP host, sbx_ path) answers 404 in-process: nothing transcribed.
    url = next((x.recording_url for x in w.calls(c, w.history_start, w.today) if x.recording_url), None)
    assert url and "hookscall.com" in url
    with pytest.raises(ValueError, match="no recordings"):
        await call_analysis_service.analyze_single_call(mock_db, SBX, recording_url=url)
    assert outgoing == []


async def test_call_logs_route_serves_hp_rows_without_recordings(mock_db, mock_mongo_client, outgoing):
    from services.hp_service import fetch_and_cache_hp_call_center

    w = _world()
    c = _centres(w)[4]
    await _connect(mock_db)
    gid = await _group(mock_db, c)
    await fetch_and_cache_hp_call_center(SBX, c.ghl_location_id, mock_mongo_client, location_name=c.name,
                                         client_group_name=c.name)
    r = _client().get(f"/api/call_logs?group_id={gid}&limit=500")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert rows and all(row["recording_url"] is None for row in rows)
