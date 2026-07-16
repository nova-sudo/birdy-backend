"""
services/slack_interaction_store.py
--------------------------------------
Pending state for in-progress :::ui forms sent to Slack. Deliberately a Mongo
collection, not an in-process dict like ai/session_store.py — a Slack
interaction (a button click, a modal submit) can land on a different worker
process minutes or hours after the message was posted, so nothing in-memory
would still be around to receive it.

For an inline form (services/slack_block_formatter.py's "inline" mode),
Slack fires one block_actions payload per individual click (radio pick,
checkbox toggle, select) — there's no aggregate "form state" for a posted
message the way a Modal's view_submission gives you everything at once. This
store accumulates those partial answers until the Submit button is clicked.
"""

from datetime import datetime, timedelta

_TTL_HOURS = 24


async def create_pending_interaction(
    db, iid: str, *, team_id: str, channel_id: str, thread_ts: str | None,
    slack_user_id: str, birdy_user_id: str, session_id: str,
    fields: list[dict], mode: str,
) -> None:
    now = datetime.utcnow()
    await db["slack_ui_interactions"].update_one(
        {"_id": iid},
        {"$set": {
            "team_id": team_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "slack_user_id": slack_user_id,
            "birdy_user_id": birdy_user_id,
            "session_id": session_id,
            "fields": fields,
            "mode": mode,
            "answers": {},
            "created_at": now,
            "expires_at": now + timedelta(hours=_TTL_HOURS),
        }},
        upsert=True,
    )


async def get_pending_interaction(db, iid: str) -> dict | None:
    return await db["slack_ui_interactions"].find_one({"_id": iid})


async def record_field_answer(db, iid: str, field_id: str, value) -> None:
    """Merges one field's answer into the pending doc (the inline, one-click-at-a-time path)."""
    await db["slack_ui_interactions"].update_one(
        {"_id": iid},
        {"$set": {f"answers.{field_id}": value}},
    )


async def record_answers(db, iid: str, answers: dict) -> None:
    """Merges a full set of answers at once (the modal view_submission path)."""
    await db["slack_ui_interactions"].update_one(
        {"_id": iid},
        {"$set": {f"answers.{k}": v for k, v in answers.items()}},
    )


async def delete_pending_interaction(db, iid: str) -> None:
    await db["slack_ui_interactions"].delete_one({"_id": iid})


async def create_slack_ui_interaction_indexes(mongo_client):
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db["slack_ui_interactions"].create_index("expires_at", expireAfterSeconds=0)
