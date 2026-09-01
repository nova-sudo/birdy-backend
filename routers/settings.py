import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from core.config import COOKIE_DOMAIN, COOKIE_SAMESITE, COOKIE_SECURE
from core.database import DB_NAME
from core.models import (
    SaveViewRequest,
    HiddenMetricRequest,
    CapabilitiesRequest,
    CreatePageViewRequest,
    UpdatePageViewRequest,
    DefaultPageViewRequest,
)
from billing import ACTIVE_STATUSES
from dependencies import get_mongo_client, get_current_user
from services import capabilities_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/capabilities")
async def get_capabilities(current_user: str = Depends(get_current_user)):
    """
    Return the current user's Birdy AI capability flags (Settings -> Capabilities).
    Defaults are applied for anything not yet set. Response: { "media_buying": false }
    """
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        return await capabilities_service.get_capabilities(db, current_user)


@router.put("/api/capabilities")
async def update_capabilities(
    request: CapabilitiesRequest,
    current_user: str = Depends(get_current_user),
):
    """
    Enable/disable one or more capabilities. Body is a partial set of flags —
    only the fields sent are changed (e.g. { "media_buying": true }). Returns the
    full resolved capability set after the update.
    """
    updates = request.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No capability flags provided")
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        return await capabilities_service.set_capabilities(db, current_user, updates)


@router.delete("/api/integrations/gohighlevel/remove")
async def remove_gohighlevel_integration(
    response: Response,
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove GoHighLevel integration for the current user.
    Deletes agency token AND all subaccount tokens from MongoDB.
    Also clears the gohighlevel_tokens cookie.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.gohighlevel": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            # Clear the cookie
            response.delete_cookie(
                key="gohighlevel_tokens",
                path="/",
                domain=COOKIE_DOMAIN,
                samesite=COOKIE_SAMESITE,
                secure=COOKIE_SECURE,
            )

            logger.info(f"Removed GoHighLevel integration for user: {current_user}")
            return {"success": True, "message": "GoHighLevel integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing GHL integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")


@router.delete("/api/integrations/facebook/remove")
async def remove_facebook_integration(
    response: Response,
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove Meta (Facebook) integration for the current user.
    Deletes the access token from MongoDB and clears the facebook_tokens cookie.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.facebook": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            # Clear the cookie
            response.delete_cookie(
                key="facebook_tokens",
                path="/",
                domain=COOKIE_DOMAIN,
                samesite=COOKIE_SAMESITE,
                secure=COOKIE_SECURE,
            )

            logger.info(f"Removed Meta integration for user: {current_user}")
            return {"success": True, "message": "Meta integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing Facebook integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")


@router.delete("/api/integrations/hotprospector/remove")
async def remove_hotprospector_integration(
    current_user: str = Depends(get_current_user)
):
    """
    Permanently remove HotProspector integration for the current user.
    Deletes API credentials from MongoDB.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            result = await users_collection.update_one(
                {"user_id": current_user},
                {
                    "$unset": {
                        "integrations.hotprospector": ""
                    },
                    "$set": {
                        "updated_at": datetime.now()
                    }
                }
            )

            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")

            logger.info(f"Removed HotProspector integration for user: {current_user}")
            return {"success": True, "message": "HotProspector integration removed successfully"}

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing HotProspector integration for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove integration: {str(e)}")


@router.get("/api/user/views")
async def get_user_views(current_user: str = Depends(get_current_user)):
    """
    Return saved column-visibility views for the current user.
    Response: { "campaigns": [...], "contacts": [...], "clients": [...] }
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            user_doc = await db["users"].find_one(
                {"user_id": current_user},
                {"saved_views": 1}
            )
            return user_doc.get("saved_views", {}) if user_doc else {}
        except Exception as e:
            logger.error(f"Error fetching views for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/user/views")
async def save_user_view(
    request: SaveViewRequest,
    current_user: str = Depends(get_current_user)
):
    """
    Persist the visible-column list for one page.
    Body: { "page": "campaigns", "visible_columns": ["name", "spend", ...] }
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await db["users"].update_one(
                {"user_id": current_user},
                {
                    "$set": {
                        f"saved_views.{request.page}": request.visible_columns,
                        "updated_at": datetime.now()
                    }
                },
                upsert=True
            )
            logger.info(f"Saved '{request.page}' view for {current_user}: {len(request.visible_columns)} columns")
            return {"success": True, "page": request.page, "saved_columns": len(request.visible_columns)}
        except Exception as e:
            logger.error(f"Error saving view for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────────────────────────────
# Hidden metrics
#
# The Metrics Hub's show/hide eye. Hiding a metric doesn't delete anything —
# it takes the metric out of the formula builder's picker so it stops being
# offered when building new custom metrics. Formulas that already reference a
# now-hidden metric keep evaluating; hiding is about what you're offered next,
# not about breaking what exists.
# ──────────────────────────────────────────────────────────────────────────


@router.get("/api/user/hidden-metrics")
async def get_hidden_metrics(current_user: str = Depends(get_current_user)):
    """Return the metric ids this user has hidden. Response: { "hidden": [...] }"""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            user_doc = await db["users"].find_one(
                {"user_id": current_user},
                {"hidden_metrics": 1},
            )
            return {"hidden": (user_doc or {}).get("hidden_metrics", [])}
        except Exception as e:
            logger.error(f"Error fetching hidden metrics for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/user/hidden-metrics")
async def set_hidden_metric(
    request: HiddenMetricRequest,
    current_user: str = Depends(get_current_user),
):
    """Hide or show one metric. Body: { "metric_id": "meta_spend", "hidden": true }

    $addToSet/$pull rather than writing the whole array back, so a second tab
    toggling a different row doesn't overwrite this one.
    """
    metric_id = request.metric_id.strip()
    if not metric_id:
        raise HTTPException(status_code=400, detail="metric_id is required")

    op = "$addToSet" if request.hidden else "$pull"
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await db["users"].update_one(
                {"user_id": current_user},
                {op: {"hidden_metrics": metric_id}, "$set": {"updated_at": datetime.now()}},
                upsert=True,
            )
            user_doc = await db["users"].find_one(
                {"user_id": current_user},
                {"hidden_metrics": 1},
            )
            return {"hidden": (user_doc or {}).get("hidden_metrics", [])}
        except Exception as e:
            logger.error(f"Error setting hidden metric for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────────────────────────────
# Named page views
#
# Deliberately separate from `saved_views` above. That field holds one column
# layout per page — "where I left off" — and the tables keep autosaving into it
# on drag-reorder. These are named presets capturing the whole page state, and
# live under `page_views` so neither store can corrupt the other and an old
# frontend against a new backend keeps working unchanged.
#
# Shape:
#   users.page_views.<page> = {
#     "views": [ { id, name, state, created_at, updated_at }, ... ],
#     "default_view_id": "<id>" | None,
#   }
# ──────────────────────────────────────────────────────────────────────────

MAX_VIEWS_PER_PAGE = 20
MAX_VIEW_NAME_LEN = 60
# A page's state is columns + filters + sort — a few KB at most. The ceiling is
# here so a runaway client can't inflate the user document; see the 16 MB
# per-document limit noted in docs/mongodb-schema-audit.md.
MAX_VIEW_STATE_BYTES = 64 * 1024


def _clean_view_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="View name is required")
    if len(cleaned) > MAX_VIEW_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"View name must be {MAX_VIEW_NAME_LEN} characters or fewer",
        )
    return cleaned


def _check_view_state(state: dict) -> dict:
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="View state must be an object")
    size = len(json.dumps(state).encode("utf-8"))
    if size > MAX_VIEW_STATE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"View state is too large ({size} bytes, limit {MAX_VIEW_STATE_BYTES})",
        )
    return state


async def _get_page_bucket(db, current_user: str, page: str) -> dict:
    """Read one page's view bucket, normalised so callers never see a missing key."""
    user_doc = await db["users"].find_one(
        {"user_id": current_user}, {f"page_views.{page}": 1}
    )
    bucket = ((user_doc or {}).get("page_views") or {}).get(page) or {}
    return {
        "views": bucket.get("views") or [],
        "default_view_id": bucket.get("default_view_id"),
    }


@router.get("/api/user/page-views")
async def get_page_views(
    page: str | None = None,
    current_user: str = Depends(get_current_user),
):
    """
    Named page views for the current user.

    With `?page=clients`, returns that page's bucket:
        { "views": [...], "default_view_id": "..." | null }
    Without it, returns every page keyed by page slug.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            if page:
                return await _get_page_bucket(db, current_user, page)

            user_doc = await db["users"].find_one(
                {"user_id": current_user}, {"page_views": 1}
            )
            return (user_doc or {}).get("page_views") or {}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error fetching page views for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/user/page-views")
async def create_page_view(
    request: CreatePageViewRequest,
    current_user: str = Depends(get_current_user),
):
    """
    Create a named view. Body: { page, name, state }.
    The first view saved for a page becomes that page's default.
    """
    name = _clean_view_name(request.name)
    state = _check_view_state(request.state)

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            bucket = await _get_page_bucket(db, current_user, request.page)

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
                    f"page_views.{request.page}.views.name": {"$ne": name},
                },
                {
                    "$push": {f"page_views.{request.page}.views": view},
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
                        {f"page_views.{request.page}.default_view_id": None},
                        {f"page_views.{request.page}.default_view_id": {"$exists": False}},
                    ],
                },
                {"$set": {f"page_views.{request.page}.default_view_id": view["id"]}},
            )

            logger.info(
                f"Created page view '{name}' on '{request.page}' for {current_user}"
            )
            return view
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error creating page view for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.patch("/api/user/page-views/{view_id}")
async def update_page_view(
    view_id: str,
    request: UpdatePageViewRequest,
    current_user: str = Depends(get_current_user),
):
    """
    Rename a view, overwrite its state, or both. Body: { page, name?, state? }.
    Omitted fields are left as they are.
    """
    if request.name is None and request.state is None:
        raise HTTPException(status_code=400, detail="Nothing to update")

    name = _clean_view_name(request.name) if request.name is not None else None
    state = _check_view_state(request.state) if request.state is not None else None

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            bucket = await _get_page_bucket(db, current_user, request.page)

            index = next(
                (i for i, v in enumerate(bucket["views"]) if v.get("id") == view_id),
                None,
            )
            if index is None:
                raise HTTPException(status_code=404, detail="View not found")

            if name is not None and any(
                v.get("name", "").lower() == name.lower() and v.get("id") != view_id
                for v in bucket["views"]
            ):
                raise HTTPException(
                    status_code=409, detail=f'A view named "{name}" already exists'
                )

            view = {**bucket["views"][index], "updated_at": datetime.now().isoformat()}
            if name is not None:
                view["name"] = name
            if state is not None:
                view["state"] = state

            # Positional update of just this element — ids are unique, so the
            # first match is the only match. Writing the whole array back would
            # clobber any view created or edited in between.
            fields = {
                f"page_views.{request.page}.views.$.updated_at": view["updated_at"],
                "updated_at": datetime.now(),
            }
            if name is not None:
                fields[f"page_views.{request.page}.views.$.name"] = name
            if state is not None:
                fields[f"page_views.{request.page}.views.$.state"] = state

            await db["users"].update_one(
                {
                    "user_id": current_user,
                    f"page_views.{request.page}.views.id": view_id,
                },
                {"$set": fields},
            )
            logger.info(
                f"Updated page view {view_id} on '{request.page}' for {current_user}"
            )
            return view
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating page view for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/user/page-views/{view_id}")
async def delete_page_view(
    view_id: str,
    page: str,
    current_user: str = Depends(get_current_user),
):
    """Delete a named view. Clears the page default if it pointed here."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            bucket = await _get_page_bucket(db, current_user, page)

            remaining = [v for v in bucket["views"] if v.get("id") != view_id]
            if len(remaining) == len(bucket["views"]):
                raise HTTPException(status_code=404, detail="View not found")

            # $pull the one element rather than writing the survivors back, so a
            # view saved between the read above and this write is not erased.
            await db["users"].update_one(
                {"user_id": current_user},
                {
                    "$pull": {f"page_views.{page}.views": {"id": view_id}},
                    "$set": {"updated_at": datetime.now()},
                },
            )

            # Never leave default_view_id pointing at a deleted view — the
            # frontend would fall back to an unsaved layout with no explanation.
            if bucket["default_view_id"] == view_id:
                await db["users"].update_one(
                    {"user_id": current_user},
                    {"$set": {
                        f"page_views.{page}.default_view_id":
                            remaining[0]["id"] if remaining else None,
                    }},
                )
            logger.info(f"Deleted page view {view_id} on '{page}' for {current_user}")
            return {"success": True, "id": view_id, "remaining": len(remaining)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting page view for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/user/page-views/default")
async def set_default_page_view(
    request: DefaultPageViewRequest,
    current_user: str = Depends(get_current_user),
):
    """Set (or clear, with view_id: null) the view a page opens on."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            bucket = await _get_page_bucket(db, current_user, request.page)

            if request.view_id is not None and not any(
                v.get("id") == request.view_id for v in bucket["views"]
            ):
                raise HTTPException(status_code=404, detail="View not found")

            await db["users"].update_one(
                {"user_id": current_user},
                {"$set": {
                    f"page_views.{request.page}.default_view_id": request.view_id,
                    "updated_at": datetime.now(),
                }},
                upsert=True,
            )
            return {"success": True, "page": request.page, "default_view_id": request.view_id}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error setting default page view for {current_user}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


# ──────────────────────────────────────────────────────────────────────────
# User profile and password
#
# The Settings design's General tab edits a name, a currency, a timezone and
# a password. Only the first two were stored and none of them had a write
# path, so the tab was a placeholder — the fields existed in the design and
# nowhere else.
# ──────────────────────────────────────────────────────────────────────────

MAX_NAME_LENGTH = 120
MIN_PASSWORD_LENGTH = 8


@router.get("/api/user/profile")
async def get_user_profile(current_user: str = Depends(get_current_user)):
    """Name, email, currency and timezone for the General tab."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            doc = await db["users"].find_one(
                {"user_id": current_user},
                {"name": 1, "default_currency": 1, "timezone": 1, "_id": 0},
            ) or {}
            return {
                "name": doc.get("name") or "",
                # The user id IS the email; there is no separate field, which
                # is also why email is not editable here — changing it would
                # rekey every document that references the account.
                "email": current_user,
                "default_currency": doc.get("default_currency") or "USD",
                "timezone": doc.get("timezone") or "",
            }
        except Exception as e:
            logger.error(f"Error loading profile for {current_user}: {e}")
            raise HTTPException(status_code=500, detail="Failed to load profile")


@router.patch("/api/user/profile")
async def update_user_profile(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Update name, currency and/or timezone. Only the fields sent are written."""
    body = await request.json()
    updates = {}

    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        if len(name) > MAX_NAME_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"Name must be {MAX_NAME_LENGTH} characters or fewer",
            )
        updates["name"] = name

    if "default_currency" in body:
        currency = str(body.get("default_currency") or "").strip().upper()
        # Three letters is the ISO-4217 shape; the picker supplies the list, so
        # this only guards against a malformed direct call.
        if len(currency) != 3 or not currency.isalpha():
            raise HTTPException(status_code=400, detail="Currency must be a 3-letter code")
        updates["default_currency"] = currency

    if "timezone" in body:
        updates["timezone"] = str(body.get("timezone") or "").strip()

    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            updates["updated_at"] = datetime.now()
            result = await db["users"].update_one(
                {"user_id": current_user}, {"$set": updates}
            )
            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="User not found")
            logger.info(f"Profile updated for {current_user}: {sorted(updates)}")
            return {"success": True, "updated": [k for k in updates if k != "updated_at"]}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error updating profile for {current_user}: {e}")
            raise HTTPException(status_code=500, detail="Failed to update profile")


# Collections holding per-user data keyed by a plain `user_id` field —
# verified against every writer in routers/services before listing here.
# (Deliberately excludes admin_audit, promo_codes_meta, waitlist, and the
# Slack team/channel-scoped caches, none of which are this account's own data.)
ACCOUNT_DATA_COLLECTIONS = [
    "client_groups",
    "ghl_contacts",
    "hotprospector_leads",
    "facebook_leads",
    "facebook_ad_insights",
    "facebook_adset_insights",
    "facebook_campaign_insights",
    "call_logs",
    "alerts",
    "alert_notifications",
    "ai_chat_sessions",
    "ai_conversation_log",
    "ai_usage",
    "mcp_tokens",
    "meta_refresh_jobs",
]


@router.delete("/api/account")
async def delete_account(current_user: str = Depends(get_current_user)):
    """
    Permanently delete the signed-in account and everything Birdy stored for
    it. Irreversible — Settings gates this behind a type-to-confirm dialog.

    Blocked while a Whop subscription is still active/trialing/past_due/
    canceling: Whop plan changes and cancellation only happen through its
    hosted customer portal (see billing.py) — there's no cancel-by-API here —
    so deleting first would leave a subscription billing an account that no
    longer exists.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            user = await db["users"].find_one(
                {"user_id": current_user}, {"subscription": 1}
            )
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            sub_status = (user.get("subscription") or {}).get("status")
            if sub_status in ACTIVE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "You have an active subscription. Cancel it from Manage "
                        "Billing (Settings → Billing) before deleting your account."
                    ),
                )

            for collection in ACCOUNT_DATA_COLLECTIONS:
                await db[collection].delete_many({"user_id": current_user})

            await db["users"].delete_one({"user_id": current_user})

            logger.info(f"Deleted account and all data for {current_user}")
            return {"deleted": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting account for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete account")


@router.post("/api/user/password")
async def change_password(
    request: Request,
    current_user: str = Depends(get_current_user),
):
    """Change the account password, verifying the current one first."""
    import bcrypt

    body = await request.json()
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""

    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"New password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    if new_password == current_password:
        raise HTTPException(
            status_code=400, detail="New password must differ from the current one"
        )

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            doc = await db["users"].find_one({"user_id": current_user}, {"password": 1})
            if not doc:
                raise HTTPException(status_code=404, detail="User not found")

            stored = (doc.get("password") or "").encode("utf-8")
            if not stored or not bcrypt.checkpw(current_password.encode("utf-8"), stored):
                # Deliberately 400 rather than 401: a 401 is the app's
                # session-expired signal and would bounce the user to /login
                # mid-form (see apiRequest in lib/api.js).
                raise HTTPException(status_code=400, detail="Current password is incorrect")

            hashed = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")
            await db["users"].update_one(
                {"user_id": current_user},
                {"$set": {"password": hashed, "updated_at": datetime.now()}},
            )
            logger.info(f"Password changed for {current_user}")
            return {"success": True}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error changing password for {current_user}: {e}")
            raise HTTPException(status_code=500, detail="Failed to change password")
