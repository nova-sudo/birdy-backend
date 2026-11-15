"""
ai/agents/access.py
-------------------
Owner-scoped agent and card lookups shared by the HTTP routers and the MCP tools. Every lookup is keyed by
user_id, so another user's agent or card is simply not found. They return None; each caller picks its own error.
"""

from ai.agents import store as agent_store


async def find_agent(db, user_id: str, agent_id: str) -> dict | None:
    return await agent_store.get_agent(db, user_id, agent_id)


async def find_stock_agent(db, user_id: str, agent_id: str) -> dict | None:
    agent = await find_agent(db, user_id, agent_id)
    return agent if agent and agent.get("type") == agent_store.TYPE_STOCK else None


async def find_card(db, user_id: str, agent_id: str, card_id: str) -> dict | None:
    """The card, only if it belongs to this user's agent."""
    return await db[agent_store.CARDS].find_one({"_id": card_id, "agent_id": agent_id, "user_id": user_id})
