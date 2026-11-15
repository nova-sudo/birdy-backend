"""
ai/mcp/access.py
------------------
Scopes, tool tags and the access middleware for Birdy's MCP server.

Every tool is tagged exactly one of `read` / `write` (spread READ / WRITE / LIVE
into @mcp.tool). `live` tools additionally change live ads/campaigns or post to
Slack. A write-scope token also has read.

BirdyAccessMiddleware runs on every tool call:
- a write tool needs the `birdy.write` scope;
- external (`type: "mcp"`) tokens need an active/trialing subscription and are
  rate-limited per user (counted in Mongo: serverless has no shared memory).
Internal `type: "access"` tokens (orchestrator, login cookie) skip both gates.
"""

import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware

import billing_middleware
from core.config import FRONTEND_URL
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.outcome import reset_outcome, set_outcome, start_activity

READ_SCOPE = "birdy.read"
WRITE_SCOPE = "birdy.write"

READ = {"tags": {"read"}, "annotations": {"readOnlyHint": True}}
WRITE = {"tags": {"write"}, "annotations": {"readOnlyHint": False, "destructiveHint": False}}
LIVE = {"tags": {"write", "live"}, "annotations": {"readOnlyHint": False, "destructiveHint": True}}

MCP_RATE_LIMIT_PER_MINUTE = 120
_RATE_TTL_SECONDS = 120
_index_ready = False


def token_scopes(claims: dict) -> set[str]:
    """The space-separated `scope` claim. A missing claim means full access (internal
    `type: "access"` tokens and manual tokens issued before scopes existed)."""
    scope = claims.get("scope")
    if scope is None:
        # Only the two token kinds that predate scopes get full access by default.
        if claims.get("type") in ("access", "mcp"):
            return {READ_SCOPE, WRITE_SCOPE}
        return set()
    return set(str(scope).split())


def _minute() -> int:
    """The current fixed rate-limit window (patched in tests to pin the clock)."""
    return int(time.time() // 60)


async def _enforce_rate_limit(db, user_id: str) -> None:
    minute = _minute()
    global _index_ready
    coll = db["mcp_rate"]
    if not _index_ready:
        await coll.create_index("expires_at", expireAfterSeconds=0)
        _index_ready = True
    doc = await coll.find_one_and_update(
        {"user_id": user_id, "minute": minute},
        {"$inc": {"count": 1}, "$setOnInsert": {"expires_at": _expiry()}},
        upsert=True,
        return_document=True,
    )
    if doc["count"] > MCP_RATE_LIMIT_PER_MINUTE:
        set_outcome("refused", reason="rate_limit")
        raise ToolError("Too many Birdy requests in a minute. Wait a moment and try again.")


def _expiry():
    return datetime.now(timezone.utc) + timedelta(seconds=_RATE_TTL_SECONDS)


class BirdyAccessMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        reset_outcome()
        start_activity()
        token = get_access_token()
        claims = token.claims if token else {}
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)

        if "write" in (tool.tags or set()) and WRITE_SCOPE not in token_scopes(claims):
            set_outcome("refused", reason="view_only")
            raise ToolError(
                'This Birdy connection is view-only. Reconnect with "View and make changes" to do this.'
            )

        # Only the internal orchestrator/login token is exempt; any other type (mcp, missing,
        # refresh, ...) is treated as external.
        if claims.get("type") != "access":
            client = get_shared_mongo_client()
            try:
                await billing_middleware.require_active_subscription(claims.get("sub"), client)
            except HTTPException as exc:
                if exc.status_code != 402:
                    raise
                set_outcome("refused", reason="no_subscription")
                raise ToolError(
                    f"Your Birdy subscription is inactive. Reactivate it at {FRONTEND_URL}/settings?tab=billing "
                    "to keep using Birdy here."
                )
            await _enforce_rate_limit(get_db(client), claims.get("sub"))

        return await call_next(context)
