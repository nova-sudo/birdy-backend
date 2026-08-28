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
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

_COLLECTION = "client_notes"

MAX_NOTE_LENGTH = 2000
MAX_NOTES_RETURNED = 200


class CreateNoteRequest(BaseModel):
    body: str
    author: Optional[str] = None      # display name; falls back to the user id


def _clean_body(raw: str) -> str:
    body = (raw or "").strip()
    if not body:
        raise HTTPException(status_code=400, detail="A note cannot be empty")
    if len(body) > MAX_NOTE_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"A note must be {MAX_NOTE_LENGTH} characters or fewer",
        )
    return body


async def _assert_owns_group(db, group_id: str, user_id: str) -> None:
    """A note may only be attached to a client group the caller owns."""
    exists = await db["client_groups"].find_one(
        {"id": group_id, "user_id": user_id}, {"_id": 1}
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Client group not found")


@router.get("/api/client-groups/{group_id}/notes")
async def list_notes(
    group_id: str,
    current_user: str = Depends(get_current_user),
):
    """Notes on one client, newest first."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await _assert_owns_group(db, group_id, current_user)

            rows = await db[_COLLECTION].find(
                {"group_id": group_id, "user_id": current_user},
                {"_id": 0},
            ).sort("created_at", -1).to_list(length=MAX_NOTES_RETURNED)

            return {"notes": [
                {**r, "created_at": r["created_at"].isoformat()
                 if hasattr(r.get("created_at"), "isoformat") else r.get("created_at")}
                for r in rows
            ]}
        except HTTPException:
            raise
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
    body = _clean_body(request.body)

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await _assert_owns_group(db, group_id, current_user)

            note = {
                "id": uuid.uuid4().hex[:12],
                "group_id": group_id,
                "user_id": current_user,
                "author": (request.author or "").strip() or current_user,
                "body": body,
                "created_at": datetime.utcnow(),
            }
            await db[_COLLECTION].insert_one(dict(note))
            logger.info(f"Note {note['id']} added to {group_id} by {current_user}")

            return {**note, "created_at": note["created_at"].isoformat()}
        except HTTPException:
            raise
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
            result = await db[_COLLECTION].delete_one(
                {"id": note_id, "group_id": group_id, "user_id": current_user}
            )
            if result.deleted_count == 0:
                raise HTTPException(status_code=404, detail="Note not found")
            return {"success": True, "id": note_id}
        except HTTPException:
            raise
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
