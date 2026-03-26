import logging
import bcrypt

from fastapi import APIRouter, Depends, HTTPException, Response

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


@router.post("/api/login")
async def login_user(request: LoginRequest, response: Response):
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
