"""
routers/mcp_oauth.py
--------------------
Consent API behind the /connect/authorize page. An AI app (Claude, ChatGPT...)
starts an OAuth request at the MCP server; the owner, logged in with the normal
auth_token cookie, approves or denies it here. See services/oauth_store.py.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from billing_middleware import require_active_subscription
from core.config import CORS_ORIGINS, FRONTEND_URL
from core.database import get_db
from dependencies import get_mongo_client, get_current_claims
from services import oauth_store

logger = logging.getLogger(__name__)

router = APIRouter()


async def session_claims(request: Request) -> dict:
    """Claims of a real browser session. An MCP token (type "mcp") sent as the
    auth_token cookie must not be able to approve new connections."""
    claims = await get_current_claims(request)
    if claims.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return claims


async def same_origin_json(request: Request) -> None:
    """CSRF guard for the cookie-authenticated decision POST (cookies are SameSite=none)."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json")
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in {*CORS_ORIGINS, FRONTEND_URL}:
        raise HTTPException(status_code=403, detail="Origin not allowed")


class DecisionRequest(BaseModel):
    allow: bool
    access: Literal["read", "write"] = "read"


@router.get("/api/oauth/requests/{request_id}")
async def get_oauth_request(request_id: str, _claims: dict = Depends(session_claims)):
    """What the consent page shows. redirect_host is where the owner's approval
    is sent; client_name is chosen by the client and can be spoofed."""
    async with get_mongo_client() as mongo_client:
        view = await oauth_store.get_request_view(get_db(mongo_client), request_id)
    if not view:
        raise HTTPException(status_code=404, detail="This connection request has expired or was already used.")
    return view


@router.post("/api/oauth/requests/{request_id}/decision")
async def decide_oauth_request(request_id: str, body: DecisionRequest, claims: dict = Depends(session_claims),
                               _csrf: None = Depends(same_origin_json)):
    if claims.get("imp"):
        raise HTTPException(status_code=403, detail="Admins can't connect AI tools while viewing as a user.")
    user_id = claims["sub"]
    not_found = HTTPException(status_code=404, detail="This connection request has expired or was already used.")

    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        if not await oauth_store.get_request_view(db, request_id):
            raise not_found
        try:
            if body.allow:
                await require_active_subscription(user_id, mongo_client)
                redirect_to = await oauth_store.approve_request(db, request_id, user_id, body.access)
                logger.info(f"OAuth request approved by {user_id} (access={body.access})")
            else:
                redirect_to = await oauth_store.deny_request(db, request_id)
        except oauth_store.OAuthRequestNotFound:
            raise not_found
    return {"redirect_to": redirect_to}
