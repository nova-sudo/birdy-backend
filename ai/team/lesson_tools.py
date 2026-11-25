"""
ai/team/lesson_tools.py
-----------------------
Read-only lesson tools that follow the hierarchy (design section 8):

  list_agent_lessons      Birdy reads a main agent's lessons (Marketing Guru's, Sales Coach's)
  list_sub_agent_lessons  a main agent reads its own sub-agents' lessons

Nothing here writes. In a one-client run only that client's lessons and the agency-wide ones come
back, never another client's. Lessons are owner text, returned as data.
"""

import json

from ai.team import definitions as defs
from ai.team import memory as team_memory

LIST_AGENT_LESSONS = "list_agent_lessons"
LIST_SUB_AGENT_LESSONS = "list_sub_agent_lessons"
TOOL_NAMES = frozenset({LIST_AGENT_LESSONS, LIST_SUB_AGENT_LESSONS})
MAX_RETURNED = 100

_AGENT_SCHEMA = {"type": "function", "function": {
    "name": LIST_AGENT_LESSONS,
    "description": ("Read one main agent's lessons: what the owner taught Marketing Guru or Sales Coach. Read only; "
                    "use it to know how the owner wants that area judged before you answer or ask."),
    "parameters": {"type": "object", "properties": {
        "agent": {"type": "string", "enum": list(defs.MAIN_AGENTS)}}, "required": ["agent"]},
}}

_SUB_SCHEMA = {"type": "function", "function": {
    "name": LIST_SUB_AGENT_LESSONS,
    "description": "Read the lessons your sub-agents have been taught. Read only. Leave sub_agent_id out for all of them.",
    "parameters": {"type": "object", "properties": {
        "sub_agent_id": {"type": "string", "description": "One sub-agent's id, or leave out."}}, "required": []},
}}


def tool_schemas(frame) -> list[dict]:
    if frame.agent == defs.BIRDY:
        return [_AGENT_SCHEMA]
    if frame.agent in defs.MAIN_AGENTS:
        return [_SUB_SCHEMA]
    return []


def _in_scope(row: dict, gid: str | None) -> bool:
    return not gid or row.get("client_group_id") in (None, gid)


async def handle(name: str, args: dict, frame, *, db, user_id: str) -> str:
    gid = frame.client_group_id
    if name == LIST_AGENT_LESSONS and frame.agent == defs.BIRDY:
        key = str((args or {}).get("agent") or "")
        if key not in defs.MAIN_AGENTS:
            return json.dumps({"error": f"agent must be one of {list(defs.MAIN_AGENTS)}"})
        agent_id = await team_memory.main_agent_id(db, user_id, key)
        rows = await team_memory.active_lessons(db, agent_id, gid) if agent_id else []
        names = await team_memory.client_names(db, user_id, [r.get("client_group_id") for r in rows])
        return json.dumps({"agent": defs.NAMES[key], "lessons": [{
            "id": r["_id"], "text": r.get("text"),
            "client": names.get(r.get("client_group_id"), r.get("client_group_id")) if r.get("client_group_id")
            else "All clients"} for r in rows[-MAX_RETURNED:]]}, default=str)
    if name == LIST_SUB_AGENT_LESSONS and frame.agent in defs.MAIN_AGENTS:
        from ai.agents.sub_agents import list_parent_lessons
        rows = [r for r in await list_parent_lessons(db, user_id, frame.agent) if _in_scope(r, gid)]
        only = str((args or {}).get("sub_agent_id") or "")
        if only:
            rows = [r for r in rows if r.get("sub_agent_id") == only]
        names = await team_memory.client_names(db, user_id, [r.get("client_group_id") for r in rows])
        return json.dumps({"lessons": [{
            "id": r["id"], "sub_agent_id": r["sub_agent_id"], "sub_agent": r.get("sub_agent_name"),
            "text": r.get("text"),
            "client": names.get(r.get("client_group_id"), r.get("client_group_id")) if r.get("client_group_id")
            else "All clients"} for r in rows[-MAX_RETURNED:]]}, default=str)
    return json.dumps({"error": f"{name} isn't available to this agent."})
