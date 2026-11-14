"""Server-enforced confirm codes for MCP tools that change live ads.

A live tool called without a code returns a preview plus a one-time code. Only
a second call carrying that code, the same user, the same tool and the exact
same arguments executes. The model cannot skip the step: nothing runs without a
code the server issued.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from core.database import DB_NAME

CONFIRM_TTL_SECONDS = 600
COLLECTION = "mcp_confirm_codes"

INVALID_CODE_ERROR = (
    "This confirmation code is invalid, expired, or for different arguments. "
    "Nothing was changed. Call again without confirm_code to get a fresh preview."
)
NEXT_STEP = (
    "Show this preview to the user. If they agree, call this tool again with "
    "the same arguments plus confirm_code."
)


def _now() -> datetime:
    """The one place this module reads the clock (tests patch it)."""
    return datetime.utcnow()


def _args_hash(args: dict) -> str:
    clean = {k: v for k, v in (args or {}).items() if k != "confirm_code"}
    canonical = json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def create_confirm_code_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db[COLLECTION].create_index("expires_at", expireAfterSeconds=0)
    await db[COLLECTION].create_index("code", unique=True)


async def issue_code(db, user_id: str, tool: str, args: dict) -> str:
    code = secrets.token_urlsafe(6)
    now = _now()
    await db[COLLECTION].insert_one(
        {
            "code": code,
            "user_id": user_id,
            "tool": tool,
            "args_hash": _args_hash(args),
            "created_at": now,
            "expires_at": now + timedelta(seconds=CONFIRM_TTL_SECONDS),
        }
    )
    return code


async def consume_code(db, user_id: str, tool: str, args: dict, code: str) -> bool:
    """Atomically burn the code. True only for a live code matching user, tool and args."""
    if not code:
        return False
    doc = await db[COLLECTION].find_one_and_delete(
        {
            "code": code,
            "user_id": user_id,
            "tool": tool,
            "args_hash": _args_hash(args),
            # Checked here as well as by the TTL index, which only sweeps about once a minute.
            "expires_at": {"$gt": _now()},
        }
    )
    return doc is not None


async def two_step(
    db,
    user_id: str,
    tool: str,
    args: dict,
    confirm_code: str | None,
    preview: Callable[[], Awaitable[dict]],
    execute: Callable[[], Awaitable[dict]],
) -> dict:
    if not confirm_code:
        return {
            "needs_confirmation": True,
            "preview": await preview(),
            "confirm_code": await issue_code(db, user_id, tool, args),
            "expires_in_seconds": CONFIRM_TTL_SECONDS,
            "next": NEXT_STEP,
        }
    if not await consume_code(db, user_id, tool, args, confirm_code):
        return {"error": INVALID_CODE_ERROR}
    return await execute()
