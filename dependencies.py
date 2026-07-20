"""
dependencies.py
---------------
Shared FastAPI dependencies used by both main.py and billing.py.
Extracted here to break the circular import:
  main.py → billing.py → main.py  (was circular)
  main.py → dependencies.py  ✓
  billing.py → dependencies.py  ✓
"""

import asyncio
import logging
import os

from fastapi import HTTPException, Request
from motor.motor_asyncio import AsyncIOMotorClient
from contextlib import asynccontextmanager
import jwt as pyjwt
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

logger = logging.getLogger(__name__)

from core.config import JWT_SECRET, JWT_ALGORITHM, JWT_EXPIRY_MINUTES, JWT_REFRESH_SECRET, JWT_REFRESH_EXPIRY_DAYS


@asynccontextmanager
async def get_mongo_client():
    """Async context manager that yields a connected AsyncIOMotorClient."""
    client = None
    try:
        loop = asyncio.get_event_loop()
        mongo_uri = os.getenv("MONGODB_URI")
        if not mongo_uri:
            raise HTTPException(
                status_code=500,
                detail="MongoDB configuration error: MONGODB_URI is not set"
            )
        client = AsyncIOMotorClient(mongo_uri, io_loop=loop)
        await client.admin.command("ping")
        yield client
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in MongoDB client setup: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected error setting up MongoDB client: {e}"
        )
    finally:
        if client:
            client.close()


async def generate_tokens(email: str):
    """Generate a (access_token, refresh_token) pair for the given email."""
    try:
        exp = int((datetime.utcnow() + timedelta(minutes=JWT_EXPIRY_MINUTES)).timestamp())
        access_token = pyjwt.encode(
            {"sub": email, "exp": exp, "type": "access"},
            JWT_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        ref_exp = int((datetime.utcnow() + timedelta(days=JWT_REFRESH_EXPIRY_DAYS)).timestamp())
        refresh_token = pyjwt.encode(
            {"sub": email, "exp": ref_exp, "type": "refresh"},
            JWT_REFRESH_SECRET,
            algorithm=JWT_ALGORITHM,
        )
        return access_token, refresh_token
    except Exception as e:
        logger.error(f"Error generating JWT tokens for {email}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate tokens: {e}")


async def get_current_user(request: Request) -> str:
    token = request.cookies.get("auth_token")
    if not token:
        raise HTTPException(status_code=401, detail="No authentication token provided")

    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        return email

    except pyjwt.ExpiredSignatureError:
        refresh_token = request.cookies.get("refresh_token")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Access token expired, please log in again")
        try:
            rp = pyjwt.decode(refresh_token, JWT_REFRESH_SECRET, algorithms=[JWT_ALGORITHM])
            email = rp.get("sub")
            if not email or rp.get("type") != "refresh":
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            access_token, new_refresh = await generate_tokens(email)
            request.state.new_tokens = {
                "auth_token": access_token,
                "refresh_token": new_refresh,
            }
            return email
        except pyjwt.PyJWTError as e:
            logger.error(f"Refresh token decode error: {e}")
            raise HTTPException(status_code=401, detail="Invalid refresh token")

    except pyjwt.PyJWTError as e:
        logger.error(f"JWT decode error: {e}")
        raise HTTPException(status_code=401, detail="Invalid authentication token")


async def get_current_claims(request: Request, verify_exp: bool = True) -> dict:
    """
    Return the full decoded JWT claims for the current request — including the
    impersonation markers `act` (the admin acting) and `imp` (True while
    impersonating), which get_current_user (which only returns `sub`) drops.

    Unlike get_current_user this does NOT run the refresh-token dance: the
    admin console and impersonation endpoints are short interactions, and the
    /impersonate/stop path deliberately needs to read `act` off an already-
    expired impersonation token (verify_exp=False) so a lapsed session can
    still be cleanly reverted to the admin.
    """
    token = request.cookies.get("auth_token")
    if not token:
        raise HTTPException(status_code=401, detail="No authentication token provided")
    try:
        return pyjwt.decode(
            token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
            options={"verify_exp": verify_exp},
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Access token expired, please log in again")
    except pyjwt.PyJWTError as e:
        logger.error(f"JWT decode error (claims): {e}")
        raise HTTPException(status_code=401, detail="Invalid authentication token")


async def require_admin(request: Request) -> str:
    """
    FastAPI dependency gating the internal Admin console. Returns the admin's
    email on success, else raises 403.

    Two checks:
      1. Reject any impersonation token (`imp`) — an impersonation session runs
         as a normal agency owner and must never be able to reach admin
         endpoints (no privilege re-escalation back into the console).
      2. Require the user document's `role == "admin"`.
    """
    from core.database import DB_NAME

    claims = await get_current_claims(request)
    if claims.get("imp"):
        raise HTTPException(
            status_code=403,
            detail="Impersonation sessions cannot access the admin console",
        )
    email = claims.get("sub")
    if not email:
        raise HTTPException(status_code=401, detail="Invalid token")

    async with get_mongo_client() as mongo_client:
        user = await mongo_client[DB_NAME]["users"].find_one(
            {"user_id": email}, {"role": 1}
        )
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return email