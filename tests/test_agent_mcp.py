import pytest
from fastapi import HTTPException
from fastmcp.exceptions import ToolError

from ai.agents import store as agent_store
from ai.mcp import agent_mcp
from routers import agent_stock, agents as agents_router

ALICE, BOB = "alice@example.com", "bob@example.com"


async def _agent(db, user, name, type=agent_store.TYPE_CUSTOM):
    a = await agent_store.create_agent(db, user, type=type, name=name)
    return a["_id"] if "_id" in a else a["id"]


@pytest.mark.asyncio
async def test_default_is_stock_agent(mock_db, set_current_user):
    set_current_user(ALICE)
    await _agent(mock_db, ALICE, "Side", agent_store.TYPE_CUSTOM)
    stock = await _agent(mock_db, ALICE, None, agent_store.TYPE_STOCK)
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None))["_id"] == stock
    rows = await agent_mcp.list_agents.fn()
    assert {r["id"]: r["is_default"] for r in rows} == {stock: True, rows[0]["id"] if rows[0]["id"] != stock else rows[1]["id"]: False}


@pytest.mark.asyncio
async def test_no_agent_points_to_birdy(mock_db):
    with pytest.raises(ToolError, match="don't have a media buying agent yet"):
        await agent_mcp.resolve_agent(mock_db, ALICE, None)


@pytest.mark.asyncio
async def test_set_default_then_resolve_uses_custom(mock_db, set_current_user):
    set_current_user(ALICE)
    await _agent(mock_db, ALICE, None, agent_store.TYPE_STOCK)
    custom = await _agent(mock_db, ALICE, "Side Agent")
    await mock_db["users"].insert_one({"user_id": ALICE})
    out = await agent_mcp.set_default_agent.fn(agent="side agent")
    assert out["default"]["id"] == custom and out["default"]["kind"] == "custom"
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None))["_id"] == custom
    assert [r["id"] for r in await agent_mcp.list_agents.fn() if r["is_default"]] == [custom]
    # a custom default never blocks the stock tools: they fall back to the stock agent
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="stock"))["_id"] != custom


@pytest.mark.asyncio
async def test_ambiguous_agent_name_returns_candidates(mock_db, set_current_user):
    set_current_user(ALICE)
    await _agent(mock_db, ALICE, "Acme Dental")
    await _agent(mock_db, ALICE, "Acme Law")
    with pytest.raises(ToolError, match=r"Several agents match 'acme': Acme Dental, Acme Law\. Which one\?"):
        await agent_mcp.resolve_agent(mock_db, ALICE, "acme")
    with pytest.raises(ToolError, match="Several agents match"):
        await agent_mcp.set_default_agent.fn(agent="acme")
    assert await mock_db["users"].find_one({"user_id": ALICE, "mcp_default_agent_id": {"$exists": True}}) is None


@pytest.mark.asyncio
async def test_cannot_resolve_other_users_agent_by_id(mock_db, set_current_user):
    bobs = await _agent(mock_db, BOB, "Bob Agent")
    set_current_user(ALICE)
    with pytest.raises(ToolError, match="No agent matches"):
        await agent_mcp.resolve_agent(mock_db, ALICE, bobs)
    with pytest.raises(ToolError):
        await agent_mcp.resolve_agent(mock_db, ALICE, "Bob Agent")
    assert await agent_mcp.list_agents.fn() == []
    with pytest.raises(ToolError):
        await agent_mcp.set_default_agent.fn(agent=bobs)


@pytest.mark.asyncio
async def test_router_404s_unchanged(mock_db):
    bobs = await _agent(mock_db, BOB, "Bob Agent")
    with pytest.raises(HTTPException) as e:
        await agents_router._agent_or_404(mock_db, ALICE, bobs)
    assert (e.value.status_code, e.value.detail) == (404, "Agent not found")
    with pytest.raises(HTTPException) as e:
        await agent_stock._stock_agent(mock_db, ALICE, bobs)
    assert e.value.status_code == 404
    mine = await _agent(mock_db, ALICE, "Custom")
    with pytest.raises(HTTPException) as e:
        await agent_stock._stock_agent(mock_db, ALICE, mine)
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        await agent_stock._require_card(mock_db, mine, "nope", ALICE)
    assert (e.value.status_code, e.value.detail) == (404, "Card not found")


@pytest.mark.asyncio
async def test_custom_default_does_not_break_stock_tools(mock_db, set_current_user):
    """I4: with a custom agent as the default, stock tools fall back to the stock agent."""
    from ai.mcp import stock_agent_mcp
    set_current_user(ALICE)
    stock = await _agent(mock_db, ALICE, None, agent_store.TYPE_STOCK)
    custom = await _agent(mock_db, ALICE, "Side agent")
    await mock_db["users"].insert_one({"user_id": ALICE})
    await agent_mcp.set_default_agent.fn(agent="Side agent")
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="stock"))["_id"] == stock
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="custom"))["_id"] == custom
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None))["_id"] == custom
    assert (await stock_agent_mcp.get_agent_cards.fn())["count"] == 0


@pytest.mark.asyncio
async def test_stock_default_custom_tools_use_the_only_custom_or_ask(mock_db, set_current_user):
    set_current_user(ALICE)
    await _agent(mock_db, ALICE, None, agent_store.TYPE_STOCK)
    with pytest.raises(ToolError, match="don't have a custom agent yet"):
        await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="custom")
    one = await _agent(mock_db, ALICE, "Side")
    assert (await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="custom"))["_id"] == one
    await _agent(mock_db, ALICE, "Other")
    with pytest.raises(ToolError, match="Which custom agent.*Side.*Other|Which custom agent.*Other.*Side"):
        await agent_mcp.resolve_agent(mock_db, ALICE, None, kind="custom")
