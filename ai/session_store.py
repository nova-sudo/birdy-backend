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


def save_messages(session_id: str, messages: list):
    """Update the stored messages for a session (trim to MAX_MESSAGES)."""
    if session_id not in _sessions:
        return
    # Always keep the system message (index 0) + last N messages
    if len(messages) > MAX_MESSAGES + 1:
        messages = [messages[0]] + messages[-(MAX_MESSAGES):]
    _sessions[session_id]["messages"] = messages
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
