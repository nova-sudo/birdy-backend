"""
Sandbox accounts: the leak guards. For a sandbox account no request may reach a real Meta,
GoHighLevel or Hot Prospector host, and no real message may reach a (fake) lead. Each guard added in
the leak audit has a test here; requests that reach httpx's real transport fail the test.
"""

import httpx
import pytest

import sandbox
from integrations import hotprospector as hp
from integrations.hotprospector import HotProspectorIntegration
from sandbox import routing
from sandbox.hp_app import sandbox_hp_credentials
from sandbox.world import world_for

SBX = "tester@sandbox.birdy.test"
REAL = "alice@example.com"


@pytest.fixture(autouse=True)
def _installed(mock_db):
    sandbox.install()
    hp._token_cache.clear()     # tokens are per process: no test may lean on another's
    yield
    hp._token_cache.clear()


def _client(user):
    import time

    import jwt as pyjwt
    from fastapi.testclient import TestClient

    from core.config import JWT_ALGORITHM, JWT_SECRET
    from main import app

    token = pyjwt.encode({"sub": user, "exp": int(time.time()) + 600, "type": "access"}, JWT_SECRET,
                         algorithm=JWT_ALGORITHM)
    client = TestClient(app)
    client.cookies.set("auth_token", token)
    return client


@pytest.fixture
def outgoing(monkeypatch):
    """Every request that reached httpx's real transport. Answers 200 {} so callers carry on."""
    seen = []

    async def record(self, request):
        seen.append(str(request.url))
        return httpx.Response(200, json={}, request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record)
    yield seen


# ---------------------------------------------------------------------------
# Hot Prospector hosts set through env
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["v2", "v1"])
async def test_an_hp_host_override_cannot_leak_a_sandbox_credential(mock_db, outgoing, monkeypatch, version):
    monkeypatch.setenv("HOTPROSPECTOR_BASE_URL", "https://hp-proxy.example.net/glu/api/v2/request")
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "api_version", version)
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "base_url", "https://hp-proxy.example.net/glu/api/v2/request")
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "auth_token_url", "https://auth.hp-proxy.example.net/glu/api/v2/auth/token")
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "auth_refresh_url", "https://auth.hp-proxy.example.net/glu/api/v2/auth/refresh")
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "legacy_base_url", "http://legacy.hp-proxy.example.net/glu/custom_api")
    hp._token_cache.clear()
    assert routing.provider_of("hp-proxy.example.net") == "hp"
    assert routing.provider_of("auth.hp-proxy.example.net") == "hp"

    ok, members = await HotProspectorIntegration(*sandbox_hp_credentials(SBX)).get_member_users()
    assert ok and len(members) == len(world_for(SBX).members())
    assert outgoing == []
    hp._token_cache.clear()


async def test_an_hp_host_override_leaves_normal_requests_alone(outgoing, monkeypatch):
    monkeypatch.setitem(hp.HOTPROSPECTOR_CONFIG, "base_url", "https://hp-proxy.example.net/glu/api/v2/request")
    async with httpx.AsyncClient() as client:
        await client.post("https://hp-proxy.example.net/glu/api/v2/request", json={"Method": "getMemberUsers"},
                          headers={"Authorization": "Bearer real-token"})
    assert outgoing == ["https://hp-proxy.example.net/glu/api/v2/request"]


def test_without_an_override_only_hps_own_hosts_count(monkeypatch):
    for name in ("HOTPROSPECTOR_BASE_URL", "HOTPROSPECTOR_AUTH_URL", "HOTPROSPECTOR_REFRESH_URL",
                 "HOTPROSPECTOR_LEGACY_URL"):
        monkeypatch.delenv(name, raising=False)
    assert routing.provider_of("api.openai.com") is None
    assert routing.provider_of("service.hookscall.com") == "hp"
    assert routing.provider_of("app.hotprospector.com") == "hp"


# ---------------------------------------------------------------------------
# Call analysis: a sandbox account never downloads an ad-hoc recording URL
# ---------------------------------------------------------------------------
@pytest.fixture
def no_openai(monkeypatch):
    import openai
    from services import call_analysis_service

    class Refuse:
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"OpenAI used: {name}")

    monkeypatch.setattr(openai, "AsyncOpenAI", Refuse)
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "sk-test")


@pytest.mark.parametrize("url", ["https://cdn.example.org/real-call.mp3",
                                 "https://services.leadconnectorhq.com/recordings/abc.mp3"])
async def test_sandbox_account_cannot_analyze_an_arbitrary_recording_url(mock_db, outgoing, no_openai,
                                                                       monkeypatch, url):
    import credits_middleware

    async def has_credits(*a, **k):
        return None

    monkeypatch.setattr(credits_middleware, "check_credits", has_credits)
    r = _client(SBX).post("/api/call_logs/analyze", json={"recording_url": url})
    assert r.status_code == 400 and "no recordings" in r.json()["detail"]
    assert outgoing == []


async def test_normal_account_ad_hoc_recording_path_is_unchanged(mock_db, outgoing, no_openai):
    from services import call_analysis_service

    with pytest.raises(ValueError, match="Could not transcribe"):
        await call_analysis_service.analyze_single_call(mock_db, REAL, recording_url="https://cdn.example.org/a.mp3")
    assert outgoing == ["https://cdn.example.org/a.mp3"]   # downloaded as before (then the mock refused OpenAI)


# ---------------------------------------------------------------------------
# Onboarding import: a sandbox account can only hold its own generated ids
# ---------------------------------------------------------------------------
def test_foreign_provider_id():
    from sandbox.connect import foreign_provider_id

    w = world_for(SBX)
    c = w.clients[0]
    assert foreign_provider_id(SBX, c.ghl_location_id, c.meta_ad_account_id) is None
    assert foreign_provider_id(SBX, c.ghl_location_id, c.meta_account_number) is None
    assert foreign_provider_id(SBX, "RealLocation12345678") == "RealLocation12345678"
    assert foreign_provider_id(SBX, c.ghl_location_id, "act_1234567890") == "act_1234567890"
    assert foreign_provider_id(SBX, c.key) == c.key                     # a key is not a location id
    other = world_for("second@sandbox.birdy.test").clients[0].ghl_location_id
    assert foreign_provider_id(SBX, other) == other
    assert foreign_provider_id(REAL, "anything", "act_1") is None        # normal accounts: unchanged


@pytest.mark.parametrize("account", [{"location_id": "RealLocation12345678", "name": "Real"},
                                     {"location_id": None, "name": "x", "meta_ad_account_id": "act_99"}])
async def test_sandbox_import_refuses_ids_outside_its_world(mock_db, outgoing, account):
    if account["location_id"] is None:
        account["location_id"] = world_for(SBX).clients[1].ghl_location_id
    r = _client(SBX).post("/api/onboarding/import-subaccounts", json={"accounts": [account]})
    assert r.status_code == 400 and "not one of this sandbox account" in r.json()["detail"]
    assert await mock_db["client_groups"].count_documents({}) == 0
    assert outgoing == []
