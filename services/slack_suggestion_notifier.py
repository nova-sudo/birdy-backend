"""
services/slack_suggestion_notifier.py
-------------------------------------
Posts Birdy suggestions to a user's chosen Slack channel — one message per
suggestion (a text section + an actions block with "Do it for me" / "Ignore").
Each button carries the suggestion id in its `value`; clicks are handled in
routers/slack_interactions.py.

Multi-tenant + fail-safe: the channel + bot token are resolved per Birdy user
(get_notify_target). If the user hasn't connected Slack OR hasn't chosen a
channel, this is a silent no-op — so it's safe to call after every pass,
including in tests (which have neither).
"""

import logging

from ai.suggestions import store
from services.slack_bot_service import get_notify_target

logger = logging.getLogger(__name__)

_SEV_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟠", "OPPORTUNITY": "🟢"}
_MAX_SECTION_CHARS = 2900  # Slack section text cap (see slack_block_formatter)


def build_suggestion_blocks(doc: dict) -> tuple[list[dict], str]:
    """Return (blocks, fallback_text) for one suggestion. Two blocks only."""
    title = doc.get("title") or "Suggestion"
    desc = doc.get("description") or ""
    client = doc.get("client_name") or "Client"
    platform = doc.get("platform") or "Meta Ads"
    severity = doc.get("severity") or ""
    stats = doc.get("stats") or []

    emoji = _SEV_EMOJI.get(severity, "•")
    lines = [f"{emoji} *{title}*", f"{client} · {platform}" + (f" · _{severity}_" if severity else "")]
    if desc:
        lines += ["", desc]
    if stats:
        lines.append("   ".join(f"*{s.get('label')}:* {s.get('value')}" for s in stats))
    section = {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:_MAX_SECTION_CHARS]}}

    elements = []
    action = doc.get("action")
    if action:
        verb = "pause" if action.get("type") == "pause_ads" else "update"
        elements.append({
            "type": "button",
            "action_id": "suggestion_apply",
            "style": "primary",
            "text": {"type": "plain_text", "text": "Do it for me"},
            "value": doc["id"],
            # Native confirm dialog — a pause is real money.
            "confirm": {
                "title": {"type": "plain_text", "text": "Do it for me?"},
                "text": {"type": "mrkdwn", "text": f"Birdy will {verb} the ad(s) for *{client}*."},
                "confirm": {"type": "plain_text", "text": "Do it"},
                "deny": {"type": "plain_text", "text": "Cancel"},
            },
        })
    elements.append({
        "type": "button",
        "action_id": "suggestion_ignore",
        "text": {"type": "plain_text", "text": "Ignore"},
        "value": doc["id"],
    })
    actions_block = {"type": "actions", "block_id": f"suggestion|{doc['id']}", "elements": elements}

    return [section, actions_block], f"Birdy suggestion: {title} — {client}"


async def post_suggestion(db, user_id: str, doc: dict, *,
                          bot_token: str | None = None, channel_id: str | None = None) -> str | None:
    """Post one suggestion. Returns the message ts, or None if not posted."""
    if not bot_token or not channel_id:
        bot_token, channel_id = await get_notify_target(db, user_id)
    if not bot_token or not channel_id:
        return None

    blocks, fallback = build_suggestion_blocks(doc)
    from slack_sdk.web.async_client import AsyncWebClient
    client = AsyncWebClient(token=bot_token)
    try:
        resp = await client.chat_postMessage(channel=channel_id, text=fallback, blocks=blocks)
    except Exception as e:
        logger.warning("slack notifier: post failed for suggestion %s: %s", doc.get("id"), e)
        return None

    ts = resp.get("ts")
    if ts:
        try:
            await store.set_slack_message(db, user_id, doc["id"], channel_id, ts)
        except Exception:
            pass  # non-critical
    return ts


async def post_new_suggestions(db, user_id: str, docs: list[dict]) -> int:
    """Post a batch of suggestions (one message each). Returns count posted."""
    if not docs:
        return 0
    bot_token, channel_id = await get_notify_target(db, user_id)
    if not bot_token or not channel_id:
        return 0  # Slack not configured for this user → silent no-op

    posted = 0
    for doc in docs:
        if await post_suggestion(db, user_id, doc, bot_token=bot_token, channel_id=channel_id):
            posted += 1
    return posted


async def update_suggestions(db, user_id: str, docs: list[dict]) -> int:
    """
    Re-render already-posted Slack messages so they match the current suggestion
    content (called when a same-window refresh changes the copy/stats). Each doc
    must carry the slack_ts + slack_channel set when it was first posted.
    """
    if not docs:
        return 0
    bot_token, _ = await get_notify_target(db, user_id)
    if not bot_token:
        return 0

    from slack_sdk.web.async_client import AsyncWebClient
    client = AsyncWebClient(token=bot_token)
    updated = 0
    for doc in docs:
        ts = doc.get("slack_ts")
        channel = doc.get("slack_channel")
        if not ts or not channel:
            continue
        blocks, fallback = build_suggestion_blocks(doc)
        try:
            await client.chat_update(channel=channel, ts=ts, text=fallback, blocks=blocks)
            updated += 1
        except Exception as e:
            logger.warning("slack notifier: update failed for suggestion %s: %s", doc.get("id"), e)
    return updated
