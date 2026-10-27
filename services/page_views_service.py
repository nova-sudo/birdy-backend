"""
Named page views (users.page_views.<page>), shared by the settings router and
Birdy's create_saved_view chat tool so both enforce the same limits.

Errors are HTTPExceptions: the router passes them straight through, and the
chat tool turns their detail into a message the model can repeat.
"""

import json
import logging
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
