"""
routers/slack_events.py
--------------------------
Slack's Events API webhook — the "plain conversation" path (@mentions and
DMs). Interactive :::ui forms are handled separately in
routers/slack_interactions.py once a message containing them has been sent
from here.

Processes run_chat() synchronously rather than via FastAPI BackgroundTasks:
Vercel's Lambda-style Python runtime can freeze the execution environment
immediately after the HTTP response flushes (the same constraint that
already rules out APScheduler on this deployment — see main.py's lifespan
comment), so background work here has no guarantee of completing. This means
we exceed Slack's 3-second ack window and Slack will retry the delivery;
we detect that via X-Slack-Retry-Num and no-op rather than reprocessing.
"""

import logging
import re

from fastapi import APIRouter, Request

from ai.orchestrator import run_chat
from ai.provider_factory import get_provider_for_user, NoAiCredentialError
from ai.tools.registry import registry
from core.database import get_db
from dependencies import get_mongo_client
from services.slack_bot_service import (
    get_user_id_by_team_id,
    get_decrypted_bot_token_for_user,
    get_thread_session_id,
    save_thread_session_id,
)
from services.slack_block_formatter import build_blocks_from_reply
from services.slack_interaction_store import create_pending_interaction
from services.slack_signature import verify_slack_request

logger = logging.getLogger(__name__)

router = APIRouter()

_MENTION_RE = re.compile(r"^\s*<@[A-Z0-9]+>\s*")


@router.post("/api/slack/events")
async def slack_events(request: Request):
    raw_body = await request.body()
    verify_slack_request(request, raw_body)

    payload = await request.json()

    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}

    if payload.get("type") != "event_callback":
        return {}

    if request.headers.get("X-Slack-Retry-Num"):
        # We couldn't ack within Slack's 3s window on the first attempt and
        # it's retrying — the original invocation is presumably still
        # working (or already posted the reply); don't reprocess.
        return {}

    event = payload.get("event") or {}
    if event.get("bot_id") or event.get("subtype"):
        return {}  # self/other-bot loop guard, ignore edits/deletes

    event_type = event.get("type")
    if event_type == "app_mention":
        text = _MENTION_RE.sub("", event.get("text", ""))
    elif event_type == "message" and event.get("channel_type") == "im":
        text = event.get("text", "")
    else:
        return {}

    if not text.strip():
        return {}

    team_id = payload.get("team_id")
    channel = event["channel"]
    thread_key = event.get("thread_ts") or event["ts"]

    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)

        user_id = await get_user_id_by_team_id(db, team_id)
        if not user_id:
            logger.info(f"Slack event from unknown/uninstalled team: {team_id}")
            return {}

        bot_token = await get_decrypted_bot_token_for_user(db, user_id)
        if not bot_token:
            return {}

        from slack_sdk.web.async_client import AsyncWebClient
        slack_client = AsyncWebClient(token=bot_token)

        try:
            provider = await get_provider_for_user(user_id, db)
        except NoAiCredentialError:
            await slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_key,
                text=(
                    "This Birdy account hasn't connected an AI key yet — "
                    "ask the workspace owner to connect one in Settings → Integrations."
                ),
            )
            return {}

        # Everything below can throw (run_chat, block formatting, the interaction
        # store, Slack's own API) and nothing upstream of this was catching it —
        # an unhandled exception here means Slack silently gets nothing back,
        # with no visible error and no retry (X-Slack-Retry-Num is already
        # deduped above, so Slack's own retry doesn't help either). Concretely
        # hit via the internal MCP token's TTL expiring mid-turn on a slow tool
        # call (see ai/mcp_client.py) — but this net is deliberately generic so
        # any other future failure here degrades to a message instead of silence.
        try:
            session_id = await get_thread_session_id(db, team_id, channel, thread_key)

            result = await run_chat(
                provider=provider,
                tool_registry=registry,
                db=db,
                user_id=user_id,
                message=text,
                session_id=session_id,
                mongo_client=mongo_client,
            )

            blocks, fallback_text, ui_pending = build_blocks_from_reply(
                result["reply"], iid_prefix=result["session_id"]
            )

            for pending in ui_pending:
                await create_pending_interaction(
                    db, pending["iid"],
                    team_id=team_id, channel_id=channel, thread_ts=thread_key,
                    slack_user_id=event.get("user"), birdy_user_id=user_id,
                    session_id=result["session_id"], fields=pending["fields"], mode=pending["mode"],
                )

            await slack_client.chat_postMessage(
                channel=channel, thread_ts=thread_key, blocks=blocks, text=fallback_text,
            )

            await save_thread_session_id(db, team_id, channel, thread_key, user_id, result["session_id"])
        except Exception:
            logger.error(
                f"Unhandled error processing Slack event for user {user_id} "
                f"in channel {channel}",
                exc_info=True,
            )
            try:
                await slack_client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_key,
                    text="Something went wrong processing that — please try again.",
                )
            except Exception:
                logger.error("Also failed to post the fallback error message to Slack", exc_info=True)

    return {}
