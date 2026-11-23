"""
sandbox/state.py
----------------
Writes a sandbox account makes through a fake (pause an ad, change a budget, rename, send a
message, add a tag or a note, create a creative test) are remembered in the Mongo collection
``sandbox_state``, one document per (user_id, kind, key):

    {user_id, kind, key, value, at}

Later reads reflect them: ``overrides(user_id)`` turns the kinds the world understands
(``status``, ``budget``, ``name``) into a world.Overrides; any other kind is free for a fake to
use and read back with ``get`` / ``list``.

    await state.set(user_id, "status", ad_id, "PAUSED")
    await state.set(user_id, "ghl_note", note_id, {"contactId": cid, "body": "..."})
    w = world.world_for(user_id, overrides=await state.overrides(user_id))

``db`` defaults to the app database (core.mongo_client's shared client), so tests using the
``mock_db`` fixture get the in-memory one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

COLLECTION = "sandbox_state"

# Kinds the world applies (sandbox.world.Overrides): object id -> value.
STATUS, BUDGET, NAME = "status", "budget", "name"
TIMELINE_KINDS = (STATUS, BUDGET)   # these keep a `history` of every change (value, at)


def _db(db=None):
    if db is not None:
        return db
    from core.database import DB_NAME
    from core.mongo_client import get_shared_mongo_client

    return get_shared_mongo_client()[DB_NAME]


def _uid(user_id: str) -> str:
    return (user_id or "").strip().lower()


def _aware(at) -> datetime:
    if at is None:
        return datetime.now(timezone.utc)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


async def set(user_id: str, kind: str, key: str, value: Any, *, at: Optional[datetime] = None, db=None) -> dict:  # noqa: A001
    """Remember ``value`` for (kind, key), replacing what was there. Returns the stored document."""
    doc = {"user_id": _uid(user_id), "kind": kind, "key": str(key), "value": value, "at": _aware(at)}
    update = {"$set": doc}
    if kind in TIMELINE_KINDS:
        # Status and budget keep every change: a later write must not rewrite the past.
        update["$push"] = {"history": {"value": value, "at": doc["at"]}}
    await _db(db)[COLLECTION].update_one(
        {"user_id": doc["user_id"], "kind": kind, "key": doc["key"]}, update, upsert=True)
    return doc


async def get(user_id: str, kind: str, key: str, default: Any = None, *, db=None) -> Any:
    """The remembered value for (kind, key), or ``default``."""
    doc = await _db(db)[COLLECTION].find_one({"user_id": _uid(user_id), "kind": kind, "key": str(key)})
    return doc["value"] if doc else default


async def list(user_id: str, kind: Optional[str] = None, *, db=None) -> list:  # noqa: A001
    """Every remembered write of the user (of one kind), oldest first: [{kind, key, value, at}]."""
    query = {"user_id": _uid(user_id)}
    if kind is not None:
        query["kind"] = kind
    docs = await _db(db)[COLLECTION].find(query, {"_id": 0, "user_id": 0, "history": 0}).to_list(length=None)
    for d in docs:
        d["at"] = _aware(d.get("at"))
    docs.sort(key=lambda d: d["at"])
    return docs


async def delete(user_id: str, kind: str, key: str, *, db=None) -> bool:
    res = await _db(db)[COLLECTION].delete_one({"user_id": _uid(user_id), "kind": kind, "key": str(key)})
    return bool(res.deleted_count)


async def overrides(user_id: str, *, db=None):
    """The world.Overrides for the user's remembered status / budget / name writes."""
    from sandbox.world import Overrides

    out = Overrides()
    docs = await _db(db)[COLLECTION].find(
        {"user_id": _uid(user_id), "kind": {"$in": [STATUS, BUDGET, NAME]}}, {"_id": 0}).to_list(length=None)
    for d in docs:
        if d["kind"] == NAME:
            out.name[d["key"]] = (d["value"], _aware(d.get("at")))
            continue
        events = [(h["value"], _aware(h.get("at"))) for h in d.get("history") or []] or \
                 [(d["value"], _aware(d.get("at")))]
        target = out.status if d["kind"] == STATUS else out.budget
        target[d["key"]] = tuple(sorted(events, key=lambda e: e[1]))
    return out


async def create_sandbox_state_indexes(client) -> None:
    """One document per (user_id, kind, key). Registered in core/indexes.py."""
    from core.database import DB_NAME

    await client[DB_NAME][COLLECTION].create_index([("user_id", 1), ("kind", 1), ("key", 1)], unique=True)
