"""
ai/agents/claims.py
-------------------
One acting agent per client. `agent_client_claims` holds one row per (user, client group)
naming the agent allowed to change that client's ads. A unique index on
(user_id, client_group_id) is what makes two concurrent claims resolve to one holder.

An agent that watches a client it does not hold can still report on it, but every change it
proposes is a Flag (see runner.run_agent and guardrails.check_guardrails).
A client nobody holds is not blocked: draft agents and legacy rows hold nothing.
"""

import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

CLAIMS = "agent_client_claims"


async def ensure_indexes(db) -> None:
    await db[CLAIMS].create_index(
        [("user_id", 1), ("client_group_id", 1)], unique=True, name="user_client_claim")


async def holder(db, user_id: str, gid: str) -> str | None:
    row = await db[CLAIMS].find_one({"user_id": user_id, "client_group_id": gid})
    return row["agent_id"] if row else None


async def acting_agent(db, user_id: str, gid: str) -> dict | None:
    agent_id = await holder(db, user_id, gid)
    if agent_id is None:
        return None
    return await db["agents"].find_one({"_id": agent_id, "user_id": user_id})


async def _conflicts(db, user_id: str, agent_id: str, gids: list[str]) -> list[dict]:
    rows = await db[CLAIMS].find(
        {"user_id": user_id, "client_group_id": {"$in": gids}, "agent_id": {"$ne": agent_id}}
    ).to_list(length=None)
    held = {r["client_group_id"]: r["agent_id"] for r in rows}
    names = {}
    if held:
        for a in await db["agents"].find({"_id": {"$in": list(set(held.values()))}}).to_list(length=None):
            names[a["_id"]] = a.get("name") or ""
    return [{"client_group_id": g, "holder_id": held[g], "holder_name": names.get(held[g], "")}
            for g in gids if g in held]


async def claim_clients(db, user_id: str, agent_id: str, gids, *, move: bool = False) -> list[dict]:
    """Claim `gids` for the agent. Returns conflicts `[{client_group_id, holder_id, holder_name}]`;
    when any exist and `move` is false nothing is claimed. `move=True` takes them over."""
    gids = list(dict.fromkeys(gids or []))
    if not gids:
        return []
    conflicts = await _conflicts(db, user_id, agent_id, gids)
    if conflicts and not move:
        return conflicts
    taken: list[str] = []  # rows this call newly created, to undo if a race is lost
    for gid in gids:
        key = {"user_id": user_id, "client_group_id": gid}
        if move:
            await db[CLAIMS].update_one(
                key, {"$set": {"agent_id": agent_id, "claimed_at": datetime.utcnow()},
                      "$setOnInsert": {"_id": str(uuid.uuid4())}}, upsert=True)
            continue
        try:
            res = await db[CLAIMS].update_one(
                key, {"$setOnInsert": {"_id": str(uuid.uuid4()), "agent_id": agent_id,
                                       "claimed_at": datetime.utcnow()}}, upsert=True)
        except Exception as e:
            if "duplicate key" not in str(e).lower():
                raise
            res = None  # another request inserted it first; the check below decides
        if res is not None and res.upserted_id is not None:
            taken.append(gid)
    if move:
        return []
    lost = await _conflicts(db, user_id, agent_id, gids)
    if lost:
        if taken:
            await db[CLAIMS].delete_many(
                {"user_id": user_id, "client_group_id": {"$in": taken}, "agent_id": agent_id})
        return lost
    return []


async def release_clients(db, user_id: str, agent_id: str, gids=None) -> None:
    """Drop the agent's claims (all of them, or just `gids`). Never touches another agent's."""
    q: dict = {"user_id": user_id, "agent_id": agent_id}
    if gids is not None:
        q["client_group_id"] = {"$in": list(gids)}
    await db[CLAIMS].delete_many(q)


async def migrate_claims(db) -> None:
    """Give every existing active agent a claim on its clients. Oldest agent first, so when
    two already watch the same client the first by created_at keeps it and the other stays a
    watcher (its changes are flagged). Safe to run on every start: never overwrites a claim."""
    agents = await db["agents"].find({"status": "active"}).sort("created_at", 1).to_list(length=None)
    for a in agents:
        gids = (a.get("scope") or {}).get("client_group_ids") or []
        for gid in dict.fromkeys(gids):
            conflicts = await claim_clients(db, a["user_id"], a["_id"], [gid])
            if conflicts:
                logger.info("claims migration: %s keeps %s, %s only watches it",
                            conflicts[0]["holder_id"], gid, a["_id"])
