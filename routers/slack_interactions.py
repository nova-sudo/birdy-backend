"""
routers/slack_interactions.py
--------------------------------
Slack's Interactivity & Shortcuts request URL — handles clicks on the
inline :::ui elements and Modal submissions built by
services/slack_block_formatter.py.

Payloads arrive as application/x-www-form-urlencoded with a single `payload`
field containing a JSON string (NOT a JSON body like the Events API) —
must read via request.form(), not request.json().

Like routers/slack_events.py, every path here (including the final
run_chat() call once a form is complete) runs synchronously rather than via
a background task: on Vercel's Lambda-style runtime, an awaited call inside
a still-executing request handler is safe — the freeze-after-flush risk
that rules out BackgroundTasks/asyncio.create_task only applies to work
detached to continue *after* the HTTP response has already been sent. For a
non-submit field change (no LLM call) this responds in well under Slack's
3-second window regardless; a submit accepts a slower ack in exchange for
correctness, exactly like the events handler.
"""

import json
import logging

from fastapi import APIRouter, Request

from ai.orchestrator import run_chat
from ai.provider_factory import get_provider_for_user, NoAiCredentialError
from ai.tools.registry import registry
from ai.suggestions import actions
from core.database import get_db
from dependencies import get_mongo_client
from services.slack_bot_service import (
    get_decrypted_bot_token_for_user,
    save_thread_session_id,
    get_user_id_by_team_id,
)
from services.slack_block_formatter import build_blocks_from_reply, build_modal_view
from services.slack_interaction_store import (
    get_pending_interaction,
    record_field_answer,
    record_answers,
    delete_pending_interaction,
    create_pending_interaction,
)
from services.slack_signature import verify_slack_request

logger = logging.getLogger(__name__)

router = APIRouter()


def _extract_action_value(action: dict):
    action_type = action.get("type")
    if action_type in ("static_select", "radio_buttons"):
        return action.get("selected_option", {}).get("value")
    if action_type == "checkboxes":
        return [o["value"] for o in action.get("selected_options", [])]
    if action_type == "datepicker":
        return action.get("selected_date")
    return action.get("value")


async def _run_chat_and_reply(db, mongo_client, slack_client, pending: dict, answers: dict):
    """Reconstructs the [UI_RESPONSE] message, calls run_chat(), and posts the reply."""
    birdy_user_id = pending["birdy_user_id"]
    message = "[UI_RESPONSE] " + json.dumps(answers)

    try:
        provider = await get_provider_for_user(birdy_user_id, db)
    except NoAiCredentialError:
        await slack_client.chat_postMessage(
            channel=pending["channel_id"], thread_ts=pending["thread_ts"],
            text="This Birdy account's AI key was removed — ask the workspace owner to reconnect it.",
        )
        return

    from credits import is_blocked
    if await is_blocked(db, birdy_user_id):
        await slack_client.chat_postMessage(
            channel=pending["channel_id"], thread_ts=pending["thread_ts"],
            text="You're out of Birdy Credits, so I can't run this right now. Top up in the Birdy app under *Credits* to continue.",
        )
        return

    result = await run_chat(
        provider=provider, tool_registry=registry, db=db, user_id=birdy_user_id,
        message=message, session_id=pending["session_id"], mongo_client=mongo_client,
        source="slack",
    )

    blocks, fallback_text, ui_pending = build_blocks_from_reply(
        result["reply"], iid_prefix=result["session_id"]
    )
    for new_pending in ui_pending:
        await create_pending_interaction(
            db, new_pending["iid"],
            team_id=pending["team_id"], channel_id=pending["channel_id"], thread_ts=pending["thread_ts"],
            slack_user_id=pending["slack_user_id"], birdy_user_id=birdy_user_id,
            session_id=result["session_id"], fields=new_pending["fields"], mode=new_pending["mode"],
        )

    await slack_client.chat_postMessage(
        channel=pending["channel_id"], thread_ts=pending["thread_ts"], blocks=blocks, text=fallback_text,
    )
    await save_thread_session_id(
        db, pending["team_id"], pending["channel_id"], pending["thread_ts"],
        birdy_user_id, result["session_id"],
    )


async def _handle_block_actions(payload: dict, db, mongo_client) -> dict:
    action = (payload.get("actions") or [{}])[0]
    block_id = action.get("block_id", "")
    parts = block_id.split("|")
    if len(parts) != 3 or parts[0] != "ui":
        return {}
    _, iid, field_id = parts

    pending = await get_pending_interaction(db, iid)
    if not pending:
        return {}  # expired or already-submitted form

    if field_id not in ("__open_modal__", "__submit__"):
        # A single field's value changed — accumulate it. No Slack API call
        # needed here, so this path doesn't depend on the bot token at all.
        value = _extract_action_value(action)
        await record_field_answer(db, iid, field_id, value)
        return {}

    birdy_user_id = pending["birdy_user_id"]
    bot_token = await get_decrypted_bot_token_for_user(db, birdy_user_id)
    if not bot_token:
        return {}

    from slack_sdk.web.async_client import AsyncWebClient
    slack_client = AsyncWebClient(token=bot_token)

    if field_id == "__open_modal__":
        view = build_modal_view(pending["fields"], iid)
        trigger_id = payload.get("trigger_id")
        if trigger_id:
            await slack_client.views_open(trigger_id=trigger_id, view=view)
        return {}

    # field_id == "__submit__"
    required_ids = {f["id"] for f in pending["fields"] if f.get("required")}
    missing = required_ids - set(pending.get("answers", {}).keys())
    if missing:
        await slack_client.chat_postMessage(
            channel=pending["channel_id"], thread_ts=pending["thread_ts"],
            text=f"Please answer all required fields before submitting: {', '.join(missing)}",
        )
        return {}

    message_ref = payload.get("message") or {}
    await delete_pending_interaction(db, iid)  # prevent double-submit
    if message_ref.get("ts"):
        await slack_client.chat_update(
            channel=pending["channel_id"], ts=message_ref["ts"], text="✅ Submitted",
        )
    await _run_chat_and_reply(db, mongo_client, slack_client, pending, pending.get("answers", {}))
    return {}


async def _handle_view_submission(payload: dict, db, mongo_client) -> dict:
    view = payload["view"]
    iid = view.get("private_metadata", "")
    pending = await get_pending_interaction(db, iid)
    if not pending:
        return {}

    answers = {}
    for block_id, block_values in view.get("state", {}).get("values", {}).items():
        action = (block_values or {}).get("value", {})
        atype = action.get("type")
        if atype == "plain_text_input":
            answers[block_id] = action.get("value")
        elif atype == "number_input":
            answers[block_id] = action.get("value")
        else:
            answers[block_id] = _extract_action_value(action)

    required_ids = {f["id"] for f in pending["fields"] if f.get("required")}
    missing = required_ids - {k for k, v in answers.items() if v not in (None, "", [])}
    if missing:
        return {"response_action": "errors", "errors": {fid: "This field is required" for fid in missing}}

    await record_answers(db, iid, answers)
    await delete_pending_interaction(db, iid)

    birdy_user_id = pending["birdy_user_id"]
    bot_token = await get_decrypted_bot_token_for_user(db, birdy_user_id)
    if bot_token:
        from slack_sdk.web.async_client import AsyncWebClient
        slack_client = AsyncWebClient(token=bot_token)
        await _run_chat_and_reply(db, mongo_client, slack_client, pending, answers)

    return {}


async def _post_action_error(db, birdy_user_id: str, payload: dict, res: dict) -> None:
    """Reply in-thread when a Slack-triggered action couldn't be completed."""
    bot_token = await get_decrypted_bot_token_for_user(db, birdy_user_id)
    channel = (payload.get("channel") or {}).get("id")
    ts = (payload.get("message") or {}).get("ts")
    if not bot_token or not channel:
        return
    msg = {
        "not_found": "That suggestion is no longer available.",
        "not_applied": "Nothing to undo — this suggestion isn't applied.",
        "failed": f"Couldn't complete that — {res.get('detail', 'error')}",
    }.get(res.get("outcome"), "Couldn't complete that action.")
    from slack_sdk.web.async_client import AsyncWebClient
    try:
        await AsyncWebClient(token=bot_token).chat_postMessage(
            channel=channel, thread_ts=ts, text=f":warning: {msg}"
        )
    except Exception as e:
        logger.warning(f"suggestion action: error notice failed: {e}")


async def _handle_suggestion_action(payload: dict, action: dict, db, mongo_client) -> dict:
    """
    Handle the "Do it for me" / "Ignore" / "Undo" buttons on a suggestion message.
    Resolves the tenant from the Slack team_id and runs the shared action — which
    also re-renders the Slack card, so we don't update the message here (the same
    path runs when the action is taken from the dashboard, keeping both in sync).
    """
    team_id = (payload.get("team") or {}).get("id")
    birdy_user_id = await get_user_id_by_team_id(db, team_id) if team_id else None
    if not birdy_user_id:
        return {}

    suggestion_id = action.get("value")
    action_id = action.get("action_id")
    slack_user = (payload.get("user") or {}).get("id")

    if action_id == "suggestion_apply":
        res = await actions.apply_suggestion(
            db, mongo_client, birdy_user_id, suggestion_id, source="slack", by=slack_user)
    elif action_id == "suggestion_undo":
        res = await actions.undo_suggestion(
            db, mongo_client, birdy_user_id, suggestion_id, source="slack", by=slack_user)
    else:
        res = await actions.dismiss_suggestion(
            db, mongo_client, birdy_user_id, suggestion_id, source="slack", by=slack_user)

    if not res.get("ok"):
        await _post_action_error(db, birdy_user_id, payload, res)
    return {}


@router.post("/api/slack/interactions")
async def slack_interactions(request: Request):
    raw_body = await request.body()
    verify_slack_request(request, raw_body)

    form = await request.form()
    payload = json.loads(form["payload"])
    ptype = payload.get("type")

    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)

        if ptype == "block_actions":
            action0 = (payload.get("actions") or [{}])[0]
            if action0.get("action_id") in ("suggestion_apply", "suggestion_ignore", "suggestion_undo"):
                return await _handle_suggestion_action(payload, action0, db, mongo_client)
            return await _handle_block_actions(payload, db, mongo_client)
        if ptype == "view_submission":
            return await _handle_view_submission(payload, db, mongo_client)
        if ptype == "view_closed":
            iid = (payload.get("view") or {}).get("private_metadata", "")
            if iid:
                await delete_pending_interaction(db, iid)
            return {}

    logger.warning(f"Unhandled Slack interaction type: {ptype}")
    return {}
