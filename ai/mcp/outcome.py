"""What happened on the current MCP tool call, for the access middleware to read afterwards.

Tools and gates record the outcome (`two_step` for confirm-code tools, the middleware for
refusals); the middleware resets it at the start of every call. Kinds: `preview`,
`live_executed`, `bad_code`, `state_changed`, `refused`, `chat_billed`. Extras may carry `reason` (refusals)
or `confirmed_after` (a live_executed call: the activity id of the preview that issued the code).

`chat_billed` (CHAT_BILLED): the tool ran a Birdy chat reply, which billed itself once like a web chat
reply (ai/mcp/team_mcp.py). The middleware then adds no per-call MCP charge; extras `credits` (what the
reply cost) and `link` (its Birdy page) fill the sign-off line instead.

The middleware also pre-allocates this call's activity id, so a preview can stamp it on its
confirm code before the activity row exists.
"""

import json
import uuid
from contextvars import ContextVar, Token

CHAT_BILLED = "chat_billed"

call_outcome: ContextVar[dict | None] = ContextVar("mcp_call_outcome", default=None)


current_activity: ContextVar[str | None] = ContextVar("mcp_current_activity", default=None)


def start_activity() -> str:
    """Allocate this call's activity id (the middleware calls it at the start of every call)."""
    activity_id = uuid.uuid4().hex
    current_activity.set(activity_id)
    return activity_id


def current_activity_id() -> str | None:
    return current_activity.get()


def set_outcome(kind: str, **extra) -> None:
    call_outcome.set({"kind": kind, **extra})


def get_outcome() -> dict | None:
    return call_outcome.get()


def reset_outcome() -> Token:
    return call_outcome.set(None)


def is_error_result(value) -> bool:
    """A tool result that reports failure: a top-level `error` key, or `ok` set to False."""
    return isinstance(value, dict) and ("error" in value or value.get("ok") is False)


def response_chars(tool_result) -> int:
    """Size of what the client receives: the structured JSON when present, else the text blocks. Never both."""
    structured = getattr(tool_result, "structured_content", None)
    if structured is not None:
        return len(json.dumps(structured))
    return sum(len(getattr(block, "text", "") or "") for block in getattr(tool_result, "content", None) or [])
