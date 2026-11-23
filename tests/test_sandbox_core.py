"""
Sandbox accounts, phase 1 core: who is a sandbox account, sbx_ credentials, the httpx hook that
answers sandbox provider requests in-process, the generated world and remembered writes.
"""

import logging
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

import sandbox
from sandbox import credentials, routing, state, world
from sandbox.credentials import make_credential, parse_credential
from sandbox.world import TZ, world_for

SBX = "tester@sandbox.birdy.test"
OTHER_SBX = "second@sandbox.birdy.test"
REAL = "alice@example.com"
NOW = datetime(2026, 11, 23, 15, 30, tzinfo=TZ)


@pytest.fixture(autouse=True)
def _installed(mock_db):
    """The hook is on, and the fakes read remembered writes from the in-memory database."""
    sandbox.install()
    yield


# ---------------------------------------------------------------------------
# Who is a sandbox account
# ---------------------------------------------------------------------------
def test_is_sandbox_user_by_domain(monkeypatch):
    monkeypatch.delenv("SANDBOX_EMAIL_DOMAINS", raising=False)
    assert sandbox.is_sandbox_user(SBX)
    assert sandbox.is_sandbox_user("Tester@Sandbox.Birdy.Test")
    assert not sandbox.is_sandbox_user(REAL)
    assert not sandbox.is_sandbox_user("x@notsandbox.birdy.test")
    assert not sandbox.is_sandbox_user("sandbox.birdy.test")
    assert not sandbox.is_sandbox_user(None)
    monkeypatch.setenv("SANDBOX_EMAIL_DOMAINS", "qa.example.org, @demo.test")
    assert sandbox.is_sandbox_user("a@demo.test") and sandbox.is_sandbox_user("b@qa.example.org")
    assert not sandbox.is_sandbox_user(SBX)


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["meta", "ghl", "ghl_location:AbC123xyz", "hp", "hp_uid"])
def test_credential_round_trip(kind):
    raw = make_credential(SBX, kind)
    assert raw.startswith("sbx_") and len(raw) < 140
    assert all(ch.isalnum() or ch in "-_." for ch in raw)
    cred = parse_credential(raw)
    assert (cred.user_id, cred.kind) == (SBX, kind)
    assert parse_credential(f"Bearer {raw}") == cred
    if ":" in kind:
        assert (cred.base_kind, cred.arg) == ("ghl_location", "AbC123xyz")


def test_credentials_are_random_unless_a_nonce_is_given():
    assert make_credential(SBX, "meta") != make_credential(SBX, "meta")
    assert make_credential(SBX, "hp_uid", nonce="") == make_credential(SBX, "hp_uid", nonce="")


def test_tampered_or_foreign_credentials_do_not_parse():
    raw = make_credential(SBX, "meta")
    payload, sig = raw[4:].rsplit(".", 1)
    assert parse_credential(f"sbx_{payload}.{'A' * len(sig)}") is None
    assert parse_credential("sbx_nonsense") is None
    assert parse_credential("EAAB123") is None
    with pytest.raises(ValueError):
        make_credential(SBX, "bad~kind")
    # A credential minted for a real account never parses: the fakes only serve sandbox accounts.
    assert parse_credential(make_credential(REAL, "meta")) is None


def test_credential_error_rules():
    mine, theirs = make_credential(SBX, "meta"), make_credential(OTHER_SBX, "meta")
    assert credentials.credential_error(SBX, mine) is None
    assert credentials.credential_error(SBX, "EAArealtoken")
    assert credentials.credential_error(SBX, theirs)
    assert credentials.credential_error(REAL, mine)
    assert credentials.credential_error(REAL, "EAArealtoken") is None


def test_find_credentials_needs_a_token_boundary():
    raw = make_credential(SBX, "meta")
    assert [c.raw for c in credentials.find_credentials(f"access_token={raw}&x=1")] == [raw]
    # A real JWT-ish token that happens to contain "sbx_" is not a sandbox credential.
    assert not credentials.has_marker([b"eyJhbGciOiJIUzI1NiJ9.abcsbx_def.ghi"])
    assert credentials.has_marker([f"Bearer {raw}".encode()])


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------
class Stub:
    """A transport that records what reached it (what would have left the process)."""

    def __init__(self):
        self.seen = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        self.seen.append(str(request.url))
        return httpx.Response(200, json={"real": True})


async def test_sandbox_request_is_answered_in_process_and_never_reaches_the_transport():
    stub, token = Stub(), make_credential(SBX, "meta")
    async with httpx.AsyncClient(transport=stub.transport) as client:
        r = await client.get("https://graph.facebook.com/v25.0/me", params={"access_token": token})
    assert r.status_code == 200 and "real" not in r.json()
    assert r.json()["name"] == "Sandbox Tester"
    assert stub.seen == []


async def test_non_sandbox_requests_go_out_untouched():
    stub = Stub()
    async with httpx.AsyncClient(transport=stub.transport) as client:
        r1 = await client.get("https://graph.facebook.com/v25.0/me", params={"access_token": "EAArealtoken"})
        r2 = await client.post("https://api.openai.com/v1/x", json={"note": make_credential(SBX, "meta")})
        r3 = await client.get("https://services.leadconnectorhq.com/contacts/",
                              headers={"Authorization": "Bearer eyJabc.defsbx_ghi.jkl"})
    assert [r.json() for r in (r1, r2, r3)] == [{"real": True}] * 3
    assert len(stub.seen) == 3


async def test_credential_in_header_or_body_is_routed_too():
    stub = Stub()
    async with httpx.AsyncClient(transport=stub.transport) as client:
        r1 = await client.post("https://services.leadconnectorhq.com/oauth/locationToken",
                               headers={"Authorization": f"Bearer {make_credential(SBX, 'ghl')}"},
                               data={"companyId": "nope", "locationId": "nope"})
        r2 = await client.post("https://service.hookscall.com/glu/api/v2/auth/token",
                               json={"api_uId": make_credential(SBX, "hp_uid", nonce=""),
                                     "api_key": make_credential(SBX, "hp_key", nonce="")})
    assert r1.status_code == 400 and r1.json()["statusCode"] == 400
    assert r2.status_code == 200 and r2.json()["data"]["access_token"].startswith("sbx_")
    assert stub.seen == []


async def test_stream_and_mounts_are_covered():
    stub, token = Stub(), make_credential(SBX, "meta")
    async with httpx.AsyncClient(mounts={"all://": stub.transport}) as client:
        async with client.stream("GET", "https://graph.facebook.com/v25.0/me/permissions",
                                 params={"access_token": token}) as r:
            body = await r.aread()
    assert r.status_code == 200 and b"read_insights" in body
    assert stub.seen == []


async def test_unregistered_provider_host_answers_not_implemented(monkeypatch, caplog):
    monkeypatch.setitem(routing.PROVIDER_HOSTS, ".provider.test", "test")
    stub = Stub()
    caplog.set_level(logging.WARNING, logger="sandbox.routing")
    async with httpx.AsyncClient(transport=stub.transport) as client:
        r = await client.get("https://api.provider.test/v1/things", params={"key": make_credential(SBX, "meta")})
    assert r.status_code == 501
    assert r.json()["error"]["message"] == "sandbox: GET /v1/things is not implemented"
    assert stub.seen == []
    assert any("sandbox_unhandled" in rec.getMessage() for rec in caplog.records)


async def test_fake_endpoint_gap_answers_in_the_providers_shape(caplog):
    caplog.set_level(logging.WARNING, logger="sandbox.fakes")
    token = make_credential(SBX, "meta")
    async with httpx.AsyncClient(transport=Stub().transport) as client:
        r = await client.get("https://graph.facebook.com/v25.0/act_1/nothing_here/deeper", params={"access_token": token})
        g = await client.get("https://services.leadconnectorhq.com/nothing",
                             headers={"Authorization": f"Bearer {make_credential(SBX, 'ghl')}"})
        h = await client.post("https://service.hookscall.com/glu/api/v2/request",
                              headers={"Authorization": f"Bearer {make_credential(SBX, 'hp')}"},
                              json={"Method": "NoSuchMethod"})
    assert r.status_code == 400 and r.json()["error"]["code"] == 100
    assert r.json()["error"]["message"] == "sandbox: GET /act_1/nothing_here/deeper is not implemented"
    assert g.status_code == 404 and g.json()["message"].startswith("sandbox: GET /nothing")
    assert h.status_code == 200 and h.json()["response"] == "false" and "NoSuchMethod" in h.json()["message"]
    assert sum("sandbox_unhandled" in rec.getMessage() for rec in caplog.records) == 3


async def test_forged_sandbox_marker_still_never_leaves():
    stub = Stub()
    async with httpx.AsyncClient(transport=stub.transport) as client:
        r = await client.get("https://graph.facebook.com/v25.0/me", params={"access_token": "sbx_forged.AAAAAAAAAAAA"})
    assert r.status_code == 400 and r.json()["error"]["code"] == 190
    assert stub.seen == []


def test_sync_client_refuses_sandbox_requests():
    stub = Stub()
    with httpx.Client(transport=stub.transport) as client:
        with pytest.raises(routing.SandboxLeakBlocked):
            client.get("https://graph.facebook.com/v25.0/me", params={"access_token": make_credential(SBX, "meta")})
        assert client.get("https://graph.facebook.com/v25.0/me").json() == {"real": True}
    assert len(stub.seen) == 1


def test_install_is_idempotent():
    sandbox.install()
    sandbox.install()
    assert routing.installed()


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------
def test_world_shape():
    w = world_for(SBX, now=NOW)
    assert len(w.clients) == 8
    assert sum(c.call_centre for c in w.clients) == 5
    assert sorted(c.health for c in w.clients) == sorted(world.HEALTH_PLAN)
    assert len({c.ghl_location_id for c in w.clients}) == 8
    assert len({c.meta_ad_account_id for c in w.clients}) == 8
    for c in w.clients:
        assert c.currency == "GBP" and c.timezone == "Europe/London"
        assert 3 <= len(c.campaigns) <= 5
        for camp in c.campaigns:
            assert 1 <= len(camp.adsets) <= 3
            for adset in camp.adsets:
                live = [ad for ad in adset.ads if ad.status == "ACTIVE"]
                assert 2 <= len(live) <= 4
        assert w.client(c.ghl_location_id) is c and w.client(c.meta_ad_account_id) is c
        assert w.client(c.meta_account_number) is c
        if c.call_centre:
            assert w.client(c.hp_group_id) is c and len(c.members) >= 3


def test_world_is_deterministic_and_per_user():
    a = world_for(SBX, now=NOW)
    c = a.clients[2]
    first = (a.metrics(c, NOW.date() - timedelta(days=30), NOW.date()),
             [p.id for p in a.leads(c, NOW.date() - timedelta(days=3), NOW.date())])
    world.clear_cache()
    b = world_for(SBX, now=NOW)
    assert [x.name for x in b.clients] == [x.name for x in a.clients]
    assert (b.metrics(b.clients[2], NOW.date() - timedelta(days=30), NOW.date()),
            [p.id for p in b.leads(b.clients[2], NOW.date() - timedelta(days=3), NOW.date())]) == first
    other = world_for(OTHER_SBX, now=NOW)
    assert [x.ghl_location_id for x in other.clients] != [x.ghl_location_id for x in a.clients]


def test_totals_are_consistent():
    w = world_for(SBX, now=NOW)
    since, until = NOW.date() - timedelta(days=13), NOW.date()
    for c in w.clients:
        camp = c.campaigns[0]
        for day_row in w.daily(camp, since, until):
            ads = [w.ad_day(ad, day_row.date) for ad in w.ads(camp)]
            assert round(sum(m.spend for m in ads), 2) == day_row.spend
            assert sum(m.leads for m in ads) == day_row.leads
            assert sum(m.impressions for m in ads) == day_row.impressions
        total = w.metrics(c, since, until)
        assert total.leads == len(w.leads(c, since, until))
        assert total.leads == sum(r.leads for r in w.daily(c, since, until))
        assert total.days == 14
        for ad in w.ads(c)[:5]:
            for day in w.days(since, until):
                assert w.ad_day(ad, day).leads == len(w.ad_leads(ad, day))


def test_no_future_days_and_today_is_partial():
    morning = world_for(SBX, now=NOW.replace(hour=8, minute=0))
    evening = world_for(SBX, now=NOW.replace(hour=22, minute=0))
    c = morning.clients[0]
    rows = morning.daily(c, NOW.date() - timedelta(days=2), NOW.date() + timedelta(days=10))
    assert rows[-1].date == NOW.date() and len(rows) == 3
    assert morning.ad_day(morning.ads(c)[0], NOW.date() + timedelta(days=1)) is None
    am, pm = morning.metrics(c, NOW.date(), NOW.date()), evening.metrics(c, NOW.date(), NOW.date())
    assert 0 < am.spend < pm.spend
    am_ids = {p.id for p in morning.leads(c, NOW.date(), NOW.date())}
    pm_leads = evening.leads(c, NOW.date(), NOW.date())
    assert am_ids <= {p.id for p in pm_leads}
    assert all(p.created_time <= morning.now for p in morning.leads(c, NOW.date(), NOW.date()))
    # Yesterday is complete and the same whatever the time today.
    y = NOW.date() - timedelta(days=1)
    assert morning.metrics(c, y, y) == evening.metrics(c, y, y)


def test_history_never_changes_as_days_pass():
    today = world_for(SBX, now=NOW)
    later = world_for(SBX, now=NOW + timedelta(days=9))
    since, until = NOW.date() - timedelta(days=60), NOW.date() - timedelta(days=1)
    for a, b in zip(today.clients, later.clients):
        if a.health == "no_leads":
            # The one deliberate exception: its last NO_LEADS_DAYS are dead relative to today, so a
            # day that leaves that window gets its leads (spend never changes).
            assert today.metrics(a, since, until).spend == later.metrics(b, since, until).spend
            continue
        assert today.metrics(a, since, until) == later.metrics(b, since, until)
        assert [p.id for p in today.leads(a, until, until)] == [p.id for p in later.leads(b, until, until)]


def test_performance_differs_by_client_on_purpose():
    w = world_for(SBX, now=NOW)
    since, until = NOW.date() - timedelta(days=29), NOW.date() - timedelta(days=5)
    ratio = {}
    for c in w.clients:
        m = w.metrics(c, since, until)
        ratio.setdefault(c.health, []).append(m.cpl / c.target_cpl)
    assert max(ratio["healthy"]) < 1.0 < min(ratio["over_cpl"])
    assert min(ratio["over_cpl"]) > 1.4
    dead = next(c for c in w.clients if c.health == "no_leads")
    recent = w.metrics(dead, NOW.date() - timedelta(days=2), NOW.date())
    assert recent.spend > 0 and recent.leads == 0


def test_people_line_up_across_providers():
    w = world_for(SBX, now=NOW)
    c = next(x for x in w.clients if x.call_centre)
    leads = w.leads(c, NOW.date() - timedelta(days=40), NOW.date() - timedelta(days=20))
    assert leads
    assert len({p.email for p in leads}) == len(leads) and len({p.phone for p in leads}) == len(leads)
    with_opp = 0
    for p in leads[:60]:
        assert w.lead(p.id) == p
        contact = w.contact_for(p)
        assert (contact.email, contact.phone, contact.ad_id) == (p.email, p.phone, p.ad_id)
        assert w.contact(contact.id) == contact and contact.location_id == c.ghl_location_id
        assert contact.hp_lead_id and w.hp_lead(contact.hp_lead_id) == p
        opp = w.opportunity_for(p)
        if opp:
            with_opp += 1
            assert w.opportunity(opp.id) == opp and opp.pipeline_id == c.pipeline.id
            assert opp.created_at >= p.created_time and opp.last_stage_change_at <= w.now
            assert (opp.monetary_value > 0) == (opp.status == "won")
        for call in w.calls_for(p):
            assert call.started_at <= w.now and call.group_id == c.hp_group_id
            assert call.member_id in {m.id for m in c.members}
            assert call.recording_url is None or call.answered
    assert with_opp > 30
    assert w.lead("90000000000000000") is None
    assert world_for(OTHER_SBX, now=NOW).lead(leads[0].id) is None


def test_calls_only_for_call_centres_and_within_range():
    w = world_for(SBX, now=NOW)
    since, until = NOW.date() - timedelta(days=6), NOW.date()
    for c in w.clients:
        calls = w.calls(c, since, until)
        if not c.call_centre:
            assert calls == []
            continue
        assert calls
        assert all(since <= x.started_at.date() <= until and x.started_at <= w.now for x in calls)
        assert any(x.direction == "outbound" for x in calls)


def test_world_is_cheap():
    world.clear_cache()
    start = time.perf_counter()
    w = world_for("speed@sandbox.birdy.test", now=NOW)
    for c in w.clients:
        w.metrics(c, NOW.date() - timedelta(days=119), NOW.date())
    first = time.perf_counter() - start
    start = time.perf_counter()
    w2 = world_for("speed@sandbox.birdy.test", now=NOW + timedelta(minutes=5))
    for c in w2.clients:
        w2.metrics(c, NOW.date() - timedelta(days=119), NOW.date())
    again = time.perf_counter() - start
    assert first < 3.0 and again < first


# ---------------------------------------------------------------------------
# Remembered writes
# ---------------------------------------------------------------------------
async def test_state_get_set_list(mock_db):
    assert await state.get(SBX, "ghl_note", "n1") is None
    await state.set(SBX, "ghl_note", "n1", {"body": "hi"})
    await state.set(SBX, "ghl_note", "n1", {"body": "edited"})
    await state.set(SBX, "ghl_tag", "c1", ["vip"])
    await state.set(OTHER_SBX, "ghl_note", "n9", {"body": "theirs"})
    assert await state.get(SBX.upper(), "ghl_note", "n1") == {"body": "edited"}
    notes = await state.list(SBX, "ghl_note")
    assert [(d["key"], d["value"]) for d in notes] == [("n1", {"body": "edited"})]
    assert {d["kind"] for d in await state.list(SBX)} == {"ghl_note", "ghl_tag"}
    assert await state.delete(SBX, "ghl_tag", "c1")
    assert await mock_db[state.COLLECTION].count_documents({}) == 3 - 1


async def test_overrides_change_reads_from_the_moment_they_were_made(mock_db):
    base = world_for(SBX, now=NOW)
    c = base.clients[0]
    ad = next(a for a in base.ads(c) if base.effective_status(a) == "ACTIVE")
    adset = base.adset_of(ad)
    paused_at = datetime.combine(NOW.date() - timedelta(days=3), datetime.min.time(), TZ) + timedelta(hours=12)
    await state.set(SBX, state.STATUS, ad.id, "PAUSED", at=paused_at)
    await state.set(SBX, state.NAME, ad.id, "Renamed ad", at=paused_at)
    budget_at = datetime.combine(NOW.date() - timedelta(days=10), datetime.min.time(), TZ)
    await state.set(SBX, state.BUDGET, adset.id, adset.daily_budget * 2, at=budget_at)

    w = world_for(SBX, now=NOW, overrides=await state.overrides(SBX))
    new_ad = w.find(ad.id)
    assert (new_ad.status, new_ad.name) == ("PAUSED", "Renamed ad")
    assert w.find(adset.id).daily_budget == adset.daily_budget * 2
    assert base.find(ad.id).status == "ACTIVE"          # the cached base world is untouched

    pause_day = paused_at.date()
    for day in (pause_day + timedelta(days=1), NOW.date()):
        assert w.ad_day(new_ad, day).spend == 0 and w.ad_day(new_ad, day).leads == 0
    half = w.ad_day(new_ad, pause_day)
    assert 0 < half.spend < base.ad_day(ad, pause_day).spend * 2   # half a day at double budget
    assert all(p.created_time < paused_at for p in w.ad_leads(new_ad, pause_day))
    before = budget_at.date() - timedelta(days=1)
    assert w.ad_day(new_ad, before) == base.ad_day(ad, before)
    sibling = next((a for a in adset.ads if a.id != ad.id and a.created_on < budget_at.date()
                    and (a.paused_on is None or a.paused_on > NOW.date())), None)
    if sibling:
        day = budget_at.date() + timedelta(days=1)
        assert w.ad_day(w.find(sibling.id), day).spend == pytest.approx(base.ad_day(sibling, day).spend * 2, abs=0.02)
    # Consistency still holds with writes applied.
    assert w.metrics(c, NOW.date() - timedelta(days=13), NOW.date()).leads == len(
        w.leads(c, NOW.date() - timedelta(days=13), NOW.date()))

    # Re-enabling later turns delivery back on from that moment.
    await state.set(SBX, state.STATUS, ad.id, "ACTIVE", at=NOW - timedelta(hours=1))
    w2 = world_for(SBX, now=NOW, overrides=await state.overrides(SBX))
    assert w2.ad_day(w2.find(ad.id), NOW.date()).spend > 0
    assert w2.ad_day(w2.find(ad.id), pause_day + timedelta(days=1)).spend == 0


async def test_campaign_pause_stops_its_ads(mock_db):
    base = world_for(SBX, now=NOW)
    camp = base.clients[1].campaigns[0]
    await state.set(SBX, state.STATUS, camp.id, "PAUSED", at=NOW - timedelta(days=5))
    w = world_for(SBX, now=NOW, overrides=await state.overrides(SBX))
    assert w.metrics(w.find(camp.id), NOW.date() - timedelta(days=4), NOW.date()).spend == 0
    assert all(w.effective_status(ad) == "CAMPAIGN_PAUSED" for ad in w.ads(w.find(camp.id)))
