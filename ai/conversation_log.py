"""
ai/conversation_log.py
-----------------------
Durable, append-only archive of every Birdy AI turn — web chat AND the Slack
bot. This is the permanent record the Admin console reads for conversation
review and AI-query analytics.

Deliberately distinct from ai/session_store.py, which is *working memory*:
that store TTL-expires after 1 hour of inactivity and is capped at 50
messages (see ai/session_store.py:28,29), so it can never answer "what has
this user been asking Birdy over the last month?". This log has NO TTL —
retention is intentional per the Admin plan.

Contract:
  - One document per message in the `ai_conversation_log` collection.
  - Writes are BEST-EFFORT: a logging failure must never break a live chat,
    so every write is wrapped and only warns on failure.
  - Both channels funnel through ai/orchestrator.run_chat, so logging lives
    there once and captures everything.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_COLLECTION = "ai_conversation_log"

# Valid message sources — kept here so the analytics layer can rely on them.
SOURCE_BIRDY = "birdy"
SOURCE_SLACK = "slack"


async def log_message(
    db,
    *,
    user_id: str,
    session_id: str,
    source: str,
    role: str,
    content: str,
    page: str | None = None,
    tools_used: list | None = None,
    category: str | None = None,
    client_group_id: str | None = None,
) -> None:
    """Append a single conversation message. Best-effort; never raises."""
    try:
        await db[_COLLECTION].insert_one({
            "user_id": user_id,
            "session_id": session_id,
            "source": source,            # "birdy" | "slack"
            "page": page,
            # Which client the thread is about, when it was opened from a
            # client's own page. Without this the Client Detail thread list
            # would show every conversation the user has ever had, including
            # other clients'.
            "client_group_id": client_group_id,
            "role": role,                # "user" | "assistant"
            "content": content,
            "tools_used": tools_used or [],
            "category": category,        # only set on user turns
            "created_at": datetime.utcnow(),
        })
    except Exception as e:
        # A durable-log failure is never worth failing a user's live chat over.
        logger.warning(f"conversation_log insert failed (non-fatal): {e}")


async def create_conversation_log_indexes(mongo_client) -> None:
    """Register indexes. NOTE: intentionally no TTL index — this is an archive."""
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db[_COLLECTION].create_index(
        [("user_id", 1), ("created_at", -1)],
        name="idx_convlog_user_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("session_id", 1), ("created_at", 1)],
        name="idx_convlog_session_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("created_at", -1)],
        name="idx_convlog_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("user_id", 1), ("client_group_id", 1), ("created_at", -1)],
        name="idx_convlog_user_client_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("category", 1), ("created_at", -1)],
        name="idx_convlog_category_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("role", 1), ("created_at", -1)],
        name="idx_convlog_role_created", background=True,
    )
