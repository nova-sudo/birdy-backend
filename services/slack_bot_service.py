"""
services/slack_bot_service.py
--------------------------------
Slack bot installation (distinct from the existing, unrelated, unused
`integrations.slack.webhook_url` outbound Incoming-Webhook feature in
services/slack_service.py). One Slack workspace installation maps to exactly
one Birdy user account — whoever installs the bot connects it to their own
login once; everyone in that Slack workspace who talks to the bot gets that
account's data and uses that account's own BYOK AI credential.

Also owns the Slack-thread -> chat session_id mapping: ai/session_store.py's
get_or_create() only reuses a session_id if it's already a live key in its
in-process dict, so an invented string can't be handed to it directly — this
module remembers the *real* session_id run_chat() returned last time for a
given Slack thread.
"""

import logging
from datetime import datetime

from core.crypto import encrypt, decrypt
from core.database import DB_NAME

logger = logging.getLogger(__name__)


class SlackTeamAlreadyLinkedError(Exception):
    """Raised when a Slack workspace is already linked to a different Birdy account."""


async def create_slack_bot_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db["users"].create_index("integrations.slack_bot.team_id", unique=True, sparse=True)
    await db["slack_thread_sessions"].create_index("updated_at")


async def save_slack_bot_installation(
    db, user_id: str, team_id: str, bot_token: str, bot_user_id: str,
    team_name: str | None = None, scope: str | None = None,
) -> dict:
    """Saves (or, for a reinstall by the SAME user, overwrites) a Slack bot installation.

    Refuses to silently re-point an already-linked workspace at a different
    Birdy account — that would leak one customer's Slack conversations into
    another's data.
    """
    existing_owner = await get_user_id_by_team_id(db, team_id)
    if existing_owner is not None and existing_owner != user_id:
        raise SlackTeamAlreadyLinkedError(
            f"This Slack workspace is already connected to a different Birdy account."
        )

    now = datetime.utcnow()
    doc = {
        "team_id": team_id,
        "team_name": team_name,
        "bot_user_id": bot_user_id,
        "bot_token_encrypted": encrypt(bot_token),
        "scope": scope,
        "installed_at": now,
        "updated_at": now,
    }
    await db["users"].update_one(
        {"user_id": user_id},
        {"$set": {"integrations.slack_bot": doc, "updated_at": now}},
        upsert=True,
    )
    logger.info(f"Saved Slack bot installation (team {team_id}) for user: {user_id}")
    return {
        "team_id": team_id,
        "team_name": team_name,
        "bot_user_id": bot_user_id,
        "installed_at": now.isoformat(),
    }


async def get_slack_bot_status(db, user_id: str) -> dict | None:
    user_doc = await db["users"].find_one(
        {"user_id": user_id},
        projection={"integrations.slack_bot": 1},
    )
    slack_bot = ((user_doc or {}).get("integrations") or {}).get("slack_bot")
    if not slack_bot:
        return None
    installed_at = slack_bot.get("installed_at")
    return {
        "team_id": slack_bot["team_id"],
        "team_name": slack_bot.get("team_name"),
        "bot_user_id": slack_bot["bot_user_id"],
        "installed_at": installed_at.isoformat() if installed_at else None,
        "notify_channel_id": slack_bot.get("notify_channel_id"),
        "notify_channel_name": slack_bot.get("notify_channel_name"),
    }


async def remove_slack_bot_installation(db, user_id: str) -> bool:
    result = await db["users"].update_one(
        {"user_id": user_id},
        {"$unset": {"integrations.slack_bot": ""}, "$set": {"updated_at": datetime.utcnow()}},
    )
    return result.matched_count > 0


async def get_user_id_by_team_id(db, team_id: str) -> str | None:
    """The critical inbound-event lookup: Slack events/interactions carry a
    team_id, not a Birdy user_id."""
    doc = await db["users"].find_one(
        {"integrations.slack_bot.team_id": team_id},
        projection={"user_id": 1},
    )
    return doc["user_id"] if doc else None


async def get_decrypted_bot_token_for_user(db, user_id: str) -> str | None:
    """Internal use only (routers/slack_events.py, routers/slack_interactions.py).
    Never expose the decrypted token through an API response."""
    user_doc = await db["users"].find_one(
        {"user_id": user_id},
        projection={"integrations.slack_bot.bot_token_encrypted": 1},
    )
    slack_bot = ((user_doc or {}).get("integrations") or {}).get("slack_bot")
    if not slack_bot:
        return None
    return decrypt(slack_bot["bot_token_encrypted"])


async def set_notify_channel(db, user_id: str, channel_id: str | None,
                             channel_name: str | None = None) -> bool:
    """
    Set (or clear, when channel_id is None) the channel Birdy posts suggestions
    to. Editable any time from settings. Returns False if the user has no Slack
    bot installed.
    """
    now = datetime.utcnow()
    if channel_id:
        update = {"$set": {
            "integrations.slack_bot.notify_channel_id": channel_id,
            "integrations.slack_bot.notify_channel_name": channel_name,
            "integrations.slack_bot.updated_at": now,
        }}
    else:
        update = {
            "$unset": {
                "integrations.slack_bot.notify_channel_id": "",
                "integrations.slack_bot.notify_channel_name": "",
            },
            "$set": {"integrations.slack_bot.updated_at": now},
        }
    result = await db["users"].update_one(
        {"user_id": user_id, "integrations.slack_bot": {"$exists": True}},
        update,
    )
    return result.matched_count > 0


async def get_notify_target(db, user_id: str) -> tuple[str | None, str | None]:
    """
    Return (decrypted_bot_token, channel_id) when the user has BOTH a Slack bot
    installed and a suggestion channel chosen, else (None, None). Used by the
    notifier to decide whether to post at all — no config → silent no-op.
    """
    user_doc = await db["users"].find_one(
        {"user_id": user_id},
        projection={
            "integrations.slack_bot.bot_token_encrypted": 1,
            "integrations.slack_bot.notify_channel_id": 1,
        },
    )
    slack_bot = ((user_doc or {}).get("integrations") or {}).get("slack_bot")
    if not slack_bot:
        return None, None
    channel_id = slack_bot.get("notify_channel_id")
    token_enc = slack_bot.get("bot_token_encrypted")
    if not channel_id or not token_enc:
        return None, None
    return decrypt(token_enc), channel_id


async def get_thread_session_id(db, team_id: str, channel: str, thread_key: str) -> str | None:
    doc = await db["slack_thread_sessions"].find_one({"_id": f"{team_id}:{channel}:{thread_key}"})
    return doc["session_id"] if doc else None


async def save_thread_session_id(
    db, team_id: str, channel: str, thread_key: str, user_id: str, session_id: str,
) -> None:
    await db["slack_thread_sessions"].update_one(
        {"_id": f"{team_id}:{channel}:{thread_key}"},
        {"$set": {"user_id": user_id, "session_id": session_id, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
