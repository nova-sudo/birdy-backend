"""
ai/team/memory.py
-----------------
The main agents' lessons (design section 8: "Lessons are private to each agent. A main agent can read
its sub-agents' lessons; Birdy can read the main agents'.").

Both live in `agent_memory`, keyed by the agent's id, never mixed:
  Marketing Guru   the user's stock (Media Buying) agent: its lessons ARE Marketing Guru's (one store).
  Sales Coach   a small per-user record in `team_agents`, id "sales_coach:<user_id>". It is stable
                (derived from the user), so lessons can be listed before it exists; it is written
                lazily, on the first lesson, never by a migration.

`main_agent_id(db, user_id, key)` gives the id lessons are stored under (None for a Marketing Guru with
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


# ---------------------------------------------------------------------------
# The app's lessons API (routers/team.py): one main agent's lessons
# ---------------------------------------------------------------------------

class LessonError(Exception):
    """A refusal with an HTTP status and a body {detail, code} (the sub-agents router's shape)."""

    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.body = status, code, {"detail": detail, "code": code}


SOURCE_APP = "app"


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def check_agent(key) -> str:
    if key not in defs.MAIN_AGENTS:
        raise LessonError(400, "bad_agent", f"agent must be one of {list(defs.MAIN_AGENTS)}")
    return key


async def _agent_id_or_raise(db, user_id: str, key: str) -> str:
    agent_id = await main_agent_id(db, user_id, check_agent(key))
    if not agent_id:
        raise LessonError(404, "agent_not_found", f"{defs.NAMES[key]} is not set up yet")
    return agent_id


def _clean_text(text) -> str:
    from ai.agents.learning import MEMORY_TEXT_MAX
    if not isinstance(text, str) or not text.strip():
        raise LessonError(400, "bad_lesson", "A lesson needs some text")
    text = " ".join(text.split())
    if len(text) > MEMORY_TEXT_MAX:
        raise LessonError(400, "bad_lesson", f"A lesson must be at most {MEMORY_TEXT_MAX} characters")
    return text


async def _check_client(db, user_id: str, gid):
    if gid is None:
        return None
    if not isinstance(gid, str) or not gid.strip():
        raise LessonError(400, "unknown_client", "client_group_id must be one of your client ids")
    from ai.agents import validation
    err = await validation.owned_groups_error(db, user_id, [gid])
    if err:
        raise LessonError(400, "unknown_client", err)
    return gid


def _lesson_api(row: dict, names: dict) -> dict:
    gid = row.get("client_group_id")
    return {"id": row["_id"], "text": row.get("text"), "client_group_id": gid,
            "client_name": names.get(gid) if gid else None, "source": row.get("source"),
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("edited_at") or row.get("created_at"))}


async def _one_api(db, user_id: str, row: dict) -> dict:
    return _lesson_api(row, await client_names(db, user_id, [row.get("client_group_id")]))


async def list_lessons_api(db, user_id: str, key: str, client_group_id: str | None = None) -> dict:
    """{"lessons": [...]}, newest first. With a client: that client's lessons and the agency-wide ones
    (what the agent itself is given for that client). Marketing Guru with no stock agent yet: none."""
    check_agent(key)
    agent_id = await main_agent_id(db, user_id, key)
    if client_group_id:
        await _check_client(db, user_id, client_group_id)
    rows = list(reversed(await active_lessons(db, agent_id, client_group_id))) if agent_id else []
    names = await client_names(db, user_id, [r.get("client_group_id") for r in rows])
    return {"lessons": [_lesson_api(r, names) for r in rows]}


async def add_lesson_api(db, user_id: str, key: str, text, client_group_id=None) -> dict:
    """The owner's words, kept as written (no AI call). Sales Coach's record is written on its first lesson."""
    from ai.agents.store import add_memory
    text = _clean_text(text)
    gid = await _check_client(db, user_id, client_group_id)
    agent_id = await _agent_id_or_raise(db, user_id, key)
    if key == defs.SALES_COACH:
        await ensure_sales_coach(db, user_id)
    row = await add_memory(db, agent_id, text=text, kind="fact", source=SOURCE_APP, author=user_id,
                           client_group_id=gid, status="active")
    return await _one_api(db, user_id, row)


async def _active_row(db, agent_id: str, lesson_id: str) -> dict:
    from ai.agents.store import MEMORY
    row = await db[MEMORY].find_one({"_id": lesson_id, "agent_id": agent_id, "active": True})
    if row is None:
        raise LessonError(404, "not_found", "Lesson not found")
    return row


async def edit_lesson_api(db, user_id: str, key: str, lesson_id: str, text) -> dict:
    """Rewrite a lesson; the old words are kept on the row (stock lessons.edit_lesson_text)."""
    from ai.agents.stock.lessons import edit_lesson_text
    text = _clean_text(text)
    agent_id = await _agent_id_or_raise(db, user_id, key)
    await _active_row(db, agent_id, lesson_id)
    row = await edit_lesson_text(db, agent_id, lesson_id, text, author=user_id)
    if row is None:
        raise LessonError(404, "not_found", "Lesson not found")
    return await _one_api(db, user_id, row)


async def delete_lesson_api(db, user_id: str, key: str, lesson_id: str) -> dict:
    """The same removal as Teach Birdy and DELETE /memory/{id}: the lesson stops being used."""
    from ai.agents.stock.lessons import remove_lesson
    agent_id = await _agent_id_or_raise(db, user_id, key)
    if not await remove_lesson(db, agent_id, lesson_id):
        raise LessonError(404, "not_found", "Lesson not found")
    return {"deleted": True, "id": lesson_id}
