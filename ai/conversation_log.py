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
    agent_id: str | None = None,
    card_id: str | None = None,
    agent_changes: list | None = None,
) -> None:
    """Append a single conversation message. Best-effort; never raises.

    `agent_id` / `card_id` tie a turn to the agent chat it came from (the
    stock card, Teach Birdy and agent setup chats) and `agent_changes` keeps
    what that turn changed, so the chat can be shown again after a reload.
    Each is stored only when set."""
    extra = {k: v for k, v in (("agent_id", agent_id), ("card_id", card_id),
                               ("agent_changes", agent_changes)) if v}
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
            **extra,
        })
    except Exception as e:
        # A durable-log failure is never worth failing a user's live chat over.
        logger.warning(f"conversation_log insert failed (non-fatal): {e}")


async def tag_latest_user_turn(
    db,
    *,
    session_id: str,
    user_id: str,
    client_group_id: str,
) -> None:
    """
    Attach a client to the newest untagged user turn in a session.

    The user turn is archived BEFORE the tool loop runs (so a crash mid-answer
    still archives the question), but which client the question was about is
    only known AFTER the loop — it is inferred from the group ids the model
    actually passed to its tools. Best-effort, like every write here.
    """
    try:
        await db[_COLLECTION].find_one_and_update(
            {
                "session_id": session_id,
                "user_id": user_id,
                "role": "user",
                "client_group_id": None,
            },
            {"$set": {"client_group_id": client_group_id}},
            sort=[("created_at", -1)],
        )
    except Exception as e:
        logger.warning(f"conversation_log tag failed (non-fatal): {e}")


async def _transcript(db, user_id: str, match: dict) -> dict:
    """The latest session among this user's turns matching `match`, oldest first. Form answers
    ([UI_RESPONSE]) are protocol, not conversation, and are left out."""
    q = {"user_id": user_id, **match}
    latest = await db[_COLLECTION].find_one(q, {"session_id": 1}, sort=[("created_at", -1)])
    if latest is None:
        return {"session_id": None, "messages": []}
    session_id = latest["session_id"]
    rows = await db[_COLLECTION].find(
        {**q, "session_id": session_id},
        {"role": 1, "content": 1, "agent_changes": 1, "created_at": 1, "_id": 0},
    ).sort("created_at", 1).to_list(length=None)
    messages = []
    for r in rows:
        content = r.get("content") or ""
        if content.lstrip().startswith("[UI_RESPONSE]"):
            continue
        at = r.get("created_at")
        messages.append({
            "role": r.get("role"),
            "content": content,
            "agent_changes": r.get("agent_changes") or [],
            "created_at": at.isoformat() if hasattr(at, "isoformat") else at,
        })
    return {"session_id": session_id, "messages": messages}


async def card_transcript(db, user_id: str, card_id: str) -> dict:
    """The latest chat about one stock card, for seeding the card's chat after a reload."""
    return await _transcript(db, user_id, {"card_id": card_id})


async def setup_transcript(db, user_id: str, agent_id: str) -> dict:
    """The latest setup chat for one custom agent, so a draft opened later (another tab, another
    day) shows where the setup got to, with its inline blocks (`agent_changes`)."""
    return await _transcript(db, user_id, {"agent_id": agent_id, "page": "agent_setup"})


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
        [("user_id", 1), ("card_id", 1), ("created_at", 1)],
        name="idx_convlog_user_card_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("user_id", 1), ("agent_id", 1), ("created_at", 1)],
        name="idx_convlog_user_agent_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("category", 1), ("created_at", -1)],
        name="idx_convlog_category_created", background=True,
    )
    await db[_COLLECTION].create_index(
        [("role", 1), ("created_at", -1)],
        name="idx_convlog_role_created", background=True,
    )
