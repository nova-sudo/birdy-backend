"""
ai/team/memory.py
-----------------
The main agents' lessons (design section 8: "Lessons are private to each agent. A main agent can read
its sub-agents' lessons; Birdy can read the main agents'.").

Both live in `agent_memory`, keyed by the agent's id, never mixed:
  Media Buyer   the user's stock (Media Buying) agent: its lessons ARE Media Buyer's (one store).
  Sales Coach   a small per-user record in `team_agents`, id "sales_coach:<user_id>". It is stable
                (derived from the user), so lessons can be listed before it exists; it is written
                lazily, on the first lesson, never by a migration.

`main_agent_id(db, user_id, key)` gives the id lessons are stored under (None for a Media Buyer with
no stock agent yet). Lessons reach an agent's own prompt only as delimited data
(definitions._data_block), and only this client's and the agency-wide ones.
"""

from datetime import datetime

from ai.team import definitions as defs

TEAM_AGENTS = "team_agents"


def sales_coach_id(user_id: str) -> str:
    return f"{defs.SALES_COACH}:{user_id}"


def is_sales_coach_id(user_id: str, agent_id) -> bool:
    return bool(agent_id) and agent_id == sales_coach_id(user_id)


async def ensure_sales_coach(db, user_id: str) -> dict:
    """The user's Sales Coach record, written on first use (an upsert, so two first lessons at once
    make one record)."""
    _id = sales_coach_id(user_id)
    await db[TEAM_AGENTS].update_one(
        {"_id": _id},
        {"$setOnInsert": {"user_id": user_id, "key": defs.SALES_COACH, "created_at": datetime.utcnow()}},
        upsert=True)
    return await db[TEAM_AGENTS].find_one({"_id": _id})


def sales_coach_view(user_id: str) -> dict:
    """The Sales Coach as the lesson tools see it, whether or not its record is written yet."""
    return {"_id": sales_coach_id(user_id), "user_id": user_id, "key": defs.SALES_COACH}


async def main_agent_id(db, user_id: str, key: str) -> str | None:
    if key == defs.MEDIA_BUYER:
        return await defs.stock_agent_id(db, user_id)
    if key == defs.SALES_COACH:
        return sales_coach_id(user_id)
    return None


def _client_filter(client_group_id: str | None) -> dict:
    if not client_group_id:
        return {}
    return {"$or": [{"client_group_id": None}, {"client_group_id": client_group_id}]}


async def active_lessons(db, agent_id: str, client_group_id: str | None = None) -> list[dict]:
    """An agent's active lessons, oldest first; for one client, its own and the agency-wide ones."""
    from ai.agents.store import MEMORY
    q = {"agent_id": agent_id, "active": True, "status": {"$ne": "overruled"}, **_client_filter(client_group_id)}
    return await db[MEMORY].find(q).sort("created_at", 1).to_list(length=None)


async def client_names(db, user_id: str, ids) -> dict:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = await db["client_groups"].find({"user_id": user_id, "id": {"$in": ids}},
                                          {"_id": 0, "id": 1, "name": 1}).to_list(length=None)
    return {r["id"]: r.get("name") for r in rows}
