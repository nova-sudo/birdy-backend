"""Scopes, tool tags and the access middleware on Birdy's MCP server."""

import pytest

from ai.mcp import mcp
from ai.mcp.access import token_scopes

ALERT_ARGS = {"name": "CPL high", "metric": "spend", "operator": "gt", "value": 100}


@pytest.mark.asyncio
async def test_every_registered_tool_is_tagged_read_or_write():
    tools = await mcp.get_tools()
    assert tools
    for name, tool in tools.items():
        assert len(tool.tags & {"read", "write"}) == 1, name
        if "live" in tool.tags:
            assert "write" in tool.tags, name


def test_token_scopes_defaults_to_full_when_claim_missing():
    assert token_scopes({"type": "internal"}) == {"birdy.read", "birdy.write"}
    assert token_scopes({"type": "mcp", "scope": "birdy.read"}) == {"birdy.read"}
    assert token_scopes({"scope": "birdy.read birdy.write"}) == {"birdy.read", "birdy.write"}


@pytest.mark.asyncio
async def test_read_only_token_cannot_call_write_tool(mcp_call):
    res = await mcp_call("create_alert", ALERT_ARGS, scope="birdy.read", token_type="mcp")
    assert res.is_error and "view-only" in res.text


@pytest.mark.asyncio
async def test_read_only_token_can_call_read_tool(mcp_call):
    res = await mcp_call("get_alerts", {}, scope="birdy.read", token_type="mcp")
    assert not res.is_error


@pytest.mark.asyncio
async def test_write_scope_token_can_call_write_tool(mcp_call):
    res = await mcp_call("create_alert", ALERT_ARGS, scope="birdy.read birdy.write", token_type="mcp")
    assert not res.is_error


@pytest.mark.asyncio
async def test_external_token_without_subscription_is_refused(mcp_call, no_subscription):
    res = await mcp_call("get_alerts", {}, token_type="mcp")
    assert res.is_error and "subscription is inactive" in res.text


@pytest.mark.asyncio
async def test_internal_token_skips_subscription_gate(mcp_call, no_subscription):
    res = await mcp_call("get_alerts", {}, token_type="internal")
    assert not res.is_error


@pytest.fixture
def pinned_minute(monkeypatch):
    """121 calls take seconds; pin the fixed window so a minute boundary can't split them."""
    monkeypatch.setattr("ai.mcp.access._minute", lambda: 28_333_333)


@pytest.mark.asyncio
async def test_external_calls_rate_limited_per_user(mcp_call, pinned_minute):
    for _ in range(120):
        res = await mcp_call("get_alerts", {}, user="alice@example.com", token_type="mcp")
        assert not res.is_error
    res = await mcp_call("get_alerts", {}, user="alice@example.com", token_type="mcp")
    assert res.is_error and "Too many Birdy requests" in res.text
    other = await mcp_call("get_alerts", {}, user="bob@example.com", token_type="mcp")
    assert not other.is_error


@pytest.mark.asyncio
async def test_internal_calls_are_not_rate_limited(mcp_call, mock_db):
    for _ in range(3):
        await mcp_call("get_alerts", {}, token_type="internal")
    assert await mock_db["mcp_rate"].count_documents({}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("token_type", [None, "refresh", "access"])
async def test_unknown_missing_or_login_type_is_rejected_before_any_tool(mcp_call, mock_db, no_subscription,
                                                                        token_type):
    # "access" is the login cookie: every user holds one, so it must not reach the tools,
    # where it used to skip subscription, credits and the rate limit.
    for name, args in (("get_alerts", {}), ("create_alert", ALERT_ARGS)):
        with pytest.raises(Exception):
            await mcp_call(name, args, token_type=token_type)
    assert await mock_db["alerts"].count_documents({}) == 0


@pytest.mark.asyncio
async def test_scoped_token_with_no_birdy_scope_is_refused(mcp_call):
    res = await mcp_call("get_alerts", {}, token_type="mcp", scope="openid profile")
    assert res.is_error and "does not grant access" in res.text
    assert not (await mcp_call("get_alerts", {}, token_type="mcp", scope="birdy.write")).is_error


def test_token_scopes_unknown_type_gets_nothing():
    assert token_scopes({}) == set()
    assert token_scopes({"type": "refresh"}) == set()
    assert token_scopes({"type": "access"}) == set()
