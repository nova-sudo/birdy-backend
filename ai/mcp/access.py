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
Only the `type: "internal"` token Birdy's own chat mints in-process (ai/mcp_client)
skips both gates. Login cookies (`type: "access"`) are refused at /mcp altogether
(ai/mcp/oauth_provider.verify_birdy_token): every user holds their own, so
exempting them would let anyone use the MCP server without paying.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

import billing_middleware
from core.config import FRONTEND_URL
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp import voice
from ai.mcp.outcome import (CHAT_BILLED, get_outcome, is_error_result, reset_outcome, response_chars, set_outcome,
                             start_activity)
from services import mcp_activity, mcp_billing, posthog_events

logger = logging.getLogger(__name__)

READ_SCOPE = "birdy.read"
WRITE_SCOPE = "birdy.write"
# The token type of Birdy's own in-process chat calls (ai/mcp_client). Never set as a cookie
# (REST accepts only `type: "access"`), so no user ever holds one.
INTERNAL_TOKEN_TYPE = "internal"
# The only token types /mcp accepts: internal chat calls and external connections.
MCP_TOKEN_TYPES = (INTERNAL_TOKEN_TYPE, "mcp")

READ = {"tags": {"read"}, "annotations": {"readOnlyHint": True}}
WRITE = {"tags": {"write"}, "annotations": {"readOnlyHint": False, "destructiveHint": False}}
LIVE = {"tags": {"write", "live"}, "annotations": {"readOnlyHint": False, "destructiveHint": True}}

MCP_RATE_LIMIT_PER_MINUTE = 120
_RATE_TTL_SECONDS = 120
_index_ready = False
_CHARGE_TIMEOUT_SECONDS = 0.3
_FREE_OUTCOMES = {"refused", "bad_code", "state_changed"}
_ZERO_CREDITS = {"request": 0.0, "data": 0.0, "live": 0.0, "total": 0.0}
_background: set = set()  # strong refs to in-flight charge-and-record tasks


def token_scopes(claims: dict) -> set[str]:
    """The space-separated `scope` claim. A missing claim means full access (internal
    chat tokens and manual tokens issued before scopes existed)."""
    scope = claims.get("scope")
    if scope is None:
        # Only the two token kinds that predate scopes get full access by default.
        if claims.get("type") in MCP_TOKEN_TYPES:
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
        raise ToolError("Too many Birdy requests in a minute. Wait a moment and try again. No credits were used.")


def _expiry():
    return datetime.now(timezone.utc) + timedelta(seconds=_RATE_TTL_SECONDS)


def _result_payload(result):
    """What the client got, for the activity excerpt: the structured JSON, else the text blocks."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    text = "".join(getattr(b, "text", "") or "" for b in getattr(result, "content", None) or [])
    return text or None


def _kind_for(tags: set, outcome_kind: str | None) -> str:
    if outcome_kind == "preview":
        return "preview"
    if outcome_kind == "live_executed" and "live" in tags:
        return "live"
    if outcome_kind in _FREE_OUTCOMES:
        return "refused"
    return "change" if "write" in tags else "lookup"


async def _app_name(db, claims: dict) -> str:
    """The claim `client_name`, else the connection's name from mcp_tokens by jti (looked up per call:
    serverless has no shared memory to cache in). OAuth access tokens carry no client_name claim, so they
    take the second path too."""
    if claims.get("client_name"):
        return claims["client_name"]
    if claims.get("jti"):
        doc = await db["mcp_tokens"].find_one({"jti": claims["jti"]}, {"name": 1})
        if doc and doc.get("name"):
            return doc["name"]
    return "Unknown app"


def _track(user_id, tool, kind, reason, live_ok, failed, credits_total, chars, duration_ms, app_name) -> None:
    """PostHog events for one external call. Only these fields: never args, results or message text."""
    posthog_events.capture(user_id, "mcp_tool_called", {
        "tool": tool, "kind": kind, "result_status": "error" if failed else "ok", "credits_total": credits_total,
        "response_chars": chars, "duration_ms": duration_ms, "app_name": app_name,
    })
    if reason:
        posthog_events.capture(user_id, "mcp_refused", {"tool": tool, "reason": reason})
    if live_ok:
        posthog_events.capture(user_id, "mcp_live_confirmed", {"tool": tool, "app_name": app_name})


async def _charge_and_record(db, claims: dict, tool_name: str, tags: set, args: dict, result, error, started: float,
                             started_at: datetime | None = None) -> None:
    """Charge (when the call earned a charge) and write the activity row, concurrently.

    The row stores the price the charge formula gives (so a slow or failed charge still logs the amount).
    Each half logs its own failure; neither raises."""
    outcome = get_outcome() or {}
    okind = outcome.get("kind")
    structured = getattr(result, "structured_content", None)
    free = okind in _FREE_OUTCOMES or error is not None or is_error_result(structured)
    # A confirmed two_step call is "live_executed"; only a LIVE-tagged tool pays the live fee.
    live = okind == "live_executed" and "live" in tags
    chars = response_chars(result) if result is not None else 0
    grant_id = claims.get("grant_id") or claims.get("jti")
    duration_ms = int((time.monotonic() - started) * 1000)

    # A chat reply (CHAT_BILLED) was already billed once by the chat engine; the MCP fee is not added on top.
    chat_billed = okind == CHAT_BILLED

    async def charge():
        if not free and not chat_billed:
            await mcp_billing.charge_mcp_call(db, claims.get("sub"), tool=tool_name, grant_id=grant_id, chars=chars, live=live)

    async def record():
        reason = outcome.get("reason") if okind == "refused" else okind if okind in _FREE_OUTCOMES else None
        rate_limited = okind == "refused" and outcome.get("reason") == "rate_limit"
        # Analytics must not depend on Mongo: a failed lookup sends None and re-raises after the events fire.
        app_name, name_error, credits, credits_error = None, None, None, None
        try:
            app_name = await _app_name(db, claims)
        except Exception as exc:
            name_error = exc
        if not rate_limited:
            try:
                if free:
                    credits = _ZERO_CREDITS
                elif chat_billed:
                    cost = round(float(outcome.get("credits") or 0.0), 4)
                    credits = {"request": cost, "data": 0.0, "live": 0.0, "total": cost}
                else:
                    credits = await mcp_billing.charge_breakdown(db, chars, live)
            except Exception as exc:
                credits_error = exc
        if rate_limited:
            posthog_events.capture(claims.get("sub"), "mcp_refused", {"tool": tool_name, "reason": "rate_limit"})
        else:
            _track(claims.get("sub"), tool_name, _kind_for(tags, okind), reason, live and not free,
                   free or error is not None, credits["total"] if credits else None, chars, duration_ms, app_name)
        if name_error or credits_error:
            raise name_error or credits_error
        app_kind = mcp_activity.app_kind_for(app_name)
        if rate_limited:
            await mcp_activity.record_rate_limited(db, claims.get("sub"), grant_id, app_name, app_kind, created_at=started_at)
            return
        await mcp_activity.record_call(
            db, user_id=claims.get("sub"), grant_id=grant_id, app_name=app_name, app_kind=app_kind,
            access="write" if WRITE_SCOPE in token_scopes(claims) else "read",
            tool=tool_name, kind=_kind_for(tags, okind), refusal_reason=reason, args=args,
            result=str(error) if error is not None else _result_payload(result),
            result_status="error" if (free or error is not None) else "ok",
            credits=credits, chars=chars, duration_ms=duration_ms, confirmed_after=outcome.get("confirmed_after"),
            created_at=started_at,
        )

    for name, outcome_of in zip(("charge", "activity log"), await asyncio.gather(charge(), record(), return_exceptions=True)):
        if isinstance(outcome_of, BaseException):
            logger.warning("MCP %s failed for %s: %r", name, tool_name, outcome_of)


async def _finish_call(db, claims, tool_name, tags, args, result, error, started, started_at=None) -> None:
    """Charge and log one external call within the latency budget.

    The work runs as its own task, shielded from the timeout's cancel: a deduction is followed by its
    ledger row (see credits._apply_charge) and a cancel between the two would lose that row. On timeout we
    warn and return the result; the task keeps running to completion."""
    task = asyncio.ensure_future(_charge_and_record(db, claims, tool_name, tags, args, result, error, started, started_at))
    _background.add(task)
    task.add_done_callback(_background.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), _CHARGE_TIMEOUT_SECONDS)
    except Exception as exc:  # incl. TimeoutError: a billing or logging hiccup never costs the caller their result
        logger.warning("MCP charge/activity log did not finish for %s: %r", tool_name, exc)


async def _with_signoff(db, tool_name: str, tags: set, args: dict, result, available: float | None):
    """The result plus Birdy's sign-off line (a "see it in Birdy" link and what the request cost).

    Built after the charge, from the same price formula, so the line never counts toward the size charged.
    A refused, failed or error result is returned as it is. Never raises: no sign-off is better than no result."""
    try:
        outcome = get_outcome() or {}
        if outcome.get("kind") in _FREE_OUTCOMES or is_error_result(getattr(result, "structured_content", None)):
            return result
        if outcome.get("kind") == CHAT_BILLED:
            # What the chat reply was billed, and the page that reply lives on.
            cost = round(float(outcome.get("credits") or 0.0), 4)
            link = outcome.get("link") or mcp_activity.link_for(tool_name, args)
            lookup = False
        else:
            live = outcome.get("kind") == "live_executed" and "live" in tags
            cost = (await mcp_billing.charge_breakdown(db, response_chars(result), live))["total"]
            link = mcp_activity.link_for(tool_name, args)
            lookup = "write" not in tags
        left = available - cost if available is not None else None
        line = voice.closing_block(link, cost, left, lookup=lookup)
        if not line:
            return result
        return ToolResult(content=[*result.content, TextContent(type="text", text=line)],
                          structured_content=result.structured_content, meta=result.meta)
    except Exception as exc:
        logger.warning("MCP sign-off skipped for %s: %r", tool_name, exc)
        return result


class BirdyAccessMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        reset_outcome()
        start_activity()
        started = time.monotonic()
        started_at = datetime.now(timezone.utc)
        token = get_access_token()
        claims = token.claims if token else {}
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        # Only Birdy's own in-process chat token is exempt; any other type is treated as external.
        external = claims.get("type") != INTERNAL_TOKEN_TYPE
        args = getattr(context.message, "arguments", None) or {}
        db = get_db(get_shared_mongo_client()) if external else None
        available = None  # the balance before this call, when known (for the sign-off line)

        try:
            if external:
                # First, so a looping client's refusals are bounded too (a rate-limit refusal is free and collapses to one row).
                await _enforce_rate_limit(db, claims.get("sub"))

            if external and not token_scopes(claims) & {READ_SCOPE, WRITE_SCOPE}:  # a write token also reads
                # A signed token with no Birdy scope (e.g. some other short-lived JWT) gets nothing, not even reads.
                set_outcome("refused", reason="no_scope")
                raise ToolError("This token does not grant access to Birdy. Reconnect Birdy to continue. No credits were used.")

            if "write" in (tool.tags or set()) and WRITE_SCOPE not in token_scopes(claims):
                set_outcome("refused", reason="view_only")
                raise ToolError(
                    "Birdy can only look right now: this connection is view-only, so it can't make changes. "
                    'Reconnect Birdy with "View and make changes" and ask again. You will see a preview before '
                    "anything changes. No credits were used."
                )

            if external:
                try:
                    await billing_middleware.require_active_subscription(claims.get("sub"), get_shared_mongo_client())
                except HTTPException as exc:
                    if exc.status_code != 402:
                        raise
                    set_outcome("refused", reason="no_subscription")
                    raise ToolError(
                        f"Your Birdy subscription is inactive. Reactivate it at {FRONTEND_URL}/settings?tab=billing "
                        "to keep using Birdy here. No credits were used."
                    )
                allowed, available = await mcp_billing.credit_state(db, claims.get("sub"))
                if not allowed:
                    set_outcome("refused", reason="out_of_credits")
                    raise ToolError(mcp_billing.OUT_OF_CREDITS_MESSAGE)

            result = await call_next(context)
        except Exception as exc:
            if external:
                await _finish_call(db, claims, context.message.name, tool.tags or set(), args, None, exc, started, started_at)
            raise
        if external:
            await _finish_call(db, claims, context.message.name, tool.tags or set(), args, result, None, started, started_at)
            return await _with_signoff(db, context.message.name, tool.tags or set(), args, result, available)
        return result
