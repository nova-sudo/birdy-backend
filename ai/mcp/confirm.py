"""Server-enforced confirm codes for MCP tools that change live ads.

A live tool called without a code returns a preview plus a one-time code. Only
a second call carrying that code, the same user, the same tool and the exact
same arguments executes. The model cannot skip the step: nothing runs without a
code the server issued.

A tool also passes `state`: a fingerprint of the object the preview showed (a
draft's content, a report's channel and blocks). The owner said yes to that
preview, so if the object changed before the confirm call the code is refused
and nothing runs.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from ai.mcp.outcome import current_activity_id, set_outcome
from core.database import DB_NAME

CONFIRM_TTL_SECONDS = 600
COLLECTION = "mcp_confirm_codes"

INVALID_CODE_ERROR = (
    "This confirmation code is invalid, expired, or for different arguments. "
    "Nothing was changed. Call again without confirm_code to get a fresh preview."
)
STATE_CHANGED_ERROR = (
    "This changed since the preview, so nothing was changed. Call again without "
    "confirm_code to get a fresh preview and ask the user again."
)
NEXT_STEP = (
    "Show this preview to the user. If they agree, call this tool again with "
    "the same arguments plus confirm_code."
)


def _now() -> datetime:
    """The one place this module reads the clock (tests patch it)."""
    return datetime.utcnow()


def _hash(value) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _args_hash(args: dict) -> str:
    return _hash({k: v for k, v in (args or {}).items() if k != "confirm_code"})


def _state_hash(state) -> str | None:
    return None if state is None else _hash(state)


async def create_confirm_code_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db[COLLECTION].create_index("expires_at", expireAfterSeconds=0)
    await db[COLLECTION].create_index("code", unique=True)


async def issue_code(
    db, user_id: str, tool: str, args: dict, state=None, activity_id: str | None = None
) -> str:
    code = secrets.token_urlsafe(6)
    now = _now()
    await db[COLLECTION].insert_one(
        {
            "code": code,
            "user_id": user_id,
            "tool": tool,
            "args_hash": _args_hash(args),
            "state_hash": _state_hash(state),
            "activity_id": activity_id,
            "created_at": now,
            "expires_at": now + timedelta(seconds=CONFIRM_TTL_SECONDS),
        }
    )
    return code


async def _burn(db, user_id: str, tool: str, args: dict, code: str) -> dict | None:
    """Atomically delete and return the live code matching user, tool and args."""
    if not code:
        return None
    return await db[COLLECTION].find_one_and_delete(
        {
            "code": code,
            "user_id": user_id,
            "tool": tool,
            "args_hash": _args_hash(args),
            # Checked here as well as by the TTL index, which only sweeps about once a minute.
            "expires_at": {"$gt": _now()},
        }
    )


async def consume_code(db, user_id: str, tool: str, args: dict, code: str, state=None) -> bool:
    """Atomically burn the code. True only for a live code matching user, tool, args and state."""
    doc = await _burn(db, user_id, tool, args, code)
    return doc is not None and doc.get("state_hash") == _state_hash(state)


async def two_step(
    db,
    user_id: str,
    tool: str,
    args: dict,
    confirm_code: str | None,
    preview: Callable[[], Awaitable[dict]],
    execute: Callable[[], Awaitable[dict]],
    *,
    state=None,
) -> dict:
    """`state` fingerprints what the preview shows; recompute it from the freshly
    loaded object on both calls. Use stable content fields, never timestamps."""
    if not confirm_code:
        set_outcome("preview")
        return {
            "needs_confirmation": True,
            "preview": await preview(),
            "confirm_code": await issue_code(db, user_id, tool, args, state, activity_id=current_activity_id()),
            "expires_in_seconds": CONFIRM_TTL_SECONDS,
            "next": NEXT_STEP,
        }
    doc = await _burn(db, user_id, tool, args, confirm_code)
    if doc is None:
        set_outcome("bad_code")
        return {"error": INVALID_CODE_ERROR}
    if doc.get("state_hash") != _state_hash(state):
        set_outcome("state_changed")
        return {"error": STATE_CHANGED_ERROR}
    result = await execute()
    # Set only once execute() returns: a call that raised is free, whatever the code did.
    set_outcome("live_executed", confirmed_after=doc.get("activity_id"))
    return result
