"""
Named page views (users.page_views.<page>), shared by the settings router and
Birdy's create_saved_view chat tool so both enforce the same limits.

Errors are HTTPExceptions: the router passes them straight through, and the
chat tool turns their detail into a message the model can repeat.
"""

import json
import logging
import re
import uuid
from datetime import datetime

from fastapi import HTTPException

logger = logging.getLogger(__name__)

MAX_VIEWS_PER_PAGE = 20
MAX_VIEW_NAME_LEN = 60
# A page's state is columns + filters + sort — a few KB at most. The ceiling is
# here so a runaway client can't inflate the user document; see the 16 MB
# per-document limit noted in docs/mongodb-schema-audit.md.
MAX_VIEW_STATE_BYTES = 64 * 1024


def clean_view_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="View name is required")
    if len(cleaned) > MAX_VIEW_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"View name must be {MAX_VIEW_NAME_LEN} characters or fewer",
        )
    return cleaned


def check_view_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="View state must be an object")
    size = len(json.dumps(state).encode("utf-8"))
    if size > MAX_VIEW_STATE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"View state is too large ({size} bytes, limit {MAX_VIEW_STATE_BYTES})",
        )
    return state


async def get_page_bucket(db, current_user: str, page: str) -> dict:
    """Read one page's view bucket, normalised so callers never see a missing key."""
    user_doc = await db["users"].find_one(
        {"user_id": current_user}, {f"page_views.{page}": 1}
    )
    bucket = ((user_doc or {}).get("page_views") or {}).get(page) or {}
    return {
        "views": bucket.get("views") or [],
        "default_view_id": bucket.get("default_view_id"),
    }


async def create_page_view(db, current_user: str, page: str, name: str, state: dict) -> dict:
    """Create a named view. The first view saved for a page becomes its default."""
    name = clean_view_name(name)
    state = check_view_state(state)
    bucket = await get_page_bucket(db, current_user, page)

    if len(bucket["views"]) >= MAX_VIEWS_PER_PAGE:
        raise HTTPException(
            status_code=400,
            detail=f"You can save up to {MAX_VIEWS_PER_PAGE} views per page. "
                   "Delete one to make room.",
        )
    if any(v.get("name", "").lower() == name.lower() for v in bucket["views"]):
        raise HTTPException(
            status_code=409, detail=f'A view named "{name}" already exists'
        )

    now = datetime.now().isoformat()
    view = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "state": state,
        "created_at": now,
        "updated_at": now,
    }

    # $push, never a whole-array $set. Rewriting the array from a value
    # read moments earlier loses any view saved concurrently — two rapid
    # saves would both succeed and one would vanish. The name guard in
    # the filter closes the same race on duplicate names.
    result = await db["users"].update_one(
        {
            "user_id": current_user,
            f"page_views.{page}.views.name": {"$ne": name},
        },
        {
            "$push": {f"page_views.{page}.views": view},
            "$set": {"updated_at": datetime.now()},
        },
        upsert=True,
    )
    if result.matched_count == 0 and result.upserted_id is None:
        raise HTTPException(
            status_code=409, detail=f'A view named "{name}" already exists'
        )

    # First view on a page becomes the default, so the next visit lands
    # on something rather than on an unsaved layout. Conditional in the
    # filter so only the first view to land claims it.
    await db["users"].update_one(
        {
            "user_id": current_user,
            "$or": [
                {f"page_views.{page}.default_view_id": None},
                {f"page_views.{page}.default_view_id": {"$exists": False}},
            ],
        },
        {"$set": {f"page_views.{page}.default_view_id": view["id"]}},
    )

    logger.info(f"Created page view '{name}' on '{page}' for {current_user}")
    return view


async def set_default_page_view(db, current_user: str, page: str, view_id) -> None:
    """Set (or clear, with view_id None) the view a page opens on."""
    bucket = await get_page_bucket(db, current_user, page)
    if view_id is not None and not any(v.get("id") == view_id for v in bucket["views"]):
        raise HTTPException(status_code=404, detail="View not found")
    await db["users"].update_one(
        {"user_id": current_user},
        {"$set": {
            f"page_views.{page}.default_view_id": view_id,
            "updated_at": datetime.now(),
        }},
        upsert=True,
    )


# ── Hub column lists for Birdy ──────────────────────────────────────────────
# Each hub table builds its columns in the browser (GHL fields, tags), so the
# frontend saves them here when a hub renders: users.view_catalogues.<page> =
# {label, path, columns: [{id, label, source}], updated_at}. Birdy's view tools
# read these, so a view can be made from any page or device once the hub has
# been opened anywhere.

MAX_CATALOGUE_COLUMNS = 400
_CATALOGUE_PAGE = re.compile(r"^[a-z0-9_]{1,40}$")


def clean_view_catalogue(page: str, label, path, columns) -> dict:
    if not _CATALOGUE_PAGE.match(page or ""):
        raise HTTPException(status_code=400, detail="Invalid page")
    if not isinstance(columns, list) or not columns:
        raise HTTPException(status_code=400, detail="columns must be a non-empty list")
    cleaned = []
    for c in columns[:MAX_CATALOGUE_COLUMNS]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"]:
            raise HTTPException(status_code=400, detail="Each column needs a string id")
        col = {"id": c["id"][:200], "label": str(c.get("label") or c["id"])[:200]}
        if isinstance(c.get("source"), str):
            col["source"] = c["source"][:40]
        cleaned.append(col)
    return {
        "label": str(label or page)[:80],
        "path": str(path)[:200] if path else None,
        "columns": cleaned,
    }


async def save_view_catalogue(db, current_user: str, page: str, entry: dict) -> None:
    entry = clean_view_catalogue(page, entry.get("label"), entry.get("path"), entry.get("columns"))
    await db["users"].update_one(
        {"user_id": current_user},
        {"$set": {f"view_catalogues.{page}": {**entry, "updated_at": datetime.now().isoformat()}}},
        upsert=True,
    )


async def get_view_catalogues(db, current_user: str) -> dict:
    user_doc = await db["users"].find_one({"user_id": current_user}, {"view_catalogues": 1})
    return (user_doc or {}).get("view_catalogues") or {}
