"""
ai/agents/sub_agents/propose_tool.py
------------------------------------
`propose_sub_agent` as a chat tool for a main agent. NOT registered anywhere: the agent-hierarchy
branch adds it to Media Buyer's and Sales Coach's tool lists and passes `parent` from the agent
that is running (never from the model's arguments, so one main agent can't propose for another).

    schema = tool_schema("media_buyer")          # the tool's JSON schema, with this parent's tools as the enum
    result = await execute(db, user_id, "media_buyer", **model_args)
    # {"ok": True, "sub_agent": <record JSON>, "approval": {...}} or {"ok": False, "error": ..., "code": ...}

The record JSON is the same one GET /api/agents/sub returns, so the chat's approval card renders it
directly and its buttons call POST /api/agents/sub/{id}/approve or /reject.
"""

from ai.agents.sub_agents import core
from ai.agents.sub_agents.tools import PARENT_LABELS, parent_tool_names

TOOL_NAME = "propose_sub_agent"


def tool_schema(parent: str) -> dict:
    tools = sorted(parent_tool_names(parent))
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                f"Propose a new sub-agent under {PARENT_LABELS[parent]} for a job the owner wants done again "
                "and again. It is only a proposal: it shows as a card the owner approves or rejects, and "
                "nothing runs before they approve. Give it only the tools the job needs."),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": f"Short name, at most {core.NAME_MAX} characters"},
                    "purpose": {"type": "string", "description": "One line: what it does"},
                    "instructions": {"type": "string", "description": "How it should do the job, written out"},
                    "tools": {"type": "array", "items": {"type": "string", "enum": tools}, "minItems": 1},
                    "clients": {
                        "description": 'The clients it covers: "all", or a list of client group ids',
                        "anyOf": [{"type": "string", "enum": [core.ALL_CLIENTS]},
                                  {"type": "array", "items": {"type": "string"}, "minItems": 1}]},
                    "schedule": {
                        "description": "When it runs by itself, or null for only when asked",
                        "anyOf": [{"type": "null"}, {
                            "type": "object",
                            "properties": {
                                "time": {"type": "string", "description": "HH:MM, 24h"},
                                "timezone": {"type": "string", "description": "IANA name, e.g. Europe/London"},
                                "days": {"type": "array", "items": {"type": "string", "enum": list(core.DAYS)}},
                            },
                            "required": ["time", "timezone"]}]},
                },
                "required": ["name", "purpose", "instructions", "tools", "clients"],
            },
        },
    }


async def execute(db, user_id: str, parent: str, /, *, name=None, purpose=None, instructions=None, tools=None,
                  clients=core.ALL_CLIENTS, schedule=None, session_id: str | None = None, **_ignored) -> dict:
    try:
        sub = await core.propose_sub_agent(
            db, user_id, parent=parent, name=name, purpose=purpose, instructions=instructions,
            tools=tools if tools is not None else [], clients=clients, schedule=schedule,
            source={"session_id": session_id} if session_id else None)
    except core.SubAgentError as e:
        return {"ok": False, "error": e.body["detail"], "code": e.code}
    record = await core.to_api(db, user_id, sub)
    return {"ok": True, "sub_agent": record,
            "approval": {"approve": f"/api/agents/sub/{sub['id']}/approve",
                         "reject": f"/api/agents/sub/{sub['id']}/reject"},
            "note": "Proposed. Tell the owner it waits for their approval; it does nothing until then."}
