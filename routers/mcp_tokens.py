"""
routers/mcp_tokens.py
-----------------------
Lets an authenticated Birdy user (via the normal auth_token cookie) mint,
list, and revoke long-lived bearer tokens for connecting external MCP
clients (Claude Desktop, Claude Code, etc.) to /mcp — see
services/mcp_token_service.py and ai/mcp/server.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from core.database import get_db
from core.models import CreateMcpTokenRequest
from dependencies import get_mongo_client, get_current_user, get_current_claims
from services.mcp_token_service import (
    create_mcp_token, list_mcp_tokens, revoke_mcp_token, list_connections, revoke_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/mcp/tokens")
async def create_token(
    request: CreateMcpTokenRequest,
    http_request: Request,
    current_user: str = Depends(get_current_user),
):
    """Mint a new MCP bearer token. The raw token is returned once — it is
    never stored or shown again, only its metadata (name, id, expiry)."""
    # get_current_user has already checked the session (and renewed an expired access token from the
    # refresh cookie), so the claims are read without the expiry check: an expired-but-signed token
    # still says whether this is an impersonation session, and a lapsed login no longer means a 401.
    claims = await get_current_claims(http_request, verify_exp=False)
    if claims.get("imp"):
        raise HTTPException(status_code=403, detail="Admins can't create AI connections while viewing as a user.")
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        result = await create_mcp_token(db, current_user, request.name, request.expiry_days, request.access)
        logger.info(f"Created MCP token '{request.name}' ({result['id']}) for {current_user}")
        return {"success": True, **result}


@router.get("/api/mcp/tokens")
async def list_tokens(current_user: str = Depends(get_current_user)):
    """List this user's issued MCP tokens (metadata only, never the raw secret)."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        tokens = await list_mcp_tokens(db, current_user)
        return {"tokens": tokens}


@router.delete("/api/mcp/tokens/{token_id}")
async def delete_token(token_id: str, current_user: str = Depends(get_current_user)):
    """Revoke an MCP token. Takes effect immediately — the next MCP request
    using it is rejected regardless of its JWT expiry."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        revoked = await revoke_mcp_token(db, current_user, token_id)
        if not revoked:
            raise HTTPException(status_code=404, detail="Token not found")
        logger.info(f"Revoked MCP token {token_id} for {current_user}")
        return {"success": True, "message": "Token revoked"}


@router.get("/api/mcp/connections")
async def get_connections(current_user: str = Depends(get_current_user)):
    """Everything connected to this user's Birdy over MCP: manual tokens and OAuth grants."""
    async with get_mongo_client() as mongo_client:
        return await list_connections(get_db(mongo_client), current_user)


@router.delete("/api/mcp/connections/{connection_id}")
async def delete_connection(connection_id: str, current_user: str = Depends(get_current_user)):
    """Revoke a manual token or an OAuth grant (with all its tokens). Immediate."""
    async with get_mongo_client() as mongo_client:
        if not await revoke_connection(get_db(mongo_client), current_user, connection_id):
            raise HTTPException(status_code=404, detail="Connection not found")
        logger.info(f"Revoked MCP connection {connection_id} for {current_user}")
        return {"success": True, "message": "Connection revoked"}
