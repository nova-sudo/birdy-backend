"""Birdy cards (MCP Apps): tools point at ui:// resources the host renders (ai/mcp/cards.py)."""

import pytest
from fastmcp import Client

from ai.mcp import cards, mcp
from core.config import API_PUBLIC_URL, FRONTEND_URL


@pytest.mark.asyncio
async def test_check_my_accounts_points_at_the_accounts_card():
    async with Client(mcp) as c:
        tool = {t.name: t for t in await c.list_tools()}["check_my_accounts"]
    assert tool.meta["ui"]["resourceUri"] == "ui://birdy/accounts.html"
    assert tool.meta["ui/resourceUri"] == "ui://birdy/accounts.html"  # older hosts


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(cards.CARDS))
async def test_every_card_is_a_complete_mcp_app_resource(name):
    async with Client(mcp) as c:
        listed = {str(r.uri): r for r in await c.list_resources()}
        [content] = await c.read_resource(cards.uri(name))
    res = listed[cards.uri(name)]
    assert res.mimeType == content.mimeType == "text/html;profile=mcp-app"
    assert res.meta["ui"]["csp"]["resourceDomains"] == ["https://cdn.jsdelivr.net", API_PUBLIC_URL]
    html = content.text
    assert html.startswith("<!doctype html>") and "__" not in html
    assert cards.SDK_URL in html and "@2.0.4/" in cards.SDK_URL  # pinned SDK
    assert f'frontend: "{FRONTEND_URL}"' in html and "window.birdyRender" in html


@pytest.mark.asyncio
async def test_card_resources_are_not_tools_and_cost_nothing():
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
    assert not any(n.startswith("birdy_card_") for n in names)
