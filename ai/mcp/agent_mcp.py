"""
ai/mcp/agent_mcp.py
--------------------
Agent lookup, the per-user default agent, and the list_agents / set_default_agent tools. Every
media-buying-agent tool resolves its agent through `resolve_agent`, so a name is never guessed:
an ambiguous one returns the candidates and changes nothing.
"""

from typing import Literal

from fastmcp.exceptions import ToolError

from ai.agents import access as agent_access
from ai.agents import store as agent_store
from ai.mcp.access import READ, WRITE
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.config import FRONTEND_URL
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

DEFAULT_FIELD = "mcp_default_agent_id"


def _kind(agent: dict) -> str:
    return "stock" if agent.get("type") == agent_store.TYPE_STOCK else "custom"


def _label(agent: dict) -> str:
    return agent.get("name") or agent["_id"]


def _ambiguous(text: str, matches: list[dict]) -> ToolError:
    return ToolError(f"Several agents match '{text}': {', '.join(_label(a) for a in matches)}. Which one?")


async def _find_by_text(db, user_id: str, text: str) -> dict:
    agent = await agent_access.find_agent(db, user_id, text)
    if agent:
        return agent
    agents = await agent_store.list_agents(db, user_id)
    needle = text.strip().lower()
    for pool in ([a for a in agents if (a.get("name") or "").strip().lower() == needle],
                 [a for a in agents if needle and needle in (a.get("name") or "").lower()]):
        if len(pool) == 1:
            return pool[0]
        if pool:
            raise _ambiguous(text, pool)
    raise ToolError(f"No agent matches '{text}'. Call list_agents to see the available agents.")


async def resolve_agent(db, user_id: str, agent: str | None,
                        kind: Literal["stock", "custom", "any"] = "any") -> dict:
    """The agent doc for `agent` (id or name), else the user's default, else their stock agent. Raises ToolError."""
    if agent:
        found = await _find_by_text(db, user_id, agent)
    else:
        found = None
        user = await db["users"].find_one({"user_id": user_id}, {DEFAULT_FIELD: 1})
        default_id = (user or {}).get(DEFAULT_FIELD)
        if default_id:
            found = await agent_access.find_agent(db, user_id, default_id)
        if found is None:
            found = await db[agent_store.AGENTS].find_one({"user_id": user_id, "type": agent_store.TYPE_STOCK})
        if found is None:
            raise ToolError(f"You don't have a media buying agent yet. Set one up in Birdy: {FRONTEND_URL}/agents")
    actual = _kind(found)
    if kind != "any" and actual != kind:
        if kind == "stock":
            raise ToolError(f"'{_label(found)}' is a custom agent, not the Birdy Media Buying Agent. "
                            "Use the custom agent tools for it, or pick the media buying agent.")
        raise ToolError(f"'{_label(found)}' is the Birdy Media Buying Agent, not a custom agent. "
                        "Use the media buying agent tools for it.")
    return found


@mcp.tool(**READ)
async def list_agents() -> list[dict]:
    """List the user's agents with id, name, kind (stock or custom), status and which one is the default.

    Use this first to see which agents exist. Tools that take an `agent` argument accept an id or a name;
    leave it out to use the default agent.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    user = await db["users"].find_one({"user_id": user_id}, {DEFAULT_FIELD: 1})
    default_id = (user or {}).get(DEFAULT_FIELD)
    agents = await agent_store.list_agents(db, user_id)
    if default_id not in [a["_id"] for a in agents]:
        stock = next((a for a in agents if a.get("type") == agent_store.TYPE_STOCK), None)
        default_id = stock["_id"] if stock else None
    return [{"id": a["_id"], "name": a.get("name"), "kind": _kind(a), "status": a.get("status"),
             "is_default": a["_id"] == default_id} for a in agents]


@mcp.tool(**WRITE)
async def set_default_agent(agent: str) -> dict:
    """Make one agent the default for every agent tool, across all of the user's connections.

    Args:
        agent: The agent's id or name (see list_agents). An ambiguous name returns the candidates and changes nothing.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    found = await resolve_agent(db, user_id, agent)
    await db["users"].update_one({"user_id": user_id}, {"$set": {DEFAULT_FIELD: found["_id"]}})
    return {"default": {"id": found["_id"], "name": found.get("name"), "kind": _kind(found)}}
