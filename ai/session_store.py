"""In-memory chat session store with auto-expiry."""

import time
import uuid
import logging

logger = logging.getLogger(__name__)

# session_id -> {"user_id": str, "messages": list, "last_active": float}
_sessions: dict[str, dict] = {}

SESSION_TTL = 60 * 60  # 1 hour of inactivity
MAX_MESSAGES = 50  # keep last N messages to avoid unbounded growth


def create_session(user_id: str) -> str:
    """Create a new session and return its ID."""
    _gc()
    session_id = f"chat_{uuid.uuid4().hex[:12]}"
    _sessions[session_id] = {
        "user_id": user_id,
        "messages": [],
        "last_active": time.time(),
    }
    return session_id


def get_or_create(session_id: str | None, user_id: str) -> tuple[str, list]:
    """
    Return (session_id, messages) for an existing session, or create a new one.
    Validates that the session belongs to the user.
    """
    _gc()

    if session_id and session_id in _sessions:
        session = _sessions[session_id]
        if session["user_id"] == user_id:
            session["last_active"] = time.time()
            return session_id, session["messages"]
        # Wrong user — ignore and create new
        logger.warning(f"Session {session_id} belongs to different user, creating new")

    new_id = create_session(user_id)
    return new_id, _sessions[new_id]["messages"]


def sanitize_history(messages: list) -> list:
    """
    Enforce provider-compatible role ordering.

    Providers (esp. Mistral) require:
      - system is first and appears at most once
      - `tool` messages immediately follow an `assistant` that has tool_calls
      - every `tool_call` must have a matching tool response by id

    This function walks the message list and drops any `tool` message or
    `assistant(tool_calls)` message that would be orphaned. It preserves
    system + user + clean assistant messages.
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


def save_messages(session_id: str, messages: list):
    """Update the stored messages for a session (trim safely to MAX_MESSAGES)."""
    if session_id not in _sessions:
        return

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

    _sessions[session_id]["messages"] = combined
    _sessions[session_id]["last_active"] = time.time()


def clear_session(session_id: str):
    """Delete a session."""
    _sessions.pop(session_id, None)


def _gc():
    """Remove expired sessions."""
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s["last_active"] > SESSION_TTL]
    for sid in expired:
        del _sessions[sid]
    if expired:
        logger.info(f"Cleaned up {len(expired)} expired chat sessions")
