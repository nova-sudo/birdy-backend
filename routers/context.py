"""
routers/context.py
------------------
The agency's Context page (services/agency_context.py): four cards, a client's own card and
Documents & notes. Same conventions as routers/sub_agents.py: owner-scoped (another user's line,
note or client is a 404, never a 403), refusals as {detail, code} JSON with their status.

  GET    /api/context?client_group_id=      the cards, the client's card, the notes
  POST   /api/context/items                 {card, text, client_group_id?} -> item
  PATCH  /api/context/items/{id}            {text} -> item
  DELETE /api/context/items/{id}            -> {deleted: true, id}
  POST   /api/context/notes                 {text, client_group_id?} -> note
  PATCH  /api/context/notes/{id}            {text} -> note
  DELETE /api/context/notes/{id}            -> {deleted: true, id}

A note with client_group_id is a client note, written through services/client_notes.py, so it is
the same note GET/POST/DELETE /api/client-groups/{id}/notes and the MCP note tools see.

Body fields are typed `object` and checked in the service, so a wrong type answers 400 with a
code, like every other refusal here, instead of FastAPI's 422.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import agency_context as ctx

router = APIRouter(prefix="/api/context", tags=["context"])


def _err(e: ctx.ContextError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.body)


async def _run(fn, *args):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await fn(db, *args)
        except ctx.ContextError as e:
            return _err(e)


class ItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    card: Optional[object] = None
    text: Optional[object] = None
    client_group_id: Optional[object] = None


class TextPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[object] = None


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Optional[object] = None
    client_group_id: Optional[object] = None
    author: Optional[object] = None          # display name; falls back to the user id


@router.get("")
async def get_context(client_group_id: Optional[str] = None, current_user: str = Depends(get_current_user)):
    return await _run(ctx.get_context, current_user, client_group_id or None)


@router.post("/items")
async def add_item(body: ItemCreate, current_user: str = Depends(get_current_user)):
    return await _run(ctx.add_item, current_user, body.card, body.text, body.client_group_id)


@router.patch("/items/{item_id}")
async def update_item(item_id: str, body: TextPatch, current_user: str = Depends(get_current_user)):
    return await _run(ctx.update_item, current_user, item_id, body.text)


@router.delete("/items/{item_id}")
async def delete_item(item_id: str, current_user: str = Depends(get_current_user)):
    return await _run(ctx.delete_item, current_user, item_id)


@router.post("/notes")
async def add_note(body: NoteCreate, current_user: str = Depends(get_current_user)):
    return await _run(ctx.add_note, current_user, body.text, body.client_group_id, body.author)


@router.patch("/notes/{note_id}")
async def update_note(note_id: str, body: TextPatch, current_user: str = Depends(get_current_user)):
    return await _run(ctx.update_note, current_user, note_id, body.text)


@router.delete("/notes/{note_id}")
async def delete_note(note_id: str, current_user: str = Depends(get_current_user)):
    return await _run(ctx.delete_note, current_user, note_id)
