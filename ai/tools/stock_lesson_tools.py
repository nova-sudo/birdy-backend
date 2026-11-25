"""
ai/tools/stock_lesson_tools.py
------------------------------
Teach Birdy's tools (page "stock_lessons", BirdyHub redesign spec section 4.1). The agent comes from
`current_lessons_agent`, which routers/chat.py sets per request after checking it is a stock agent
the user owns; each tool checks that again, and every lesson write is matched on that agent's id.
Tools return {ok, changed, ...} or {ok: false, error}, never raising for a person's mistake.

The add tool is `save_lesson`, not `add_lesson`: tool names are global in the registry, and the card
chat already has an `add_lesson` with a different shape (ai/tools/stock_card_tools.py).
"""

from contextvars import ContextVar

from ai.agents import store
from ai.agents.stock import lessons as stock_lessons
from ai.agents.store import MEMORY, TYPE_STOCK
from ai.prompts import stock_lessons_chat
from ai.tools.agent_tools import _match_client
from ai.team import proposals as team_proposals
from ai.tools.registry import registry

NOT_FOUND = "not_found"
SOURCE = "chat"
ALL_CLIENTS = "All clients"

# The stock agent Teach Birdy works on. routers/chat.py sets it for one request on page "stock_lessons"
# and resets it afterwards; None means "no agent".
current_lessons_agent: ContextVar[str | None] = ContextVar("current_lessons_agent", default=None)


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


async def load_lessons_agent(db, user_id: str, agent_id: str | None) -> dict | None:
    """The user's own stock agent with this id, or None."""
    if not agent_id:
        return None
    agent = await store.get_agent(db, user_id, agent_id)
    return agent if agent and agent.get("type") == TYPE_STOCK else None


async def _agent(db, user_id) -> dict | None:
    """The agent these tools work on: the user's stock agent (Teach Birdy, and Marketing Guru's own chat),
    or the user's Sales Coach (ai/team/memory.py) in Sales Coach's own chat."""
    from ai.team import memory as team_memory
    agent_id = current_lessons_agent.get()
    if team_memory.is_sales_coach_id(user_id, agent_id):
        return team_memory.sales_coach_view(user_id)
    return await load_lessons_agent(db, user_id, agent_id)


def _is_sales_coach(agent: dict) -> bool:
    return agent.get("key") == "sales_coach"


async def _groups(db, agent: dict) -> list[dict]:
    """The clients a lesson can be for: the agent's watched clients, or all of the user's if it watches none yet."""
    q = {"user_id": agent.get("user_id")}
    gids = (agent.get("scope") or {}).get("client_group_ids") or []
    if gids:
        q["id"] = {"$in": list(gids)}
    return await db["client_groups"].find(q, {"id": 1, "name": 1}).to_list(length=None)


def _view(m: dict, names: dict) -> dict:
    gid = m.get("client_group_id")
    return {"id": m["_id"], "text": m.get("text"), "client": names.get(gid, gid) if gid else ALL_CLIENTS,
            "source": m.get("source"), "uses": int(m.get("uses") or 0),
            **({"status": m["status"]} if m.get("status") else {})}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def list_lessons(db, user_id, mongo_client=None, *, only_client: str | None = None, **_):
    """`only_client` (set by the server in a one-client agent run, ai/team/pinning.py): that client's
    lessons and the agency-wide ones, never another client's."""
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    names = {g["id"]: g.get("name") or g["id"] for g in await _groups(db, agent)}
    q = {"agent_id": agent["_id"], "active": True}
    if only_client:
        q["$or"] = [{"client_group_id": None}, {"client_group_id": only_client}]
    rows = await db[MEMORY].find(q).sort("created_at", -1).to_list(length=None)
    return {"ok": True, "lessons": [_view(m, names) for m in rows]}


async def save_lesson(db, user_id, mongo_client=None, text=None, client_name=None, **_):
    """Keep what the person said as a lesson: for one client (matched by name) or for all clients."""
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    text = str(text or "").strip()
    if not text:
        return _fail("empty_lesson")
    gid, name = None, ALL_CLIENTS
    if client_name and str(client_name).strip():
        group, err = _match_client(await _groups(db, agent), client_name)
        if err:
            return err
        gid, name = group["id"], group.get("name") or group["id"]
    if _is_sales_coach(agent):
        # Sales Coach's lessons are kept as the person said them; its record is written on the first one.
        from ai.agents.learning import MEMORY_TEXT_MAX
        from ai.team import memory as team_memory
        await team_memory.ensure_sales_coach(db, user_id)
        lesson = await store.add_memory(db, agent["_id"], text=" ".join(text.split())[:MEMORY_TEXT_MAX], kind="fact",
                                        source=SOURCE, author=user_id, client_group_id=gid,
                                        status=stock_lessons.ACTIVE)
    else:
        lesson = await stock_lessons.lesson_from_text(db, agent, text, source=SOURCE, author=user_id,
                                                      client_group_id=gid)
    if lesson is None or "_id" not in lesson:
        return _fail("empty_lesson")
    team_proposals.record({"op": team_proposals.LESSON_SAVED, "agent_id": agent["_id"], "lesson_id": lesson["_id"],
                           "text": lesson.get("text")})
    return {"ok": True, "changed": "lesson", "lesson": {"id": lesson["_id"], "text": lesson.get("text"), "client": name}}


async def edit_lesson(db, user_id, mongo_client=None, lesson_id=None, text=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    if not str(text or "").strip():
        return _fail("empty_lesson")
    lesson = await stock_lessons.edit_lesson_text(db, agent["_id"], str(lesson_id or ""), text, author=user_id)
    if lesson is None:
        return _fail(NOT_FOUND)
    before = (lesson.get("previous_texts") or [{}])[-1]
    team_proposals.record({"op": team_proposals.LESSON_EDITED, "agent_id": agent["_id"], "lesson_id": lesson["_id"],
                           "before": {"text": before.get("text"), "source": before.get("source")},
                           "after_text": lesson.get("text")})
    return {"ok": True, "changed": "lesson_edited", "lesson": {"id": lesson["_id"], "text": lesson.get("text")}}


async def remove_lesson(db, user_id, mongo_client=None, lesson_id=None, **_):
    agent = await _agent(db, user_id)
    if agent is None:
        return _fail(NOT_FOUND)
    if not await stock_lessons.remove_lesson(db, agent["_id"], str(lesson_id or "")):
        return _fail(NOT_FOUND)
    if team_proposals.current_journal.get() is not None:
        gone = await db[MEMORY].find_one({"_id": str(lesson_id), "agent_id": agent["_id"]}, {"text": 1, "status": 1})
        team_proposals.record({"op": team_proposals.LESSON_REMOVED, "agent_id": agent["_id"],
                               "lesson_id": str(lesson_id), "text": (gone or {}).get("text"),
                               "status": (gone or {}).get("status")})
    return {"ok": True, "changed": "lesson_removed"}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

async def build_system_prompt(db, user_id) -> str:
    agent = await _agent(db, user_id)
    if agent is None:
        return stock_lessons_chat.system_prompt({})
    clients = sorted(g.get("name") or g["id"] for g in await _groups(db, agent))
    count = await db[MEMORY].count_documents({"agent_id": agent["_id"], "active": True})
    return stock_lessons_chat.system_prompt({"clients": clients, "lessons_count": count})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_LESSON_ID = {"type": "string", "description": "A lesson id from list_lessons."}

_TOOL_DEFS = {
    "list_lessons": (
        list_lessons,
        "List the agent's active lessons, newest first: id, text, which client (or All clients), where it came "
        "from and how often Birdy has used it.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "save_lesson": (
        save_lesson,
        "Save a new lesson, worded as one short sentence. Pass client_name for one client; leave it out for all "
        "clients.",
        {"type": "object", "properties": {"text": {"type": "string"}, "client_name": {"type": "string"}},
         "required": ["text"]},
    ),
    "edit_lesson": (
        edit_lesson,
        "Rewrite one lesson's text. Only after the person confirmed the new wording.",
        {"type": "object", "properties": {"lesson_id": _LESSON_ID, "text": {"type": "string"}},
         "required": ["lesson_id", "text"]},
    ),
    "remove_lesson": (
        remove_lesson,
        "Remove one lesson so Birdy stops using it. Only after the person confirmed.",
        {"type": "object", "properties": {"lesson_id": _LESSON_ID}, "required": ["lesson_id"]},
    ),
}

STOCK_LESSON_TOOL_NAMES = tuple(_TOOL_DEFS)


def register_stock_lesson_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
