"""Server-side PostHog events: fire-and-forget, no args or client names, never raise."""

import asyncio
import json

import pytest

from services import mcp_token_service, posthog_events
from tests.test_mcp_activity import RW, USER, temp_tools  # noqa: F401
from tests.test_mcp_tokens import seed_grant
from tests.test_mcp_oauth_router import api, login, make_request  # noqa: F401

TOOL_CALLED_KEYS = {"tool", "kind", "result_status", "credits_total", "response_chars", "duration_ms", "app_name"}


class FakeClient:
    def __init__(self, fail=False):
        self.posts, self.fail = [], fail

    async def post(self, url, json=None, **kw):
        if self.fail:
            raise RuntimeError("posthog down")
        self.posts.append((url, json))


@pytest.fixture
def ph(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(posthog_events.config, "POSTHOG_API_KEY", "phc_test")
    monkeypatch.setattr(posthog_events.config, "POSTHOG_HOST", "https://ph.example")
    monkeypatch.setattr(posthog_events, "_client", client)
    return client


async def drain():
    while posthog_events._tasks:
        await asyncio.gather(*list(posthog_events._tasks))


def events(client, name):
    return [body for _, body in client.posts if body["event"] == name]


@pytest.mark.asyncio
async def test_noop_without_key(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(posthog_events.config, "POSTHOG_API_KEY", "")
    monkeypatch.setattr(posthog_events, "_client", client)
    posthog_events.capture(USER, "mcp_connected", {})
    assert not posthog_events._tasks
    await drain()
    assert client.posts == []


def test_no_running_loop_is_a_noop(ph):
    posthog_events.capture(USER, "mcp_connected", {})  # sync context: nothing scheduled, nothing raised
    assert ph.posts == [] and not posthog_events._tasks


@pytest.mark.asyncio
async def test_payload_shape(ph):
    posthog_events.capture(USER, "mcp_refused", {"tool": "t", "reason": "bad_code"})
    await drain()
    (url, body), = ph.posts
    assert url == "https://ph.example/i/v0/e/"
    assert body == {"api_key": "phc_test", "event": "mcp_refused", "distinct_id": USER,
                    "properties": {"tool": "t", "reason": "bad_code", "$lib": "birdy-backend"}}


@pytest.mark.asyncio
async def test_posthog_failure_never_raises(ph, monkeypatch):
    monkeypatch.setattr(posthog_events, "_client", FakeClient(fail=True))
    posthog_events.capture(USER, "mcp_connected", {})
    await drain()  # the send task swallowed the error


@pytest.mark.asyncio
async def test_tool_called_properties_have_no_args_or_client_names(ph, mcp_call):
    await mcp_call("add_client_note", {"client": "Nobody", "text": "secret text 555 123 4821"}, user=USER,
                   scope=RW, token_type="mcp")
    await drain()
    (ev,) = events(ph, "mcp_tool_called")
    assert set(ev["properties"]) == TOOL_CALLED_KEYS | {"$lib"}
    assert ev["distinct_id"] == USER and ev["properties"]["tool"] == "add_client_note"
    assert "secret text" not in str(ph.posts) and "Nobody" not in str(ph.posts)


@pytest.mark.asyncio
async def test_internal_calls_send_nothing(ph, mcp_call):
    await mcp_call("get_alerts", {}, user=USER, token_type="access")
    await drain()
    assert ph.posts == []


@pytest.mark.asyncio
async def test_live_confirmed_fired_once(ph, mcp_call, temp_tools):
    pre = await mcp_call("zz_live_two_step", {}, user=USER, scope=RW, token_type="mcp")
    code = json.loads(pre.text)["confirm_code"]
    await mcp_call("zz_live_two_step", {"confirm_code": "wrong"}, user=USER, scope=RW, token_type="mcp")
    await mcp_call("zz_live_two_step", {"confirm_code": code}, user=USER, scope=RW, token_type="mcp")
    await drain()
    live = events(ph, "mcp_live_confirmed")
    assert len(live) == 1
    assert set(live[0]["properties"]) == {"tool", "app_name", "$lib"}


@pytest.mark.asyncio
async def test_refused_reason(ph, mcp_call, temp_tools):
    await mcp_call("create_alert", {"name": "x", "metric": "spend", "operator": "gt", "value": 1},
                   user=USER, scope="birdy.read", token_type="mcp")
    await mcp_call("zz_live_two_step", {"confirm_code": "wrong"}, user=USER, scope=RW, token_type="mcp")
    await drain()
    refused = events(ph, "mcp_refused")
    assert [e["properties"]["reason"] for e in refused] == ["view_only", "bad_code"]
    called = events(ph, "mcp_tool_called")
    assert [e["properties"]["kind"] for e in called if e["properties"]["tool"] != "zz_live_two_step"] == ["refused"]
    assert [e["properties"]["kind"] for e in called if e["properties"]["tool"] == "zz_live_two_step"] == ["refused"]
    assert all(set(e["properties"]) == {"tool", "reason", "$lib"} for e in refused)


@pytest.mark.asyncio
async def test_rate_limit_refusal_fires_refused(ph, mcp_call, monkeypatch):
    monkeypatch.setattr("ai.mcp.access.MCP_RATE_LIMIT_PER_MINUTE", 0)
    await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    await drain()
    assert [e["properties"]["reason"] for e in events(ph, "mcp_refused")] == ["rate_limit"]
    assert events(ph, "mcp_tool_called") == []


@pytest.mark.asyncio
async def test_connected_and_disconnected_fired(ph, mock_db):
    created = await mcp_token_service.create_mcp_token(mock_db, USER, "Laptop", access="read")
    await seed_grant(mock_db, "g1", USER, name="Claude", access="write")
    assert await mcp_token_service.revoke_connection(mock_db, USER, created["id"])
    assert await mcp_token_service.revoke_connection(mock_db, USER, "g1")
    assert not await mcp_token_service.revoke_connection(mock_db, USER, "nope")
    assert await mcp_token_service.revoke_connection(mock_db, USER, created["id"])  # already revoked: no new event
    assert await mcp_token_service.revoke_connection(mock_db, USER, "g1")
    await drain()
    (con,) = events(ph, "mcp_connected")
    assert con["properties"] == {"app_name": "Laptop", "access": "read", "method": "token", "$lib": "birdy-backend"}
    assert [e["properties"] for e in events(ph, "mcp_disconnected")] == [
        {"app_name": "Laptop", "$lib": "birdy-backend"}, {"app_name": "Claude", "$lib": "birdy-backend"}]


@pytest.mark.asyncio
async def test_oauth_approve_fires_connected_only_when_allowed(api, monkeypatch):
    seen = []
    monkeypatch.setattr(posthog_events, "capture", lambda *a: seen.append(a))
    await login(api)
    deny = await make_request(api.db)
    assert api.post(f"/api/oauth/requests/{deny}/decision", json={"allow": False}).status_code == 200
    assert seen == []
    ok = await make_request(api.db)
    assert api.post(f"/api/oauth/requests/{ok}/decision", json={"allow": True, "access": "write"}).status_code == 200
    assert seen == [("alice@example.com", "mcp_connected", {"app_name": "Claude", "access": "write", "method": "oauth"})]



@pytest.mark.asyncio
async def test_events_fire_and_result_returns_when_mongo_lookups_raise(ph, mcp_call, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr("ai.mcp.access._app_name", boom)
    monkeypatch.setattr("ai.mcp.access.mcp_billing.charge_breakdown", boom)
    res = await mcp_call("get_alerts", {}, user=USER, token_type="mcp")
    await drain()
    assert not res.is_error
    (ev,) = events(ph, "mcp_tool_called")
    assert ev["properties"]["app_name"] is None and ev["properties"]["credits_total"] is None
    assert set(ev["properties"]) == TOOL_CALLED_KEYS | {"$lib"}


@pytest.mark.asyncio
async def test_concurrent_revokes_fire_one_disconnect_per_connection(ph, mock_db):
    class _Coll:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def find_one(self, *a, **kw):  # both revokes read the live doc before either writes
            doc = await self._real.find_one(*a, **kw)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return doc

    class _Db:
        def __getitem__(self, name):
            return _Coll(mock_db[name])

    created = await mcp_token_service.create_mcp_token(mock_db, USER, "Laptop", access="read")
    await seed_grant(mock_db, "g2", USER, name="Claude", access="write")
    for cid in (created["id"], "g2"):
        results = await asyncio.gather(*[mcp_token_service.revoke_connection(_Db(), USER, cid) for _ in range(2)])
        assert results == [True, True]
    await drain()
    assert sorted(e["properties"]["app_name"] for e in events(ph, "mcp_disconnected")) == ["Claude", "Laptop"]
