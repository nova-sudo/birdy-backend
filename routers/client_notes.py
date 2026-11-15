"""
routers/client_notes.py
-----------------------
Hand-written notes on a client — the composer at the bottom of the Client
Detail history book.

Distinct from `ai_conversation_log` (what was said to Birdy) and from the
dashboard activity feed (what Birdy or a cron did). Those are both generated;
this is the one place a person records something the data cannot show — a call
with the client, a change of plan, why a month looked odd.

Stored per client group and scoped by user, so a note is only ever visible to
the account that wrote it.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user
from services import client_notes as notes_service
from services.client_notes import MAX_NOTE_LENGTH, MAX_NOTES_RETURNED  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter()

_COLLECTION = notes_service.COLLECTION


class CreateNoteRequest(BaseModel):
    body: str
    author: Optional[str] = None      # display name; falls back to the user id


def _http(e: notes_service.NoteError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


@router.get("/api/client-groups/{group_id}/notes")
async def list_notes(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """Notes on one client, newest first."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            return {"notes": await notes_service.list_notes(db, current_user, group_id)}
        except notes_service.NoteError as e:
            raise _http(e)
        except Exception as e:
            logger.error(f"Error listing notes for {group_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load notes")


@router.post("/api/client-groups/{group_id}/notes")
async def create_note(
    group_id: str,
    request: CreateNoteRequest,
    current_user: str = Depends(get_current_user),
):
    """Append a note. Returns it, so the client can render without refetching."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            note = await notes_service.create_note(
                db, current_user, group_id, request.body, author=request.author)
            logger.info(f"Note {note['id']} added to {group_id} by {current_user}")
            return note
        except notes_service.NoteError as e:
            raise _http(e)
        except Exception as e:
            logger.error(f"Error creating note on {group_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save note")


@router.delete("/api/client-groups/{group_id}/notes/{note_id}")
async def delete_note(
    group_id: str,
    note_id: str,
    current_user: str = Depends(get_current_user),
):
    """Remove one note. Scoped by user, so one account cannot delete another's."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await notes_service.delete_note(db, current_user, group_id, note_id)
            return {"success": True, "id": note_id}
        except notes_service.NoteError as e:
            raise _http(e)
        except Exception as e:
            logger.error(f"Error deleting note {note_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete note")


async def create_note_indexes(mongo_client) -> None:
    """The list query is always (group, user) newest-first."""
    db = mongo_client[DB_NAME]
    await db[_COLLECTION].create_index(
        [("group_id", 1), ("user_id", 1), ("created_at", -1)],
        name="idx_client_notes_group_user_created", background=True,
    )
