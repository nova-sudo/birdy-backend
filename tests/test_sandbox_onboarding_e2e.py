"""
Sandbox accounts, end to end: a brand-new sandbox account goes through the whole account onboarding
with all three fakes together, through the product's real HTTP routes in the order the frontend
calls them (/welcome: basics, Meta, GoHighLevel, calling, the import dialog), then the first syncs
the way production runs them (the cron ticks), then the pages a tester sees first, one write each
way, and the agent-onboarding prerequisites.

The tests run in file order on one shared walk (module scope): each is a step, so a failure names
the step. Nothing may leave the process for the whole walk: httpx's real transport fails the walk,
every OpenAI / Anthropic client refuses to be built, and the fakes may not answer "not implemented".
"""

import asyncio
import logging
import time
import types
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

import core.mongo_client
import sandbox
from core.database import DB_NAME
from sandbox import parse_credential
from sandbox.world import world_for

SBX = "tester@sandbox.birdy.test"
PASSWORD = "sandbox-pass-1"
FB_REDIRECT = "https://api.example.test/oauth/callback/facebook"
GHL_REDIRECT = "https://api.example.test/oauth/callback"
CRON = {"Authorization": "Bearer e2e-cron"}
TICKS = ("ghl-tokens", "ghl-tick", "meta-tick", "hp-tick", "hp-members-tick")


class _Refuse:
    """Stands in for every model client: building one fails the walk."""
    calls: list = []

    def __init__(self, *a, **k):
        _Refuse.calls.append(type(self).__name__)
        raise AssertionError("a model client was built during the sandbox onboarding walk")


class _Problems(logging.Handler):
    """A fake that answered "not implemented" or crashed is a gap in the fake."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.seen = []

    def emit(self, record):
        msg = record.getMessage()
        if "sandbox_unhandled" in msg or "sandbox_error" in msg:
            self.seen.append(msg)


class _IdentityIndex:
    """mongomock finds a document by scanning the whole collection, so an upsert per contact / lead /
    call is quadratic: the first sync of eight clients (~8k contacts, ~8k HP leads, ~15k calls) spent
    minutes in mongomock's filter matching, not in the product or the fakes (Mongo itself has indexes
    for these keys). This narrows a lookup that pins one of a few identity fields to the documents
    holding that value, then lets mongomock match as usual, so results are unchanged. The fields are
    ones the product never rewrites to another value in place."""

    KEYS = ("contact_id", "lead_data.id", "source_event_id", "lead_id", "ad_id", "opportunity_id", "agentId",
            "client_group_id", "ghl_location_id")
    _MISSING = object()

    @classmethod
    def value(cls, doc, path):
        for part in path.split("."):
            if not isinstance(doc, dict) or part not in doc:
                return cls._MISSING
            doc = doc[part]
        return doc if isinstance(doc, (str, int)) and not isinstance(doc, bool) else cls._MISSING

    @classmethod
    def install(cls, mp):
        from mongomock import collection as mc
        from mongomock import filtering
        from mongomock import store as ms

        original_iter = mc.Collection._iter_documents
        original_set, original_del = ms.CollectionStore.__setitem__, ms.CollectionStore.__delitem__
        original_drop = ms.CollectionStore.drop

        def tables(store):
            if "_e2e_index" not in store.__dict__:
                store.__dict__["_e2e_index"] = {}
            return store.__dict__["_e2e_index"]

        def table(store, key):
            t = tables(store)
            if key not in t:
                by_value, by_id = {}, {}
                for oid, doc in store._documents.items():
                    v = cls.value(doc, key)
                    if v is not cls._MISSING:
                        by_value.setdefault(v, {})[oid] = doc
                        by_id[oid] = v
                t[key] = (by_value, by_id)
            return t[key]

        def unindex(store, oid):
            for by_value, by_id in tables(store).values():
                old = by_id.pop(oid, cls._MISSING)
                if old is not cls._MISSING:
                    by_value.get(old, {}).pop(oid, None)

        def set_item(store, oid, doc):
            original_set(store, oid, doc)
            unindex(store, oid)
            for key, (by_value, by_id) in tables(store).items():
                v = cls.value(doc, key)
                if v is not cls._MISSING:
                    by_value.setdefault(v, {})[oid] = doc
                    by_id[oid] = v

        def del_item(store, oid):
            original_del(store, oid)
            unindex(store, oid)

        def iter_documents(self, filter):
            store = self._store
            if isinstance(filter, dict) and not store.is_empty:
                for key in cls.KEYS:
                    wanted = filter.get(key, cls._MISSING)
                    if isinstance(wanted, (str, int)) and not isinstance(wanted, bool):
                        docs = store._documents
                        found = [docs[o] for o in list(table(store, key)[0].get(wanted, {})) if o in docs]
                        return (d for d in found if filtering.filter_applies(filter, d))
            return original_iter(self, filter)

        def drop(store):
            original_drop(store)
            tables(store).clear()

        mp.setattr(mc.Collection, "_iter_documents", iter_documents)
        mp.setattr(ms.CollectionStore, "drop", drop)
        mp.setattr(ms.CollectionStore, "__setitem__", set_item)
        mp.setattr(ms.CollectionStore, "__delitem__", del_item)


@pytest.fixture(scope="module")
def walk():
    """One sandbox account's onboarding, shared by the steps below."""
    import openai

    from integrations import hotprospector as hp
    from integrations.facebook_utils.facebook import facebook_integration
    from integrations.gohighlevel import ghl_integration
    from main import app

    mp = pytest.MonkeyPatch()
    _IdentityIndex.install(mp)
    # Every product httpx.AsyncClient builds its own TLS context (a CA bundle load, ~4 ms); nothing
    # here is sent over TLS, so the walk builds the default one once.
    from httpx._transports import default as httpx_default

    shared_ssl = {}

    def ssl_context(verify=True, cert=None, trust_env=True):
        if verify is True and cert is None:
            if trust_env not in shared_ssl:
                shared_ssl[trust_env] = real_ssl_context(verify, cert, trust_env)
            return shared_ssl[trust_env]
        return real_ssl_context(verify, cert, trust_env)

    real_ssl_context = httpx_default.create_ssl_context
    mp.setattr(httpx_default, "create_ssl_context", ssl_context)
    mongo = AsyncMongoMockClient()
    previous_client = core.mongo_client._client
    core.mongo_client._client = mongo
    sandbox.install()
    hp._token_cache.clear()
    # utils/currency_exchange.py opens its own Motor client from MONGODB_URI (the real database):
    # it reads the same users collection as everything else here.
    from utils.currency_exchange import CurrencyService

    mp.setattr(CurrencyService, "_get_users_collection", staticmethod(lambda: mongo[DB_NAME]["users"]))

    left = []

    async def leak(self, request):
        left.append(str(request.url))
        raise AssertionError(f"left the process: {request.url}")

    mp.setattr(httpx.AsyncHTTPTransport, "handle_async_request", leak)
    # The product waits between provider calls (rate limits); against the fakes that is dead time.
    real_sleep = asyncio.sleep

    async def no_wait(delay=0, result=None):
        return await real_sleep(0, result)

    mp.setattr(asyncio, "sleep", no_wait)
    # A key is set so a model call would really be attempted (and refused), not skipped for want of one.
    mp.setenv("OPENAI_API_KEY", "sk-e2e-not-a-key")
    mp.setenv("CRON_SECRET", CRON["Authorization"].split()[1])
    mp.setattr(openai, "AsyncOpenAI", type("AsyncOpenAI", (_Refuse,), {}))
    mp.setattr(openai, "OpenAI", type("OpenAI", (_Refuse,), {}))
    try:
        import anthropic

        mp.setattr(anthropic, "AsyncAnthropic", type("AsyncAnthropic", (_Refuse,), {}))
        mp.setattr(anthropic, "Anthropic", type("Anthropic", (_Refuse,), {}))
    except ImportError:
        pass
    mp.setattr(facebook_integration, "redirect_uri", FB_REDIRECT)
    mp.setattr(ghl_integration, "redirect_uri", GHL_REDIRECT)
    problems = _Problems()
    logging.getLogger("sandbox").addHandler(problems)
    _Refuse.calls.clear()

    loop = asyncio.new_event_loop()
    env = types.SimpleNamespace(
        client=TestClient(app),
        db=mongo[DB_NAME],
        run=loop.run_until_complete,
        world=lambda: world_for(SBX),
        left=left,
        problems=problems,
        started=time.monotonic(),
        timings={},
    )
    try:
        yield env
    finally:
        logging.getLogger("sandbox").removeHandler(problems)
        mp.undo()
        loop.close()
        hp._token_cache.clear()
        core.mongo_client._client = previous_client
        print(f"\nsandbox onboarding walk: {time.monotonic() - env.started:.1f}s {env.timings}")


@pytest.fixture(autouse=True)
def _clean(walk):
    """After every step: nothing left the process, no model client, no gap in a fake."""
    yield
    assert walk.left == [], f"requests left the process: {walk.left}"
    assert _Refuse.calls == [], f"model clients built: {_Refuse.calls}"
    assert walk.problems.seen == [], f"a fake could not answer: {walk.problems.seen}"


def _ok(r, status=200):
    assert r.status_code == status, f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:500]}"
    return r.json()


def _follow(client, auth_url):
    """What the browser does with the auth URL, which for a sandbox account is our own callback."""
    parts = urlsplit(auth_url)
    return client.get(f"{parts.path}?{parts.query}", follow_redirects=False)


def _user(walk):
    return walk.run(walk.db["users"].find_one({"user_id": SBX}))


def _groups(walk):
    return walk.run(walk.db["client_groups"].find({"user_id": SBX}).to_list(None))


# ---------------------------------------------------------------------------
# 1. Register, the wizard gate, the basics
# ---------------------------------------------------------------------------
def test_01_register_and_basics(walk):
    c = walk.client
    body = _ok(c.post("/api/register", json={"email": SBX, "password": PASSWORD}))
    assert body["user"]["email"] == SBX
    # Registering signs the tester in. The cookie is Secure and set for the deployed domain, which
    # the test client's http://testserver never sends back, so it is carried over by hand.
    token = next(ck.value for ck in c.cookies.jar if ck.name == "auth_token")
    c.cookies.clear()
    c.cookies.set("auth_token", token)

    status = _ok(c.get("/api/onboarding/status"))
    assert status.get("completed") is False

    _ok(c.put("/api/onboarding/state", json={"data": {
        "name": "Sandy Tester", "agency": "Sandbox Agency", "currency": "GBP", "sales_tool": "hp"}}))
    status = _ok(c.get("/api/onboarding/status"))
    assert status["name"] == "Sandy Tester" and status["agency_name"] == "Sandbox Agency"
    assert status["default_currency"] == "GBP"


# ---------------------------------------------------------------------------
# 2-4. The three connections
# ---------------------------------------------------------------------------
def test_02_connect_meta(walk):
    c = walk.client
    auth_url = _ok(c.get("/api/connect/facebook"))["auth_url"]
    assert auth_url.startswith(FB_REDIRECT + "?")
    assert parse_credential(parse_qs(urlsplit(auth_url).query)["code"][0]).kind == "meta_code"
    r = _follow(c, auth_url)
    assert r.status_code == 302 and "status=success" in r.headers["location"], r.headers.get("location")

    token = _user(walk)["integrations"]["facebook"]["access_token"]
    assert parse_credential(token).user_id == SBX and parse_credential(token).kind == "meta"
    fb = _ok(c.get("/api/status"))["facebook"]
    assert fb["connected"] is True and not fb["token_expired"]


def test_03_connect_ghl(walk):
    c = walk.client
    auth_url = _ok(c.get("/api/connect"))["auth_url"]
    assert auth_url.startswith(GHL_REDIRECT + "?")
    r = _follow(c, auth_url)
    assert r.status_code == 302 and "status=success" in r.headers["location"], r.headers.get("location")

    agency = _ok(c.get("/api/status"))["gohighlevel"]["agency"]
    assert agency["connected"] is True and not agency["token_expired"]
    assert agency["company_id"] == walk.world().agency.ghl_company_id


def test_04_connect_hot_prospector(walk):
    c = walk.client
    _ok(c.post("/api/hotprospector/connect", json={"api_uid": "typed-uid", "api_key": "typed-key"}))
    creds = _user(walk)["integrations"]["hotprospector"]["credentials"]
    assert parse_credential(creds["api_uid"]).kind == "hp_uid"   # what was typed is ignored
    assert _ok(c.get("/api/hotprospector/status")).get("connected") is True


# ---------------------------------------------------------------------------
# 5. What the pickers list
# ---------------------------------------------------------------------------
def test_05_locations_and_ad_accounts(walk):
    c = walk.client
    w = walk.world()
    locations = _ok(c.get("/api/subaccount/locations"))["locations"]
    assert sorted(loc["id"] for loc in locations) == sorted(cl.ghl_location_id for cl in w.clients)
    assert {loc["id"]: loc["name"] for loc in locations} == {cl.ghl_location_id: cl.name for cl in w.clients}

    body = _ok(c.get("/api/facebook/adaccounts"))
    accounts = body["data"]["data"]   # the route wraps Graph's own {data, paging}
    assert len(accounts) == 8 and body["meta"]["total"] == 8
    assert {a["id"]: a["name"] for a in accounts} == {cl.meta_ad_account_id: cl.name for cl in w.clients}


# ---------------------------------------------------------------------------
# 6. The import dialog's review: activity probe + ad-account matching (no model)
# ---------------------------------------------------------------------------
def test_06_review(walk):
    c = walk.client
    w = walk.world()
    # The test client runs the background job before it answers, like the wizard's first poll would see it later.
    assert _ok(c.post("/api/onboarding/prepare-review"))["started"] is True
    for _ in range(20):
        review = _ok(c.get("/api/onboarding/subaccounts-review"))
        if review["prep"]["status"] != "running":
            break
    assert review["prep"]["status"] == "complete", review["prep"]
    assert review["prep"]["done"] == review["prep"]["total"] == 8

    by_location = {cl.ghl_location_id: cl for cl in w.clients}
    assert len(review["accounts"]) == 8
    for row in review["accounts"]:
        cl = by_location[row["location_id"]]
        match = row["fb_match"]
        assert match and match["id"] == cl.meta_ad_account_id, (cl.name, row)
        today = w.today
        leads_30d = len(w.contacts(cl, today - timedelta(days=29), today))
        assert row["contact_count"] > 0
        assert row["leads_30d"] == min(leads_30d, 50) or row["leads_30d_capped"], (cl.name, row, leads_30d)
        assert row["last_lead_at"] and row["status_default"] == "active", (cl.name, row)
    # The world names an ad account after its client, so name similarity pairs all eight and the
    # model pass has nothing left to ask about (it would be answered from the world, never the model).
    assert all(row["fb_match"]["score"] for row in review["accounts"])
    assert _user(walk)["onboarding"]["review_prep"]["ai_matches"] == {}
    walk.review = review


# ---------------------------------------------------------------------------
# 7. Import every reviewed pair
# ---------------------------------------------------------------------------
def test_07_import(walk):
    c = walk.client
    # Billing stays real: past the first (free) client the dialog needs a plan. The tester picks one.
    walk.run(walk.db["users"].update_one({"user_id": SBX}, {"$set": {
        "subscription": {"status": "active", "plan_id": "scale", "extra_clients_paid": 0}}}))
    payload = [{
        "location_id": row["location_id"],
        "name": row["name"],
        "meta_ad_account_id": row["fb_match"]["id"],
        "ad_account_currency": row["fb_match"]["currency"],
        "client_status": "Active",
        "lead_collection_method": None,
    } for row in walk.review["accounts"]]
    body = _ok(c.post("/api/onboarding/import-subaccounts", json={"accounts": payload}))
    assert len(body["imported"]) == 8 and not body["skipped_limit"], body
    groups = _groups(walk)
    assert len(groups) == 8
    assert {g["call_log_provider"] for g in groups} == {"hotprospector"}
    # The token-minting background task ran (the test client runs background tasks before answering).
    assert {g["status"] for g in groups} == {"complete"}, [(g["name"], g["status"], g.get("status_message")) for g in groups]
    assert all("failed" not in (g.get("status_message") or "") for g in groups)


# ---------------------------------------------------------------------------
# 8. The first syncs, the way production runs them
# ---------------------------------------------------------------------------
def _synced(g):
    return all(g.get(k) for k in ("last_ghl_refresh", "last_meta_refresh", "last_hp_refresh"))


def test_08_first_syncs(walk):
    c = walk.client
    rounds = 0
    for rounds in range(1, 25):
        for tick in TICKS:
            t0 = time.monotonic()
            body = _ok(c.get(f"/api/cron/{tick}", headers=CRON))
            walk.timings[tick] = round(walk.timings.get(tick, 0) + time.monotonic() - t0, 2)
            assert "refresh_error" not in body, (tick, body)
        groups = _groups(walk)
        if all(_synced(g) for g in groups):
            break
    errors = [(g["name"], g.get("ghl_refresh_error"), g.get("hp_refresh_error")) for g in groups
              if g.get("ghl_refresh_status") == "error" or g.get("hp_refresh_status") == "error"]
    assert not errors, errors
    assert all(_synced(g) for g in groups), [
        (g["name"], {k: bool(g.get(k)) for k in ("last_ghl_refresh", "last_meta_refresh", "last_hp_refresh")})
        for g in groups]
    walk.timings["rounds"] = rounds
    jobs = walk.run(walk.db["meta_refresh_jobs"].find({}).to_list(None))
    assert jobs and all(j.get("status") == "complete" for j in jobs), [(j.get("status"), j.get("error")) for j in jobs]



# ---------------------------------------------------------------------------
# 9. What a tester sees first, and that it agrees with the world
# ---------------------------------------------------------------------------
def _as_utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _ghl_window_contacts(w, cl, group, days):
    """The contacts GHL's last_Nd window counts: UTC dateAdded from today-N through today, as of the
    group's last GHL sync (core.constants.ghl_date_bounds)."""
    today = datetime.now(timezone.utc).date()
    synced = _as_utc(group["last_ghl_refresh"])
    return [x for x in w.contacts(cl, today - timedelta(days=days + 1), w.today)
            if today - timedelta(days=days) <= _as_utc(x.date_added).date() <= today
            and _as_utc(x.date_added) <= synced]


@pytest.mark.parametrize("preset,days", [("last_7d", 7), ("last_30d", 30)])
def test_09_client_groups_agree_with_the_world(walk, preset, days):
    w = walk.world()
    stored = {g["id"]: g for g in _groups(walk)}
    body = _ok(walk.client.get(f"/api/client-groups?date_preset={preset}"))
    assert body["meta"]["total_groups"] == 8 and body["meta"]["total_meta_ad_accounts"] == 8
    since, until = w.today - timedelta(days=days), w.today - timedelta(days=1)   # Meta's window ends yesterday
    for g in body["client_groups"]:
        cl = w.client(g["ghl_location_id"])
        assert g["meta_ad_account_id"] == cl.meta_ad_account_id and g["status"] == "complete"
        want = w.metrics(cl, since, until)
        insights = g["facebook"]["metrics"]["insights"]
        assert want.spend > 0 and insights["spend"] == pytest.approx(want.spend, abs=0.01), cl.name
        assert insights["impressions"] == want.impressions and insights["clicks"] == want.clicks, cl.name
        assert insights["results"] == want.leads, cl.name
        assert g["facebook"]["metrics"]["total_campaigns"] == len(cl.campaigns)
        ghl = g["gohighlevel"]["metrics"]
        assert ghl["total_contacts"] == len(_ghl_window_contacts(w, cl, stored[g["id"]], days)) > 0, cl.name
        assert ghl["lifetime_total_contacts"] > ghl["total_contacts"]
        calls = (g["hotprospector"] or {}).get("call_stats") or {}
        if cl.hp_group_id:
            assert calls.get("total_calls", 0) > 0 and g["hotprospector"]["metrics"]["total_leads"] > 0, cl.name
        else:
            assert not calls.get("total_calls"), cl.name
    assert body["meta"]["total_hotprospector_enabled"] == sum(1 for c in w.clients if c.hp_group_id)


def test_09_daily_series(walk):
    w = walk.world()
    series = _ok(walk.client.get("/api/client-groups/daily"))["series"]
    by_group = {g["id"]: w.client(g["ghl_location_id"]) for g in _groups(walk)}
    assert set(series) == set(by_group)
    for gid, s in series.items():
        cl = by_group[gid]
        spend = {r["date"]: r for r in s["daily_spend"]}
        for day in w.days(w.today - timedelta(days=7), w.today - timedelta(days=1)):
            want = w.metrics(cl, day, day)
            assert spend[day.isoformat()]["spend"] == pytest.approx(want.spend, abs=0.01), (cl.name, day)
            assert spend[day.isoformat()]["leads"] == want.leads, (cl.name, day)
        assert sum(r["leads"] for r in s["daily_leads"]) > 0
        if cl.hp_group_id:
            assert sum(r["calls"] for r in s["daily_calls"]) > 0, cl.name
        else:
            assert not any(r.get("calls") for r in (s["daily_calls"] or [])), cl.name


def test_09_dashboard_summary(walk):
    body = _ok(walk.client.get("/api/dashboard/summary"))
    assert set(body["counts"]) == {"suggestions", "alerts", "wins"}


def test_09_call_centre_is_the_hot_prospector_clients(walk):
    w = walk.world()
    c = walk.client
    everyone = _ok(c.get("/api/hotprospector/call-center?limit=1"))
    centres = [cl for cl in w.clients if cl.hp_group_id]
    assert everyone["meta"]["total"] > 0
    stored = {g["ghl_location_id"]: g for g in _groups(walk)}
    total = 0
    for cl in w.clients:
        body = _ok(c.get(f"/api/hotprospector/call-center?location_id={cl.ghl_location_id}&limit=5"))
        if cl.hp_group_id:
            synced = _as_utc(stored[cl.ghl_location_id]["last_hp_refresh"])
            people = [x for x in w.leads(cl, w.history_start, w.today) if _as_utc(x.created_time) <= synced]
            assert body["meta"]["total"] == len(people), cl.name
            assert all(row["ghl_location_id"] == cl.ghl_location_id for row in body["data"])
            assert any(row["call_logs_count"] for row in body["data"]) or body["meta"]["total"] > 5
            total += body["meta"]["total"]
        else:
            assert body["meta"]["total"] == 0, cl.name
    assert total == everyone["meta"]["total"]
    assert len(centres) == 5


def test_09_leads(walk):
    w = walk.world()
    c = walk.client
    groups = _groups(walk)
    since, until = w.today - timedelta(days=7), w.today - timedelta(days=1)
    body = _ok(c.get(f"/api/facebook-leads/filtered?start_date={since}&end_date={until}"))
    counted = {}
    for row in body["leads"]:
        counted[row["client_group_id"]] = counted.get(row["client_group_id"], 0) + 1
    for g in groups:
        cl = w.client(g["ghl_location_id"])
        assert counted.get(g["id"], 0) == len(w.leads(cl, since, until)) > 0, cl.name
    # One lead: the Meta lead and its GoHighLevel contact are the same person.
    row = body["leads"][0]
    assert row["ghl_matched"] and row["email"].endswith("@example.com")

    g = groups[0]
    cl = w.client(g["ghl_location_id"])
    stored = walk.run(walk.db["ghl_contacts"].count_documents({"client_group_id": g["id"]}))
    table = _ok(c.get(f"/api/contacts/ghl-paginated?groups={g['id']}&page=1&limit=15"))
    assert table["meta"]["total_contacts"] == stored > 0 and len(table["contacts"]) == 15
    unified = _ok(c.get(f"/api/leads/unified?groups={g['id']}&page=1&limit=15"))
    assert unified["meta"]["total_contacts"] == stored
    opps = [o for o in (w.opportunity_for(w.lead(x.lead_id)) for x in w.contacts(cl, w.history_start, w.today)) if o]
    assert abs(unified["meta"]["stats"]["total_opportunities"] - len(opps)) <= 2

    contact = unified["contacts"][0]
    detail = _ok(c.get(f"/api/leads/contact/{contact['contactId']}"))
    person = w.contact(contact["contactId"])
    assert person is not None and person.email == contact["email"]
    assert person.email in str(detail) and person.full_name.split()[0] in str(detail)
    walk.contact_id = contact["contactId"]


def test_09_complete_onboarding(walk):
    _ok(walk.client.post("/api/onboarding/complete"))
    assert _ok(walk.client.get("/api/onboarding/status"))["completed"] is True


# ---------------------------------------------------------------------------
# 10. One write each way
# ---------------------------------------------------------------------------
def test_10_pause_an_ad_and_read_it_back(walk):
    from sandbox import state

    c = walk.client
    w = walk.world()
    g = _groups(walk)[0]
    cl = w.client(g["ghl_location_id"])
    ads = _ok(c.get("/api/client-groups?date_preset=last_7d"))["client_groups"]
    listed = next(x for x in ads if x["id"] == g["id"])["facebook"]["ads"]
    ad = next(a for a in listed if str(a.get("status", "")).upper() == "ACTIVE")
    _ok(c.post("/api/facebook/update-status", json={"object_id": ad["id"], "object_type": "ad", "status": "PAUSED"}))

    again = _ok(c.get("/api/client-groups?date_preset=last_7d"))["client_groups"]
    row = next(a for a in next(x for x in again if x["id"] == g["id"])["facebook"]["ads"] if a["id"] == ad["id"])
    assert str(row["status"]).upper() == "PAUSED"
    # The fake remembers it: Graph itself now answers PAUSED for this account.
    assert walk.run(state.get(SBX, "status", ad["id"])) == "PAUSED"
    paused = world_for(SBX, overrides=walk.run(state.overrides(SBX))).find(ad["id"])
    assert paused.status == "PAUSED" and w.client_of(paused).index == cl.index


def test_10_message_a_lead_and_read_it_in_the_conversation(walk):
    c = walk.client
    contact_id = walk.contact_id
    sent = _ok(c.post(f"/api/leads/contact/{contact_id}/messages",
                      json={"channel": "SMS", "message": "Hi, still keen on a consult?"}), 201)["message"]
    thread = _ok(c.get(f"/api/leads/contact/{contact_id}/messages"))["messages"]
    mine = next(m for m in thread if m["id"] == sent["id"])
    assert mine["body"] == "Hi, still keen on a consult?" and mine["channel"] == "SMS"


# ---------------------------------------------------------------------------
# 11. The agents' own onboarding can start (no chat, no model call)
# ---------------------------------------------------------------------------
@pytest.mark.agent_onboarding
def test_11_team_setup(walk):
    body = _ok(walk.client.get("/api/team/setup"))
    assert body["order"][0] == "birdy" and set(body["agents"]) >= {"birdy", "media_buyer", "sales_coach"}
    assert body["agents"]["birdy"]["status"] == "done"          # Birdy's own setup (this walk) is finished
    assert body["agents"]["media_buyer"]["status"] != "done"    # the agents' interviews are next
    # ~45 s on a laptop, nearly all of it the first syncs on mongomock; the bound only catches a
    # sync that went quadratic or a fake that got slow.
    walk.timings["total"] = round(time.monotonic() - walk.started, 1)
    assert walk.timings["total"] < 90, walk.timings
