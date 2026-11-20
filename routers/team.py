"""
routers/team.py
---------------
The main agents' lessons for the app (ai/team/memory.py). Same conventions as routers/sub_agents.py:
owner-scoped (another user's lesson is a 404), refusals as {detail, code} JSON with their status.

  GET    /api/team/{agent}/lessons?client_group_id=   -> {"lessons": [lesson]}
  POST   /api/team/{agent}/lessons {text, client_group_id?} -> lesson
  PATCH  /api/team/{agent}/lessons/{id} {text}          -> lesson
  DELETE /api/team/{agent}/lessons/{id}                 -> {deleted: true, id}

  agent   "media_buyer" | "sales_coach" (anything else: 400 bad_agent)
  lesson  {id, text, client_group_id, client_name, source, created_at, updated_at}

Media Buyer's lessons are the stock (Media Buying) agent's own: the same rows Teach Birdy, the card
chat and the card writer use. Sales Coach's are under its per-user id.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai.team import memory as team_memory
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client

router = APIRouter(prefix="/api/team", tags=["team"])


async def _run(fn, *args):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await fn(db, *args)
        except team_memory.LessonError as e:
            return JSONResponse(status_code=e.status, content=e.body)


class LessonBody(BaseModel):
    """Typed `object` and checked in ai/team/memory.py, so a wrong type is a 400 with a code."""
    model_config = ConfigDict(extra="forbid")
    text: Optional[object] = None
    client_group_id: Optional[object] = None


class LessonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[object] = None


@router.get("/{agent}/lessons")
async def list_lessons(agent: str, client_group_id: Optional[str] = None,
                       current_user: str = Depends(get_current_user)):
    return await _run(team_memory.list_lessons_api, current_user, agent, client_group_id or None)


@router.post("/{agent}/lessons")
async def add_lesson(agent: str, body: LessonBody, current_user: str = Depends(get_current_user)):
    return await _run(team_memory.add_lesson_api, current_user, agent, body.text, body.client_group_id)


@router.patch("/{agent}/lessons/{lesson_id}")
async def edit_lesson(agent: str, lesson_id: str, body: LessonPatch, current_user: str = Depends(get_current_user)):
    return await _run(team_memory.edit_lesson_api, current_user, agent, lesson_id, body.text)


@router.delete("/{agent}/lessons/{lesson_id}")
async def delete_lesson(agent: str, lesson_id: str, current_user: str = Depends(get_current_user)):
    return await _run(team_memory.delete_lesson_api, current_user, agent, lesson_id)
