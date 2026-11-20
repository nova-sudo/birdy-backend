"""
ai/agents/sub_agents/core.py
----------------------------
Sub-agents: the third level under a main agent (Media Buyer, Sales Coach). Design:
briefs/2026-11-20-agent-hierarchy-design.md, sections 5, 6 and 8.

Two kinds are listed as sub-agents:
  specialist  stored here, in `sub_agents`. Written instructions, a tool list picked by its main
              agent (never more than the main agent has), the clients it covers, an optional
              schedule and private lessons. Proposed by its main agent; nothing runs until the
              owner approves it.
  custom      today's four-rule ad agents, stored in `agents` (type "custom"). They count as
              Media Buyer's sub-agents, read through here with parent "media_buyer". They are not
              migrated or written by this module: their own setup, activate and turn-off routes
              still own them, and every write here refuses them (409 managed_elsewhere).

Why a sibling collection and not more rows in `agents`: every path that reads `agents` treats a
non-stock active row as a four-rule agent (jobs/agent_jobs.run_due_agents runs it, claims and its
nightly migration claim its clients, history pulls their Meta ads, the suggestions pass skips its
clients, GET /api/agents and the MCP list_agents show it). A specialist in `agents` would be run as a
rule set. In `sub_agents` none of those paths can see it.

Lifecycle (status):
  proposed --approve--> active --pause--> paused --resume--> active
  proposed --reject---> (deleted)        any --delete--> (deleted, with its lessons and schedule)

Every function is owner-scoped: another user's sub-agent is simply not found (404).
Lessons reuse `agent_memory` keyed by the sub-agent's id (kind "lesson"): private to the sub-agent,
readable by its parent through `list_parent_lessons`.
"""

import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from ai.agents import store as agent_store
from ai.agents import validation
from ai.agents.custom_ops import OpError
from ai.agents.sub_agents import tools as sub_tools
from ai.agents.sub_agents.tools import PARENTS, parent_tool_names

SUB_AGENTS = "sub_agents"

KIND_SPECIALIST = "specialist"
KIND_CUSTOM = "custom"          # a four-rule agent from `agents`, read only here
KINDS = (KIND_SPECIALIST, KIND_CUSTOM)

PROPOSED, ACTIVE, PAUSED = "proposed", "active", "paused"
STATUSES = (PROPOSED, ACTIVE, PAUSED)
ALL_CLIENTS = "all"

NAME_MAX = agent_store.NAME_MAX            # 60, the same as a custom agent's name
PURPOSE_MAX = 140
INSTRUCTIONS_MAX = 4000
TOOLS_MAX = 30
LESSON_MAX = 280                           # ai.agents.learning.MEMORY_TEXT_MAX
LESSON_KIND = "lesson"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

PATCH_FIELDS = ("name", "purpose", "instructions", "tools", "clients", "schedule")


class SubAgentError(OpError):
    """A refusal with an HTTP status and a body {detail, code} (the agents router's OpError shape)."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, {"detail": detail, "code": code})
        self.code = code


def _not_found() -> SubAgentError:
    return SubAgentError(404, "not_found", "Sub-agent not found")


def _now() -> datetime:
    """Naive UTC, at Mongo's millisecond precision, so a returned doc matches what was stored."""
    now = datetime.utcnow()
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


# ---------------------------------------------------------------------------
# Validation (each returns the clean value or raises SubAgentError 400)
# ---------------------------------------------------------------------------

def _bad(code: str, detail: str) -> SubAgentError:
    return SubAgentError(400, code, detail)


def check_parent(parent) -> str:
    if parent not in PARENTS:
        raise _bad("bad_parent", f"parent must be one of {list(PARENTS)}")
    return parent


def clean_name(name) -> str:
    if not isinstance(name, str) or not " ".join(name.split()):
        raise _bad("bad_name", "Give the sub-agent a name")
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise _bad("bad_name", f"Name must be at most {NAME_MAX} characters")
    return name


def name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def clean_purpose(purpose) -> str:
    if not isinstance(purpose, str) or not purpose.strip():
        raise _bad("bad_purpose", "Say in one line what the sub-agent does")
    purpose = " ".join(purpose.split())  # one line
    if len(purpose) > PURPOSE_MAX:
        raise _bad("bad_purpose", f"Purpose must be at most {PURPOSE_MAX} characters")
    return purpose


def clean_instructions(instructions) -> str:
    if not isinstance(instructions, str) or not instructions.strip():
        raise _bad("bad_instructions", "Write the sub-agent's instructions")
    instructions = instructions.strip()
    if len(instructions) > INSTRUCTIONS_MAX:
        raise _bad("bad_instructions", f"Instructions must be at most {INSTRUCTIONS_MAX} characters")
    return instructions


def clean_tools(parent: str, tools) -> list[str]:
    """At least one tool, every one known to Birdy and held by the parent. Order kept, repeats dropped."""
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise _bad("bad_tools", "tools must be a list of tool names")
    names = list(dict.fromkeys(t.strip() for t in tools if t.strip()))
    if not names:
        raise _bad("bad_tools", "Pick at least one tool")
    if len(names) > TOOLS_MAX:
        raise _bad("bad_tools", f"At most {TOOLS_MAX} tools")
    unknown = [t for t in names if t not in sub_tools.known_tool_names()]
    if unknown:
        raise _bad("unknown_tool", f"Unknown tool(s): {unknown}")
    allowed = parent_tool_names(parent)
    outside = [t for t in names if t not in allowed]
    if outside:
        writes = [t for t in outside if sub_tools.is_write_tool(t)]
        label = sub_tools.PARENT_LABELS[parent]
        if writes:
            raise _bad("tool_not_allowed",
                       f"{label} can't make the change(s) {writes}, so its sub-agent can't either")
        raise _bad("tool_not_allowed", f"Outside {label}'s area: {outside}")
    return names


async def clean_clients(db, user_id: str, clients):
    """"all", or a non-empty list of the user's client group ids (order kept, repeats dropped)."""
    if clients == ALL_CLIENTS:
        return ALL_CLIENTS
    if not isinstance(clients, list) or not all(isinstance(c, str) and c.strip() for c in clients):
        raise _bad("bad_clients", 'clients must be "all" or a list of client ids')
    ids = list(dict.fromkeys(c.strip() for c in clients))
    if not ids:
        raise _bad("bad_clients", 'Pick at least one client, or "all"')
    err = await validation.owned_groups_error(db, user_id, ids)
    if err:
        raise _bad("unknown_client", err)
    return ids


def clean_schedule(schedule):
    """None (no schedule), or {time "HH:MM" 24h, timezone (IANA), days (subset of mon..sun, default
    every day)}. `tz` is accepted for `timezone`, as the custom agents' schedule calls it."""
    if schedule is None:
        return None
    if not isinstance(schedule, dict):
        raise _bad("bad_schedule", "schedule must be an object or null")
    schedule = dict(schedule)
    if "tz" in schedule and "timezone" not in schedule:
        schedule["timezone"] = schedule.pop("tz")
    unknown = set(schedule) - {"time", "timezone", "days"}
    if unknown:
        raise _bad("bad_schedule", f"Unknown schedule key(s): {sorted(unknown)}")
    time = schedule.get("time")
    if not (isinstance(time, str) and _TIME_RE.match(time)):
        raise _bad("bad_schedule", "schedule.time must be HH:MM (24h)")
    tz = schedule.get("timezone")
    try:
        if not isinstance(tz, str) or not tz:
            raise ValueError
        ZoneInfo(tz)
    except Exception:
        raise _bad("bad_schedule", "schedule.timezone is not a valid timezone")
    days = schedule.get("days", list(DAYS))
    if (not isinstance(days, list) or not days
            or not all(isinstance(d, str) and d.lower() in DAYS for d in days)):
        raise _bad("bad_schedule", f"schedule.days must be a non-empty list of {list(DAYS)}")
    picked = {d.lower() for d in days}
    return {"time": time, "timezone": tz, "days": [d for d in DAYS if d in picked]}


async def _check_name_free(db, user_id: str, parent: str, name: str, *, exclude_id: str | None = None):
    key = name_key(name)
    q = {"user_id": user_id, "parent": parent, "name_key": key}
    if exclude_id:
        q["_id"] = {"$ne": exclude_id}
    clash = await db[SUB_AGENTS].find_one(q, {"status": 1})
    if clash is None and parent == sub_tools.MEDIA_BUYER:
        # Today's custom agents are Media Buyer's sub-agents too.
        async for a in db[agent_store.AGENTS].find(
                {"user_id": user_id, "type": agent_store.TYPE_CUSTOM}, {"name": 1}):
            if name_key(a.get("name") or "") == key:
                clash = {"status": "custom"}
                break
    if clash is not None:
        waiting = " and is waiting for approval" if clash.get("status") == PROPOSED else ""
        raise SubAgentError(409, "duplicate_name",
                            f"{sub_tools.PARENT_LABELS[parent]} already has a sub-agent called {name!r}{waiting}")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _normalize(doc: dict) -> dict:
    """A stored specialist as callers see it: `id` instead of `_id`, no name_key."""
    out = {k: v for k, v in doc.items() if k not in ("_id", "name_key")}
    out["id"] = doc["_id"]
    return out


def _custom_status(agent: dict) -> str:
    if agent.get("status") == "active":
        return ACTIVE
    return PAUSED if agent.get("turned_off_at") else PROPOSED  # a draft still in setup is not on yet


def _from_custom(agent: dict) -> dict:
    """A four-rule custom agent shown as one of Media Buyer's sub-agents (backfilled on read)."""
    scope = agent.get("scope") or {}
    sched = scope.get("schedule") or {}
    return {
        "id": agent["_id"],
        "user_id": agent.get("user_id"),
        "parent": sub_tools.MEDIA_BUYER,
        "kind": KIND_CUSTOM,
        "name": agent.get("name") or agent_store.DEFAULT_CUSTOM_NAME,
        "purpose": agent.get("description") or "",
        "instructions": agent.get("description") or "",
        "tools": [],
        "clients": list(scope.get("client_group_ids") or []),
        "schedule": ({"time": sched.get("time"), "timezone": sched.get("tz"), "days": list(DAYS)}
                     if sched.get("time") else None),
        "status": _custom_status(agent),
        "created_by": "owner",
        "created_at": agent.get("created_at"),
        "updated_at": None,
        "last_run_at": agent.get("last_run_at"),
    }


async def get_sub_agent(db, user_id: str, sub_agent_id: str) -> dict | None:
    """One of the user's sub-agents (a specialist, or a custom agent shown as one), or None.

    Returns the plain dict shape:
      {id, user_id, parent, kind, name, purpose, instructions, tools: [tool names],
       clients: "all" | [client_group_id], schedule: None | {time, timezone, days},
       status, created_by, created_at, updated_at, approved_at?, paused_at?, last_run_at}
    """
    doc = await db[SUB_AGENTS].find_one({"_id": sub_agent_id, "user_id": user_id})
    if doc is not None:
        return _normalize(doc)
    agent = await db[agent_store.AGENTS].find_one(
        {"_id": sub_agent_id, "user_id": user_id, "type": agent_store.TYPE_CUSTOM})
    return _from_custom(agent) if agent else None


async def list_sub_agents(db, user_id: str, parent: str | None = None, *, status: str | None = None,
                          kind: str | None = None) -> list[dict]:
    """The user's sub-agents, oldest first, in get_sub_agent's shape.
    parent: "media_buyer" | "sales_coach" | None (both). status: "proposed" | "active" | "paused" |
    None (all). kind: "specialist" | "custom" | None (both). To run children, call with
    status="active", kind="specialist"."""
    if parent is not None:
        check_parent(parent)
    if status is not None and status not in STATUSES:
        raise _bad("bad_status", f"status must be one of {list(STATUSES)}")
    if kind is not None and kind not in KINDS:
        raise _bad("bad_kind", f"kind must be one of {list(KINDS)}")
    out: list[dict] = []
    if kind in (None, KIND_SPECIALIST):
        q: dict = {"user_id": user_id}
        if parent is not None:
            q["parent"] = parent
        if status is not None:
            q["status"] = status
        out += [_normalize(d) for d in await db[SUB_AGENTS].find(q).to_list(length=None)]
    if kind in (None, KIND_CUSTOM) and parent in (None, sub_tools.MEDIA_BUYER):
        for a in await db[agent_store.AGENTS].find(
                {"user_id": user_id, "type": agent_store.TYPE_CUSTOM}).to_list(length=None):
            row = _from_custom(a)
            if status is None or row["status"] == status:
                out.append(row)
    out.sort(key=lambda r: (r.get("created_at") or datetime.min, r["kind"] != KIND_CUSTOM))
    return out


async def _specialist_or_raise(db, user_id: str, sub_agent_id: str) -> dict:
    """The stored specialist doc. A custom agent's id is a 409: its own routes manage it."""
    doc = await db[SUB_AGENTS].find_one({"_id": sub_agent_id, "user_id": user_id})
    if doc is not None:
        return doc
    if await db[agent_store.AGENTS].find_one(
            {"_id": sub_agent_id, "user_id": user_id, "type": agent_store.TYPE_CUSTOM}, {"_id": 1}):
        raise SubAgentError(409, "managed_elsewhere",
                            "This is a custom rules agent: change it from its own page")
    raise _not_found()


# ---------------------------------------------------------------------------
# Proposing and the owner's decisions
# ---------------------------------------------------------------------------

async def propose_sub_agent(db, user_id: str, *, parent: str, name: str, purpose: str,
                            instructions: str, tools: list[str], clients="all",
                            schedule: dict | None = None, created_by: str | None = None,
                            source: dict | None = None) -> dict:
    """A main agent proposes a sub-agent. It is stored as "proposed" and does nothing until the
    owner approves it. Returns the new sub-agent (get_sub_agent's shape).

    parent        "media_buyer" | "sales_coach": the main agent it will belong to.
    created_by    the main agent proposing it; defaults to `parent`, and must equal it (a main agent
                  only creates its own sub-agents).
    tools         tool names, a subset of parent_tool_names(parent).
    clients       "all" or a list of the user's client_group_ids.
    schedule      None, or {time "HH:MM", timezone, days?}.
    source        optional {session_id, ...} for the approval card to point back at its chat.
    Raises SubAgentError (400 validation, 409 duplicate_name)."""
    parent = check_parent(parent)
    created_by = created_by or parent
    if created_by != parent:
        raise _bad("bad_creator", "A main agent can only propose its own sub-agents")
    name = clean_name(name)
    doc_fields = {
        "purpose": clean_purpose(purpose),
        "instructions": clean_instructions(instructions),
        "tools": clean_tools(parent, tools),
        "clients": await clean_clients(db, user_id, clients),
        "schedule": clean_schedule(schedule),
    }
    await _check_name_free(db, user_id, parent, name)
    now = _now()
    doc = {
        "_id": str(uuid.uuid4()),
        "user_id": user_id,
        "parent": parent,
        "kind": KIND_SPECIALIST,
        "name": name,
        "name_key": name_key(name),
        **doc_fields,
        "status": PROPOSED,
        "created_by": created_by,
        "source": source if isinstance(source, dict) else None,
        "created_at": now,
        "updated_at": now,
        "approved_at": None,
        "paused_at": None,
        "last_run_at": None,
    }
    try:
        await db[SUB_AGENTS].insert_one(doc)
    except Exception as e:
        if "duplicate key" in str(e).lower():  # the unique (user, parent, name_key) index won a race
            raise SubAgentError(409, "duplicate_name",
                                f"{sub_tools.PARENT_LABELS[parent]} already has a sub-agent called {name!r}") from e
        raise
    return _normalize(doc)


async def _move(db, user_id: str, sub_agent_id: str, *, from_status: str, to_status: str,
                stamp: dict, wrong: str) -> dict:
    doc = await _specialist_or_raise(db, user_id, sub_agent_id)
    if doc.get("status") == to_status:
        return _normalize(doc)  # already there: the same press twice is not an error
    if doc.get("status") != from_status:
        raise SubAgentError(409, "wrong_status", wrong)
    now = _now()
    res = await db[SUB_AGENTS].update_one(
        {"_id": sub_agent_id, "user_id": user_id, "status": from_status},
        {"$set": {"status": to_status, "updated_at": now, **{k: now for k in stamp}}})
    if res.modified_count == 0:
        raise SubAgentError(409, "wrong_status", wrong)  # changed between the read and the write
    return _normalize(await db[SUB_AGENTS].find_one({"_id": sub_agent_id, "user_id": user_id}))


async def approve_sub_agent(db, user_id: str, sub_agent_id: str) -> dict:
    """The owner says yes: proposed -> active. Approving an active one returns it unchanged."""
    return await _move(db, user_id, sub_agent_id, from_status=PROPOSED, to_status=ACTIVE,
                       stamp={"approved_at": 1}, wrong="Only a proposed sub-agent can be approved")


async def reject_sub_agent(db, user_id: str, sub_agent_id: str) -> dict:
    """The owner says no: the proposal is deleted (nothing was ever active). Returns {deleted: id}."""
    doc = await _specialist_or_raise(db, user_id, sub_agent_id)
    if doc.get("status") != PROPOSED:
        raise SubAgentError(409, "wrong_status", "Only a proposed sub-agent can be rejected; delete it instead")
    res = await db[SUB_AGENTS].delete_one({"_id": sub_agent_id, "user_id": user_id, "status": PROPOSED})
    if res.deleted_count == 0:
        raise SubAgentError(409, "wrong_status", "Only a proposed sub-agent can be rejected; delete it instead")
    await db[agent_store.MEMORY].delete_many({"agent_id": sub_agent_id})
    return {"deleted": sub_agent_id}


async def pause_sub_agent(db, user_id: str, sub_agent_id: str) -> dict:
    """active -> paused. Everything is kept (instructions, tools, schedule, lessons); it just
    doesn't run or get asked."""
    return await _move(db, user_id, sub_agent_id, from_status=ACTIVE, to_status=PAUSED,
                       stamp={"paused_at": 1}, wrong="Only an active sub-agent can be paused")


async def resume_sub_agent(db, user_id: str, sub_agent_id: str) -> dict:
    """paused -> active."""
    return await _move(db, user_id, sub_agent_id, from_status=PAUSED, to_status=ACTIVE,
                       stamp={}, wrong="Only a paused sub-agent can be resumed")


async def update_sub_agent(db, user_id: str, sub_agent_id: str, patch: dict) -> dict:
    """The owner edits a sub-agent in any status. `patch` may hold name, purpose, instructions,
    tools, clients, schedule (null clears the schedule). The status does not change: an edit to a
    proposal is still a proposal. Parent and kind never change."""
    unknown = set(patch) - set(PATCH_FIELDS)
    if unknown:
        raise _bad("bad_field", f"Can't change: {sorted(unknown)}")
    doc = await _specialist_or_raise(db, user_id, sub_agent_id)
    parent = doc["parent"]
    updates: dict = {}
    if "name" in patch:
        name = clean_name(patch["name"])
        if name_key(name) != doc.get("name_key"):
            await _check_name_free(db, user_id, parent, name, exclude_id=sub_agent_id)
        updates.update(name=name, name_key=name_key(name))
    if "purpose" in patch:
        updates["purpose"] = clean_purpose(patch["purpose"])
    if "instructions" in patch:
        updates["instructions"] = clean_instructions(patch["instructions"])
    if "tools" in patch:
        updates["tools"] = clean_tools(parent, patch["tools"])
    if "clients" in patch:
        updates["clients"] = await clean_clients(db, user_id, patch["clients"])
    if "schedule" in patch:
        updates["schedule"] = clean_schedule(patch["schedule"])
    if updates:
        updates["updated_at"] = _now()
        try:
            await db[SUB_AGENTS].update_one({"_id": sub_agent_id, "user_id": user_id}, {"$set": updates})
        except Exception as e:
            if "duplicate key" in str(e).lower():
                raise SubAgentError(409, "duplicate_name", "That name is taken") from e
            raise
    fresh = await db[SUB_AGENTS].find_one({"_id": sub_agent_id, "user_id": user_id})
    if fresh is None:
        raise _not_found()  # deleted meanwhile
    return _normalize(fresh)


async def delete_sub_agent(db, user_id: str, sub_agent_id: str) -> dict:
    """Delete a sub-agent in any status, with its lessons and its schedule (which lives on the
    record) and its run records. The UI asks for confirmation first. Returns {deleted: id}."""
    await _specialist_or_raise(db, user_id, sub_agent_id)
    res = await db[SUB_AGENTS].delete_one({"_id": sub_agent_id, "user_id": user_id})
    if res.deleted_count == 0:
        raise _not_found()
    await db[agent_store.MEMORY].delete_many({"agent_id": sub_agent_id})
    from ai.agents.sub_agents.schedule import RUNS
    await db[RUNS].delete_many({"sub_agent_id": sub_agent_id})
    return {"deleted": sub_agent_id}


# ---------------------------------------------------------------------------
# Lessons (agent_memory, keyed by the sub-agent's id)
# ---------------------------------------------------------------------------

async def add_sub_agent_lesson(db, user_id: str, sub_agent_id: str, text: str, *, source: str = "chat",
                               client_group_id: str | None = None, author: str | None = None) -> dict:
    """Store a lesson for one specialist. Private to it; its parent reads it via list_parent_lessons."""
    doc = await _specialist_or_raise(db, user_id, sub_agent_id)
    if not isinstance(text, str) or not text.strip():
        raise _bad("bad_lesson", "A lesson needs some text")
    text = text.strip()
    if len(text) > LESSON_MAX:
        raise _bad("bad_lesson", f"A lesson must be at most {LESSON_MAX} characters")
    if client_group_id is not None:
        clients = doc.get("clients")
        if clients != ALL_CLIENTS and client_group_id not in (clients or []):
            raise _bad("unknown_client", "That client is not one this sub-agent covers")
        err = await validation.owned_groups_error(db, user_id, [client_group_id])
        if err:
            raise _bad("unknown_client", err)
    row = await agent_store.add_memory(db, sub_agent_id, text=text, kind=LESSON_KIND, source=source,
                                       author=author, client_group_id=client_group_id)
    return _lesson(row)


def _lesson(row: dict) -> dict:
    return {"id": row["_id"], "sub_agent_id": row["agent_id"], "text": row.get("text"),
            "kind": row.get("kind"), "source": row.get("source"),
            "client_group_id": row.get("client_group_id"), "created_at": row.get("created_at")}


async def list_sub_agent_lessons(db, user_id: str, sub_agent_id: str) -> list[dict]:
    """A sub-agent's active lessons, oldest first (custom agents: their notes and exceptions)."""
    if await get_sub_agent(db, user_id, sub_agent_id) is None:
        raise _not_found()
    return [_lesson(r) for r in await agent_store.list_memory(db, sub_agent_id)]


async def list_parent_lessons(db, user_id: str, parent: str) -> list[dict]:
    """Every active lesson of the parent's sub-agents, each tagged with its sub-agent's id and name.
    For the main agent's own prompt: a main agent may read its sub-agents' lessons, nobody else's."""
    subs = await list_sub_agents(db, user_id, parent)
    names = {s["id"]: s["name"] for s in subs}
    if not names:
        return []
    rows = await db[agent_store.MEMORY].find(
        {"agent_id": {"$in": list(names)}, "active": True}).sort("created_at", 1).to_list(length=None)
    return [{**_lesson(r), "sub_agent_name": names.get(r["agent_id"])} for r in rows]


async def delete_sub_agent_lesson(db, user_id: str, sub_agent_id: str, lesson_id: str) -> dict:
    await _specialist_or_raise(db, user_id, sub_agent_id)
    if not await agent_store.deactivate_memory(db, sub_agent_id, lesson_id):
        raise SubAgentError(404, "not_found", "Lesson not found")
    return {"deleted": lesson_id}


# ---------------------------------------------------------------------------
# The HTTP / card shape
# ---------------------------------------------------------------------------

async def _client_names(db, user_id: str, ids) -> dict:
    ids = sorted(set(ids))
    if not ids:
        return {}
    rows = await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": ids}}, {"id": 1, "name": 1, "_id": 0}).to_list(length=None)
    return {r["id"]: r.get("name") for r in rows}


def _to_api(sub: dict, names: dict) -> dict:
    clients = sub.get("clients")
    return {
        "id": sub["id"],
        "parent": sub["parent"],
        "kind": sub["kind"],
        "name": sub["name"],
        "purpose": sub.get("purpose") or "",
        "instructions": sub.get("instructions") or "",
        "tools": [{"name": t, "label": sub_tools.tool_label(t)} for t in sub.get("tools") or []],
        "clients": ALL_CLIENTS if clients == ALL_CLIENTS
        else [{"id": c, "name": names.get(c)} for c in clients or []],
        "schedule": sub.get("schedule"),
        "status": sub["status"],
        "created_by": sub.get("created_by"),
        "created_at": sub.get("created_at"),
        "updated_at": sub.get("updated_at"),
        "approved_at": sub.get("approved_at"),
        "paused_at": sub.get("paused_at"),
        "last_run_at": sub.get("last_run_at"),
    }


async def to_api_many(db, user_id: str, subs: list[dict]) -> list[dict]:
    """Sub-agents (get_sub_agent's shape) as the frontend and the approval card get them: tools with
    labels, clients with names (a client deleted since shows name null)."""
    ids = [c for s in subs if isinstance(s.get("clients"), list) for c in s["clients"]]
    names = await _client_names(db, user_id, ids)
    return [_to_api(s, names) for s in subs]


async def to_api(db, user_id: str, sub: dict) -> dict:
    return (await to_api_many(db, user_id, [sub]))[0]


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

async def create_sub_agent_indexes(mongo_client) -> None:
    from core.database import DB_NAME
    from ai.agents.sub_agents.schedule import RUNS
    db = mongo_client[DB_NAME]
    await db[SUB_AGENTS].create_index([("user_id", 1), ("parent", 1), ("status", 1)])
    await db[SUB_AGENTS].create_index([("user_id", 1), ("parent", 1), ("name_key", 1)], unique=True,
                                      name="sub_agent_name_per_parent")
    await db[SUB_AGENTS].create_index("status")  # the scheduled-run scan (schedule.due_sub_agents)
    await db[RUNS].create_index([("sub_agent_id", 1), ("local_date", 1)], unique=True, name="sub_agent_daily_run")
