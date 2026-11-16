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

import asyncio
import logging
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
from ai.mcp.outcome import get_outcome, is_error_result, reset_outcome, response_chars, set_outcome, start_activity
from services import mcp_activity, mcp_billing, posthog_events

logger = logging.getLogger(__name__)

READ_SCOPE = "birdy.read"
WRITE_SCOPE = "birdy.write"

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
    if outcome_kind == "live_executed":
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


async def _charge_and_record(db, claims: dict, tool_name: str, tags: set, args: dict, result, error, started: float) -> None:
    """Charge (when the call earned a charge) and write the activity row, concurrently.

    The row stores the price the charge formula gives (so a slow or failed charge still logs the amount).
    Each half logs its own failure; neither raises."""
    outcome = get_outcome() or {}
    okind = outcome.get("kind")
    structured = getattr(result, "structured_content", None)
    free = okind in _FREE_OUTCOMES or error is not None or is_error_result(structured)
    live = okind == "live_executed"
    chars = response_chars(result) if result is not None else 0
    grant_id = claims.get("grant_id") or claims.get("jti")
    duration_ms = int((time.monotonic() - started) * 1000)

    async def charge():
        if not free:
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
                credits = _ZERO_CREDITS if free else await mcp_billing.charge_breakdown(db, chars, live)
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
            await mcp_activity.record_rate_limited(db, claims.get("sub"), grant_id, app_name, app_kind)
            return
        await mcp_activity.record_call(
            db, user_id=claims.get("sub"), grant_id=grant_id, app_name=app_name, app_kind=app_kind,
            access="write" if WRITE_SCOPE in token_scopes(claims) else "read",
            tool=tool_name, kind=_kind_for(tags, okind), refusal_reason=reason, args=args,
            result=str(error) if error is not None else _result_payload(result),
            result_status="error" if (free or error is not None) else "ok",
            credits=credits, chars=chars, duration_ms=duration_ms, confirmed_after=outcome.get("confirmed_after"),
        )

    for name, outcome_of in zip(("charge", "activity log"), await asyncio.gather(charge(), record(), return_exceptions=True)):
        if isinstance(outcome_of, BaseException):
            logger.warning("MCP %s failed for %s: %r", name, tool_name, outcome_of)


async def _finish_call(db, claims, tool_name, tags, args, result, error, started) -> None:
    """Charge and log one external call within the latency budget.

    The work runs as its own task, shielded from the timeout's cancel: a deduction is followed by its
    ledger row (see credits._apply_charge) and a cancel between the two would lose that row. On timeout we
    warn and return the result; the task keeps running to completion."""
    task = asyncio.ensure_future(_charge_and_record(db, claims, tool_name, tags, args, result, error, started))
    _background.add(task)
    task.add_done_callback(_background.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), _CHARGE_TIMEOUT_SECONDS)
    except Exception as exc:  # incl. TimeoutError: a billing or logging hiccup never costs the caller their result
        logger.warning("MCP charge/activity log did not finish for %s: %r", tool_name, exc)


class BirdyAccessMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        reset_outcome()
        start_activity()
        started = time.monotonic()
        token = get_access_token()
        claims = token.claims if token else {}
        tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
        # Only the internal orchestrator/login token is exempt; any other type (mcp, missing,
        # refresh, ...) is treated as external.
        external = claims.get("type") != "access"
        args = getattr(context.message, "arguments", None) or {}
        db = get_db(get_shared_mongo_client()) if external else None

        try:
            if external and not token_scopes(claims) & {READ_SCOPE, WRITE_SCOPE}:  # a write token also reads
                # A signed token with no Birdy scope (e.g. some other short-lived JWT) gets nothing, not even reads.
                set_outcome("refused", reason="no_scope")
                raise ToolError("This token does not grant access to Birdy. Reconnect Birdy to continue.")

            if "write" in (tool.tags or set()) and WRITE_SCOPE not in token_scopes(claims):
                set_outcome("refused", reason="view_only")
                raise ToolError(
                    'This Birdy connection is view-only. Reconnect with "View and make changes" to do this.'
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
                        "to keep using Birdy here."
                    )
                if not await mcp_billing.has_credits(db, claims.get("sub")):
                    set_outcome("refused", reason="out_of_credits")
                    raise ToolError(mcp_billing.OUT_OF_CREDITS_MESSAGE)
                await _enforce_rate_limit(db, claims.get("sub"))

            result = await call_next(context)
        except Exception as exc:
            if external:
                await _finish_call(db, claims, context.message.name, tool.tags or set(), args, None, exc, started)
            raise
        if external:
            await _finish_call(db, claims, context.message.name, tool.tags or set(), args, result, None, started)
        return result
