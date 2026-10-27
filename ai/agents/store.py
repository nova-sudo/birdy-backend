"""
ai/agents/store.py
------------------
Mongo-backed persistence for BirdyHub agents (Phase 1: the Media Buying Agent).

Collections:
  agents               one per (user_id, type) — unique index
  agent_rules          the rules each agent runs (seeded from defaults)
  agent_rule_versions  snapshot of a rule before each params/override change
  agent_memory         notes the agent has learned or been taught
  agent_practice       practice cases used to build confidence
  agent_proposals      rule-change proposals raised from repeated reasons
  agent_jobs           scheduled / queued runs
  agent_ad_days        daily Meta ad rows per (user, client group, ad, day) for the agent

Same per-request Motor pattern as ai/suggestions/store.py: raw dicts, no ODM.
"""

import copy
import logging
import uuid
from datetime import datetime

from core.database import DB_NAME
from ai.suggestions.store import SUGGESTIONS
from ai.agents.defaults import (
    DEFAULT_GUARDRAILS,
    DEFAULT_MODE,
    DEFAULT_PARAMS,
    DEFAULT_PERMISSIONS,
    RULE_KINDS,
)

logger = logging.getLogger(__name__)

AGENTS = "agents"
RULES = "agent_rules"
RULE_VERSIONS = "agent_rule_versions"
MEMORY = "agent_memory"
PRACTICE = "agent_practice"
PROPOSALS = "agent_proposals"
JOBS = "agent_jobs"
AD_DAYS = "agent_ad_days"


AGENT_PATCH_KEYS = {"scope", "permissions", "guardrails", "client_caps", "status", "training"}


class AgentExists(Exception):
    """The user already has an agent of this type."""


def _now() -> datetime:
    return datetime.utcnow()


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def create_agent(db, user_id: str, *, type: str = "media_buying", tz: str = "Europe/London") -> dict:
    # The unique (user_id, type) index enforces this in production; the explicit
    # check keeps the error clean and works where the index isn't enforced.
    if await db[AGENTS].find_one({"user_id": user_id, "type": type}):
        raise AgentExists(f"user already has a '{type}' agent")

    now = _now()
    agent = {
        "_id": _new_id(),
        "user_id": user_id,
        "type": type,
        "status": "draft",
        "scope": {"client_group_ids": [], "schedule": {"time": "07:00", "tz": tz}},
        "permissions": copy.deepcopy(DEFAULT_PERMISSIONS),
        "guardrails": copy.deepcopy(DEFAULT_GUARDRAILS),
        "client_caps": {},
        "training": {"topics": {"targets": False, "pause": False, "revive": False, "flags": False}},
        "confidence": {"score": 0, "parts": {}, "at_activation": None},
        "last_run_at": None,
        "created_at": now,
    }
    try:
        await db[AGENTS].insert_one(agent)
    except Exception as e:
        if "duplicate key" in str(e).lower():
            raise AgentExists(f"user already has a '{type}' agent") from e
        raise

    rules = [
        {
            "_id": _new_id(),
            "agent_id": agent["_id"],
            "kind": kind,
            "version": 1,
            "enabled": True,
            "mode": DEFAULT_MODE[kind],
            "params": copy.deepcopy(DEFAULT_PARAMS[kind]),
            "client_overrides": {},
            "stats": {"streak": 0, "last10": [], "approved": 0, "denied": 0, "undone_auto": [], "offer_after": 10},
            "auto_since": None,
        }
        for kind in RULE_KINDS
    ]
    await db[RULES].insert_many(rules)
    return agent


class AgentActive(Exception):
    """The agent is switched on; only a draft can be deleted."""


async def delete_draft_agent(db, user_id: str, agent_id: str) -> bool:
    """Delete a draft agent and everything it owns, so setup can start over.
    False when the user has no such agent; AgentActive when it is switched on."""
    agent = await db[AGENTS].find_one({"_id": agent_id, "user_id": user_id})
    if agent is None:
        return False
    if agent.get("status") == "active":
        raise AgentActive(agent_id)
    res = await db[AGENTS].delete_one({"_id": agent_id, "user_id": user_id, "status": {"$ne": "active"}})
    if res.deleted_count == 0:
        raise AgentActive(agent_id)  # switched on between the read and the delete
    for coll in (RULES, RULE_VERSIONS, MEMORY, PRACTICE, PROPOSALS, JOBS):
        await db[coll].delete_many({"agent_id": agent_id})
    return True


async def get_agent(db, user_id: str, agent_id: str) -> dict | None:
    return await db[AGENTS].find_one({"_id": agent_id, "user_id": user_id})


async def list_agents(db, user_id: str) -> list[dict]:
    return await db[AGENTS].find({"user_id": user_id}).sort("created_at", 1).to_list(length=None)


async def update_agent(db, user_id: str, agent_id: str, patch: dict) -> dict:
    bad = set(patch) - AGENT_PATCH_KEYS
    if bad:
        raise ValueError(f"cannot update agent field(s): {sorted(bad)}")
    if patch:
        await db[AGENTS].update_one({"_id": agent_id, "user_id": user_id}, {"$set": patch})
    return await get_agent(db, user_id, agent_id)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

async def list_rules(db, agent_id: str) -> list[dict]:
    return await db[RULES].find({"agent_id": agent_id}).to_list(length=None)


async def update_rule(db, agent_id: str, rule_id: str, *, params=None, client_overrides=None,
                      mode=None, enabled=None) -> dict:
    rule = await db[RULES].find_one({"_id": rule_id, "agent_id": agent_id})
    if rule is None:
        raise KeyError(f"rule {rule_id} not found")

    updates: dict = {}
    if mode is not None:
        updates["mode"] = mode
    if enabled is not None:
        updates["enabled"] = enabled

    if params is not None or client_overrides is not None:
        # Snapshot the document as it stood before this change.
        snapshot = copy.deepcopy(rule)
        snapshot["rule_id"] = snapshot.pop("_id")
        snapshot["_id"] = _new_id()
        snapshot["snapshotted_at"] = _now()
        await db[RULE_VERSIONS].insert_one(snapshot)
        if params is not None:
            updates["params"] = params
        if client_overrides is not None:
            updates["client_overrides"] = client_overrides
        updates["version"] = rule["version"] + 1

    if updates:
        await db[RULES].update_one({"_id": rule_id, "agent_id": agent_id}, {"$set": updates})
    if mode is not None and mode != rule.get("mode"):
        await sync_open_rule_mode(db, rule_id, mode)
    return await db[RULES].find_one({"_id": rule_id, "agent_id": agent_id})


async def sync_open_rule_mode(db, rule_id: str, mode: str) -> None:
    """Open items carry their rule's mode (apply's flag_only check, Slack
    buttons); keep it current so a mode change takes effect immediately."""
    await db[SUGGESTIONS].update_many(
        {"rule_id": rule_id, "status": "open"}, {"$set": {"rule_mode": mode}})


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

async def add_memory(db, agent_id: str, *, text: str, kind: str, source: str,
                     source_ref=None, filter=None) -> dict:
    doc = {
        "_id": _new_id(),
        "agent_id": agent_id,
        "text": text,
        "kind": kind,
        "source": source,
        "source_ref": source_ref,
        "filter": filter,
        "active": True,
        "created_at": _now(),
    }
    await db[MEMORY].insert_one(doc)
    return doc


async def list_memory(db, agent_id: str, active_only: bool = True) -> list[dict]:
    q: dict = {"agent_id": agent_id}
    if active_only:
        q["active"] = True
    return await db[MEMORY].find(q).sort("created_at", 1).to_list(length=None)


async def deactivate_memory(db, agent_id: str, memory_id: str) -> bool:
    res = await db[MEMORY].update_one(
        {"_id": memory_id, "agent_id": agent_id, "active": True}, {"$set": {"active": False}}
    )
    return res.modified_count > 0


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

async def create_agent_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db[AGENTS].create_index([("user_id", 1), ("type", 1)], unique=True)
    await db[RULES].create_index("agent_id")
    await db[MEMORY].create_index([("agent_id", 1), ("active", 1)])
    await db[PRACTICE].create_index("agent_id")
    await db[PROPOSALS].create_index([("agent_id", 1), ("status", 1)])
    await db[JOBS].create_index([("agent_id", 1), ("status", 1)])
    # Imported here: ai.agents.history imports AGENTS from this module.
    from ai.agents.history import HISTORY
    await db[HISTORY].create_index([("status", 1), ("claimed_at", 1)])
    await db[HISTORY].create_index("user_id")
    await db[AD_DAYS].create_index(
        [("user_id", 1), ("client_group_id", 1), ("ad_id", 1), ("date_start", 1)], unique=True)
    await db[AD_DAYS].create_index([("client_group_id", 1), ("ad_id", 1), ("date_start", 1)])
    await db[JOBS].create_index("created_at", expireAfterSeconds=7 * 24 * 3600)
    await db["meta_ad_state"].create_index([("client_group_id", 1), ("ad_id", 1)], unique=True)
    await db["meta_adset_state"].create_index([("client_group_id", 1), ("adset_id", 1)], unique=True)
