"""MCP prompts (ai/mcp/prompts_mcp.py): registered, render the client name, and only name tools that exist."""

import re
import time
import types
import uuid

import pytest

from ai.mcp import mcp

PENDING: set[str] = set()  # tools a prompt names before the tool ships

PROMPT_NAMES = {"weekly_client_review", "why_did_cpl_jump", "prep_client_call", "agent_week", "daily_check_in",
                "fix_worst_client"}
CLIENT_PROMPTS = {"why_did_cpl_jump", "prep_client_call"}
TOOL_NAME = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")

pytestmark = pytest.mark.asyncio


async def _render(name: str, arguments: dict | None = None) -> str:
    prompt = await mcp.get_prompt(name)
    messages = await prompt.render(arguments or {})
    return "\n".join(m.content.text for m in messages)


async def test_prompts_registered_and_render_client_name():
    prompts = await mcp.get_prompts()
    assert set(prompts) == PROMPT_NAMES

    text = await _render("why_did_cpl_jump", {"client": "Acme Dental"})
    assert text == (
        "Using Birdy, find out why CPL changed for Acme Dental. Read get_client notes first, compare this week "
        "to last with compare_periods, break it down by campaign and ad, and check get_agent_cards for related "
        "findings. End with the likely cause and what to do."
    )
    assert "{client}" not in await _render("prep_client_call", {"client": "Acme Dental"})


async def test_prompt_texts_are_verbatim():
    assert await _render("weekly_client_review") == (
        "Use Birdy's check_my_accounts for the last 7 days. For each client that needs attention, explain what "
        "changed and why using get_client, compare_periods and the agent's cards. Answer as Birdy's morning "
        "brief: a one-line headline, The win, The worry, The number, then numbered next steps."
    )
    assert await _render("prep_client_call", {"client": "Acme Dental"}) == (
        "Prep me for a call with Acme Dental using Birdy: last 30 days results vs target, best and worst ads, "
        "lead quality from get_unified_lead_stats, open agent cards, and recent notes. Format as a one-page brief "
        "with three talking points at the end."
    )
    assert await _render("agent_week") == (
        "Using Birdy, summarise what my media buying agent did this week: runs, applied and dismissed cards, "
        "outcomes, and anything waiting for my decision."
    )
    assert await _render("daily_check_in") == (
        "Use Birdy's check_my_accounts and tell me only what needs my attention today, most urgent first, as "
        "Birdy's morning brief. If nothing does, say so in one line."
    )
    assert "pick the one client that most needs help" in await _render("fix_worst_client")


async def test_prompts_only_name_registered_tools():
    tools = await mcp.get_tools()
    all_mentioned = set()
    for name in PROMPT_NAMES:
        args = {"client": "Acme Dental"} if name in CLIENT_PROMPTS else {}
        mentioned = set(TOOL_NAME.findall(await _render(name, args)))
        all_mentioned |= mentioned
        for tool in sorted(mentioned - PENDING):
            assert tool in tools, f"prompt {name} names '{tool}', which is not a registered MCP tool"
    # agent_week names no tool, so guard against the regex silently matching nothing across the set.
    assert {"get_client", "compare_periods", "get_agent_cards", "get_unified_lead_stats"} <= all_mentioned


async def test_prompts_listed_with_read_scope_token(mock_db, monkeypatch, subscription_state):
    """Prompts are not tools, so the scope middleware does not gate them: a read-only token lists and gets them."""
    import jwt as pyjwt
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from core.config import JWT_SECRET, JWT_ALGORITHM

    async def _token_valid(db, jti):
        return True

    monkeypatch.setattr("ai.mcp.oauth_provider.is_mcp_token_valid", _token_valid)
    claims = {"sub": "alice@example.com", "exp": int(time.time()) + 600, "type": "mcp",
              "scope": "birdy.read", "jti": uuid.uuid4().hex}
    token = pyjwt.encode(claims, JWT_SECRET, algorithm=JWT_ALGORITHM)
    app = mcp.http_app(path="/", stateless_http=True)

    def factory(headers=None, timeout=None, auth=None, **kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test",
                                 headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                                 timeout=timeout, **kwargs)

    transport = StreamableHttpTransport(url="http://mcp.test/", httpx_client_factory=factory)
    async with app.router.lifespan_context(app):
        async with Client(transport) as client:
            listed = {p.name for p in await client.list_prompts()}
            got = await client.get_prompt("why_did_cpl_jump", {"client": "Acme Dental"})
    assert listed == PROMPT_NAMES
    assert "Acme Dental" in got.messages[0].content.text
