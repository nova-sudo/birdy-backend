"""
ai/session_store.py
--------------------
Persistent (Mongo-backed) chat session store with sliding TTL expiry.

Was previously an in-process dict — broken on this deployment. Vercel's
Lambda-style Python runtime does not guarantee the same worker handles two
requests that are even a few seconds apart (see routers/slack_events.py's
own comment on this), and a Slack conversation routinely has minutes between
turns. An in-memory session_id lookup missing on a cold/different worker
silently fell through to create_session() and returned a BRAND NEW, EMPTY
history — wiping the system prompt (including every anti-hallucination
rule) and every prior tool-call result with no error, no log distinguishing
"resumed" from "lost", and no signal to the caller. The model then had to
answer follow-up questions ("what's her cost per won opportunity?") with no
real grounding, which is what produced fabricated numbers under user
pushback. services/slack_interaction_store.py already documents this exact
class of bug and was deliberately built Mongo-backed to avoid it — this
module follows that same, proven pattern instead of the one that broke.
"""

import uuid
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

SESSION_TTL_HOURS = 1  # 1 hour of inactivity, matches the old in-memory TTL
MAX_MESSAGES = 50  # keep last N messages to avoid unbounded growth

_COLLECTION = "ai_chat_sessions"


def _expiry(now: datetime) -> datetime:
    return now + timedelta(hours=SESSION_TTL_HOURS)


async def create_session(db, user_id: str) -> str:
    """Create a new session and return its ID."""
    session_id = f"chat_{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow()
    await db[_COLLECTION].insert_one({
        "_id": session_id,
        "user_id": user_id,
        "messages": [],
        "last_active": now,
        "expires_at": _expiry(now),
    })
    return session_id


async def rehydrate_from_archive(db, session_id: str, user_id: str) -> list:
    """
    Rebuild working memory for an expired session from the durable archive.

    Working memory expires after an hour, but ai_conversation_log keeps every
    turn forever. Without this, reopening yesterday's conversation showed the
    user their old transcript while the model saw an empty history — it would
    answer follow-ups with no grounding at all, which is the failure mode this
    module's docstring was written about.

    Only user/assistant prose is archived, so what comes back has no tool_calls
    and needs no sanitising for orphaned tool groups.
    """
    from ai.conversation_log import _COLLECTION as _LOG_COLLECTION

    rows = await db[_LOG_COLLECTION].find(
        {"session_id": session_id, "user_id": user_id, "role": {"$in": ["user", "assistant"]}},
        {"role": 1, "content": 1, "_id": 0},
    ).sort("created_at", -1).limit(MAX_MESSAGES).to_list(length=MAX_MESSAGES)

    # Sorted newest-first above so the limit keeps the most RECENT turns, then
    # flipped back into chronological order for the model.
    return [{"role": r["role"], "content": r.get("content") or ""} for r in reversed(rows)]


async def get_or_create(db, session_id: str | None, user_id: str) -> tuple[str, list]:
    """
    Return (session_id, messages) for an existing session, or create a new one.
    Validates that the session belongs to the user. Sliding TTL: every read
    pushes expires_at forward another SESSION_TTL_HOURS.

    An expired session is resumed from the archive rather than silently
    restarted — see rehydrate_from_archive.
    """
    if session_id:
        now = datetime.utcnow()
        doc = await db[_COLLECTION].find_one_and_update(
            {"_id": session_id, "user_id": user_id},
            {"$set": {"last_active": now, "expires_at": _expiry(now)}},
        )
        if doc:
            return session_id, doc.get("messages", [])

        # Not in working memory: expired, or this worker has never seen it.
        # The archive is the source of truth for what was actually said, so try
        # to resume under the SAME id before giving up and starting over.
        archived = await rehydrate_from_archive(db, session_id, user_id)
        if archived:
            await db[_COLLECTION].update_one(
                {"_id": session_id},
                {"$set": {
                    "user_id": user_id,
                    "messages": archived,
                    "last_active": now,
                    "expires_at": _expiry(now),
                }},
                upsert=True,
            )
            logger.info(
                "Resumed session %s for %s from archive (%d messages)",
                session_id, user_id, len(archived),
            )
            return session_id, archived

        # Nothing archived either — the id is unknown or belongs to someone
        # else. Indistinguishable from here, so start fresh.
        logger.info(f"Session {session_id} not found/expired for user {user_id}, creating new")

    new_id = await create_session(db, user_id)
    return new_id, []


def sanitize_history(messages: list) -> list:
    """
    Enforce provider-compatible role ordering.

    Providers (esp. Mistral) require:
      - system is first and appears at most once
      - `tool` messages immediately follow an `assistant` that has tool_calls
      - every `tool_call` must have a matching tool response by id

    This function walks the message list and drops any `tool` message or
    `assistant(tool_calls)` message that would be orphaned. It preserves
    system + user + clean assistant messages. Pure/synchronous — no DB
    access needed.
    """
    if not messages:
        return messages

    cleaned = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            # Only keep the first system message
            if not any(m.get("role") == "system" for m in cleaned):
                cleaned.append(msg)
            continue

        if role == "tool":
            # A tool message must follow an assistant(tool_calls) whose tool_call_ids
            # include this tool_call_id. Check the tail of cleaned.
            tc_id = msg.get("tool_call_id")
            if not tc_id:
                continue  # orphan
            # Walk back through cleaned to find the matching assistant
            matched = False
            for prev in reversed(cleaned):
                if prev.get("role") == "assistant" and prev.get("tool_calls"):
                    ids = {tc.get("id") for tc in (prev.get("tool_calls") or [])}
                    if tc_id in ids:
                        matched = True
                    break
                # If we hit any other role, stop walking
                if prev.get("role") in ("user", "system"):
                    break
            if matched:
                cleaned.append(msg)
            # else: drop the orphan tool message
            continue

        if role == "assistant" and msg.get("tool_calls"):
            # Keep it; we'll check after the whole pass whether its tool calls
            # were answered. Defer.
            cleaned.append(msg)
            continue

        # user, plain assistant
        cleaned.append(msg)

    # Second pass: drop assistant(tool_calls) whose tool responses were lost
    # (e.g., the tool response was dropped above, or the group was mid-trim).
    final = []
    for i, msg in enumerate(cleaned):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            needed_ids = {tc.get("id") for tc in (msg.get("tool_calls") or [])}
            # Collect tool responses that appear immediately after until the next
            # non-tool message
            j = i + 1
            got_ids = set()
            while j < len(cleaned) and cleaned[j].get("role") == "tool":
                got_ids.add(cleaned[j].get("tool_call_id"))
                j += 1
            if not needed_ids.issubset(got_ids):
                # Missing tool responses — drop this assistant AND any partial tools
                logger.warning(
                    "Dropping assistant with unmatched tool_calls (needed %s, got %s)",
                    needed_ids, got_ids,
                )
                continue
        final.append(msg)

    return final


async def save_messages(db, session_id: str, messages: list) -> None:
    """Update the stored messages for a session (trim safely to MAX_MESSAGES)."""
    # First, keep a reference to the system prompt (index 0 if present)
    system_msg = None
    if messages and messages[0].get("role") == "system":
        system_msg = messages[0]
        rest = messages[1:]
    else:
        rest = list(messages)

    # Trim the tail to MAX_MESSAGES, but never cut in the middle of a tool
    # call group. We walk forward from the tail start, dropping any leading
    # `tool` messages or `assistant(tool_calls)` messages whose responses
    # would be orphaned.
    if len(rest) > MAX_MESSAGES:
        tail = rest[-MAX_MESSAGES:]
        # Skip leading orphans until we find a safe start (user, or assistant
        # without tool_calls, or an assistant with tool_calls whose tool
        # responses are all present in the tail)
        start = 0
        while start < len(tail):
            m = tail[start]
            r = m.get("role")
            if r == "tool":
                start += 1
                continue
            if r == "assistant" and m.get("tool_calls"):
                needed = {tc.get("id") for tc in (m.get("tool_calls") or [])}
                # Scan forward until the next user/system/plain-assistant
                got = set()
                k = start + 1
                while k < len(tail) and tail[k].get("role") == "tool":
                    got.add(tail[k].get("tool_call_id"))
                    k += 1
                if needed.issubset(got):
                    break  # safe — all responses present
                start += 1  # missing responses; skip this assistant
                continue
            # user or plain assistant — safe
            break
        rest = tail[start:]

    combined = ([system_msg] if system_msg else []) + rest
    # Final sanitize pass to guarantee role-ordering invariants
    combined = sanitize_history(combined)

    now = datetime.utcnow()
    await db[_COLLECTION].update_one(
        {"_id": session_id},
        {"$set": {
            "messages": combined,
            "last_active": now,
            "expires_at": _expiry(now),
        }},
    )


async def clear_session(db, session_id: str) -> None:
    """Delete a session."""
    await db[_COLLECTION].delete_one({"_id": session_id})


async def create_ai_session_indexes(mongo_client):
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db[_COLLECTION].create_index("expires_at", expireAfterSeconds=0)
    await db[_COLLECTION].create_index("user_id")
