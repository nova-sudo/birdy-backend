import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response

from core.config import COOKIE_DOMAIN, COOKIE_SAMESITE, COOKIE_SECURE
from core.database import DB_NAME
from core.models import SaveViewRequest
from dependencies import get_mongo_client, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


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
