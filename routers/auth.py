import logging
import bcrypt

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response

from core.config import (
    JWT_EXPIRY_MINUTES,
    JWT_REFRESH_EXPIRY_DAYS,
    COOKIE_DOMAIN,
    COOKIE_SAMESITE,
    COOKIE_SECURE,
)
from core.database import DB_NAME
from core.models import RegisterRequest, LoginRequest
from core.utils import set_cookie
from dependencies import get_mongo_client, get_current_user, generate_tokens
from datetime import datetime

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/auth/check")
async def check_auth(current_user: str = Depends(get_current_user)):
    """Simple endpoint to check if user is authenticated"""
    return {
        "authenticated": True,
        "user": current_user
    }


# Register endpoint
@router.post("/api/register")
async def register_user(request: RegisterRequest, response: Response):
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            users_collection = db["users"]
            existing_user = await users_collection.find_one({"user_id": request.email})
            if existing_user:
                raise HTTPException(status_code=400, detail="Email already registered")
            hashed_password = bcrypt.hashpw(request.password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            user_doc = {
                "user_id": request.email,
                "name": request.name,
                "password": hashed_password,
                "created_at": datetime.now(),
                "updated_at": datetime.now(),
                "default_currency": request.default_currency,
                "integrations": {}
            }
            await users_collection.insert_one(user_doc)
            logger.info(f"Registered user: {request.email}")

            access_token, refresh_token = await generate_tokens(request.email)

            set_cookie(response, "auth_token", access_token, JWT_EXPIRY_MINUTES * 60)
            set_cookie(response, "refresh_token", refresh_token, JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60)

            logger.info(f"Set auth_token and refresh_token cookies for user: {request.email}")
            return {"message": "Registration successful", "user": {"email": request.email, "name": request.name}}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error registering user: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to register user: {str(e)}")


async def _refresh_ghl_tokens_on_login(user_id: str):
    """Refresh GHL agency + location tokens in the background after login."""
    try:
        from integrations.gohighlevel import (
            ghl_integration, get_agency_token, save_agency_token,
            get_subaccount_tokens, save_subaccount_token,
            fetch_location_details, get_contact_count_from_ghl,
        )
        async with get_mongo_client() as mongo_client:
            agency_token = await get_agency_token(user_id, mongo_client)
            if not agency_token or not agency_token.get("refresh_token"):
                return

            # Refresh agency token
            success, result = await ghl_integration.refresh_agency_token(
                agency_token["refresh_token"]
            )
            if not success:
                logger.warning(f"GHL agency token refresh failed for {user_id}: {result.get('error')}")
                return

            await save_agency_token(user_id, result, mongo_client)
            logger.info(f"GHL agency token refreshed on login for {user_id}")

            # Regenerate location tokens with the fresh agency token
            company_id = result.get("companyId") or agency_token.get("company_id")
            agency_access = result.get("access_token")
            if not company_id or not agency_access:
                return

            subaccounts = await get_subaccount_tokens(user_id, mongo_client)
            for location_id in (subaccounts or {}):
                try:
                    ok, loc_tokens = await ghl_integration.generate_location_token(
                        company_id, location_id, agency_access
                    )
                    if ok:
                        details = await fetch_location_details(
                            location_id, loc_tokens.get("access_token")
                        )
                        count = await get_contact_count_from_ghl(
                            location_id, loc_tokens.get("access_token")
                        )
                        await save_subaccount_token(
                            user_id, location_id, loc_tokens, mongo_client,
                            details, contact_count=count
                        )
                except Exception as e:
                    logger.warning(f"Failed to refresh location {location_id}: {e}")

    except Exception as e:
        logger.error(f"GHL token refresh on login failed for {user_id}: {e}", exc_info=True)


@router.post("/api/login")
async def login_user(request: LoginRequest, response: Response, background_tasks: BackgroundTasks):
    async with get_mongo_client() as mongo_client:
        try:
            logger.debug(f"Starting login for user {request.email}, rememberMe: {request.rememberMe}")
            db = mongo_client[DB_NAME]
            users_collection = db["users"]

            logger.debug(f"Querying user with email {request.email}")
            user_doc = await users_collection.find_one({"user_id": request.email})

            if not user_doc:
                logger.warning(f"No user found with email {request.email}")
                raise HTTPException(status_code=401, detail="Invalid email or password")

            logger.debug(f"User found: {user_doc['user_id']}, verifying password")
            if not bcrypt.checkpw(request.password.encode('utf-8'), user_doc["password"].encode('utf-8')):
                logger.warning(f"Password mismatch for user {request.email}")
                raise HTTPException(status_code=401, detail="Invalid email or password")

            # ============================================
            # GENERATE JWT TOKENS
            # ============================================
            logger.debug(f"Generating JWT tokens for {request.email}")
            access_token, refresh_token = await generate_tokens(request.email)

            access_token_max_age = (JWT_EXPIRY_MINUTES * 60) if not request.rememberMe else (30 * 24 * 60 * 60)
            refresh_token_max_age = (JWT_REFRESH_EXPIRY_DAYS * 24 * 60 * 60) if not request.rememberMe else (90 * 24 * 60 * 60)

            logger.debug(
                f"Setting cookies: access_token_max_age={access_token_max_age}, "
                f"refresh_token_max_age={refresh_token_max_age}"
            )

            set_cookie(response, "auth_token", access_token, access_token_max_age)
            set_cookie(response, "refresh_token", refresh_token, refresh_token_max_age)

            # Refresh GHL tokens in background (non-blocking)
            ghl_data = user_doc.get("integrations", {}).get("gohighlevel", {})
            if ghl_data.get("agency", {}).get("refresh_token"):
                background_tasks.add_task(_refresh_ghl_tokens_on_login, request.email)

            # Read default_currency from the user doc
            default_currency = user_doc.get("default_currency")
            logger.info(f"User [{request.email}] logged in | default_currency={default_currency}")

            return {
                "message": "Login successful",
                "user": {
                    "email": request.email,
                    "name": user_doc.get("name"),
                    "default_currency": default_currency,
                }
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error logging in user {request.email}: {str(e)}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to login user: {str(e)}")


@router.post("/api/logout")
async def logout_user(response: Response):
    response.delete_cookie(
        key="auth_token",
        path="/",
        domain=COOKIE_DOMAIN,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE
    )
    response.delete_cookie(
        key="refresh_token",
        path="/",
        domain=COOKIE_DOMAIN,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE
    )
    logger.info("User logged out, auth_token and refresh_token cleared")
    return {"message": "Logout successful"}
