"""
ai/mcp_client.py is the bridge ai/orchestrator.py uses to reach the MCP
server internally. These tests cover the pure-logic pieces: the per-page
gating that decides whether an MCP client is even constructed, the internal
token minting (and its timezone-safety — see the docstring in mcp_client.py),
and that MCP_TOOL_NAMES matches what's actually registered on the server.
"""

import time

import jwt as pyjwt
import pytest

from ai import mcp_client
from core.config import JWT_SECRET, JWT_ALGORITHM


def test_needs_mcp_true_when_page_unknown_or_none():
    assert mcp_client.needs_mcp(None) is True


def test_needs_mcp_true_when_allowlist_includes_an_mcp_tool():
    assert mcp_client.needs_mcp(["get_client_groups", "get_alerts"]) is True


def test_needs_mcp_false_when_allowlist_has_no_mcp_tools():
    # None of these exist in MCP_TOOL_NAMES at time of writing; this asserts
    # the *shape* of the check, not a specific tool list.
    assert mcp_client.needs_mcp(["some_tool_not_in_mcp_yet"]) is False


def test_mint_internal_token_round_trips_and_is_not_expired():
    token = mcp_client._mint_internal_token("user@example.com")
    claims = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

    assert claims["sub"] == "user@example.com"
    assert claims["type"] == "access"
    # Must be in the future relative to real wall-clock time (time.time()),
    # not a naive-datetime.utcnow().timestamp() computation — that pattern
    # silently mints already-expired tokens on any non-UTC host.
    assert claims["exp"] > time.time()
    assert claims["exp"] <= time.time() + mcp_client._INTERNAL_TOKEN_TTL_SECONDS + 5


@pytest.mark.asyncio
async def test_mcp_tool_names_matches_server_registration():
    from ai.mcp import mcp

    registered = set((await mcp.get_tools()).keys())
    assert mcp_client.MCP_TOOL_NAMES == registered, (
        f"MCP_TOOL_NAMES is out of sync with the server's registered tools.\n"
        f"In MCP_TOOL_NAMES but not registered: {mcp_client.MCP_TOOL_NAMES - registered}\n"
        f"Registered but missing from MCP_TOOL_NAMES: {registered - mcp_client.MCP_TOOL_NAMES}"
    )
