"""
services/client_notes.py
------------------------
Hand-written notes on a client. The logic behind routers/client_notes.py (the
Client Detail composer) and the MCP note tools, so both write the same shape.

Stored per client group and scoped by user, so a note is only ever visible to
the account that wrote it. Failures raise NoteError(status, detail); the router
turns them into the same HTTP responses it always returned.
"""

import uuid
from datetime import datetime
from typing import Optional

COLLECTION = "client_notes"

MAX_NOTE_LENGTH = 2000
MAX_NOTES_RETURNED = 200


class NoteError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def clean_body(raw: str) -> str:
    body = (raw or "").strip()
    if not body:
        raise NoteError(400, "A note cannot be empty")
    if len(body) > MAX_NOTE_LENGTH:
        raise NoteError(400, f"A note must be {MAX_NOTE_LENGTH} characters or fewer")
    return body


async def assert_owns_group(db, group_id: str, user_id: str) -> None:
    """A note may only be attached to a client group the caller owns."""
    exists = await db["client_groups"].find_one({"id": group_id, "user_id": user_id}, {"_id": 1})
    if not exists:
        raise NoteError(404, "Client group not found")


def _iso(row: dict) -> dict:
    created = row.get("created_at")
    return {**row, "created_at": created.isoformat() if hasattr(created, "isoformat") else created}


async def list_notes(db, user_id: str, group_id: str, limit: int = MAX_NOTES_RETURNED) -> list[dict]:
    """Notes on one client, newest first."""
    await assert_owns_group(db, group_id, user_id)
    rows = await db[COLLECTION].find(
        {"group_id": group_id, "user_id": user_id}, {"_id": 0},
    ).sort("created_at", -1).to_list(length=limit)
    return [_iso(r) for r in rows[:limit]]


async def create_note(db, user_id: str, group_id: str, body: str, author: Optional[str] = None,
                      source: Optional[str] = None) -> dict:
    """Append a note and return it. `source` ("mcp") is recorded only when given."""
    body = clean_body(body)
    await assert_owns_group(db, group_id, user_id)
    note = {
        "id": uuid.uuid4().hex[:12],
        "group_id": group_id,
        "user_id": user_id,
        "author": (author or "").strip() or user_id,
        "body": body,
        "created_at": datetime.utcnow(),
    }
    if source:
        note["source"] = source
    await db[COLLECTION].insert_one(dict(note))
    return _iso(note)


async def update_note(db, user_id: str, note_id: str, body: str) -> dict:
    """Rewrite one note's text (the Context page's Edit). Scoped by user: another account's note
    is not found. Returns the note with `updated_at` set."""
    body = clean_body(body)
    q = {"id": note_id, "user_id": user_id}
    res = await db[COLLECTION].update_one(q, {"$set": {"body": body, "updated_at": datetime.utcnow()}})
    if res.matched_count == 0:
        raise NoteError(404, "Note not found")
    row = await db[COLLECTION].find_one(q, {"_id": 0})
    updated = row.get("updated_at")
    return {**_iso(row), "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated}


async def delete_note(db, user_id: str, group_id: str, note_id: str) -> None:
    """Remove one note. Scoped by user and client, so neither another account's
    nor another client's note can be removed."""
    result = await db[COLLECTION].delete_one({"id": note_id, "group_id": group_id, "user_id": user_id})
    if result.deleted_count == 0:
        raise NoteError(404, "Note not found")
