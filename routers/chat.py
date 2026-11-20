import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from core.models import ChatRequest, ChatResponse
from core.database import get_db
from dependencies import get_current_user, get_mongo_client
from ai.provider_factory import get_provider_for_user, NoAiCredentialError
from credits_middleware import check_credits

from ai.tools.registry import registry
from ai.tools.meta_tools import register_meta_tools
from ai.tools.ghl_tools import register_ghl_tools
from ai.tools.group_tools import register_group_tools
from ai.tools.summary_tools import register_summary_tools
from ai.tools.compare_tools import register_compare_tools
from ai.tools.alert_tools import register_alert_tools
from ai.tools.meta_live_tools import register_meta_live_tools
from ai.tools.custom_metrics_tools import register_custom_metrics_tools
from ai.tools.unified_leads_tools import register_unified_leads_tools
from ai.tools.multi_window_tools import register_multi_window_tools
from ai.tools.hp_tools import register_hp_tools
from ai.tools.call_analysis_tools import register_call_analysis_tools
from ai.tools.report_designer_tools import register_report_designer_tools
from ai.tools.dashboard_tools import register_dashboard_tools
from ai.tools.agent_tools import register_agent_tools
from ai.tools.creative_tools import register_creative_tools
from ai.tools.render_ui_tools import register_render_ui_tools
from ai.tools.page_view_tools import register_page_view_tools
from ai.tools.attribution_tools import register_attribution_tools
from ai.orchestrator import run_chat
from ai.team import definitions as team_defs
from ai.tools.agent_tools import current_training_agent
from ai.tools.stock_card_tools import (current_card_read_only, current_stock_card, load_chat_card,
                                      register_stock_card_tools)
from ai.tools.stock_lesson_tools import current_lessons_agent, load_lessons_agent, register_stock_lesson_tools

logger = logging.getLogger(__name__)

router = APIRouter()

# Pages whose chat acts on one custom agent (request.agent_id): training it, or setting it up.
AGENT_CHAT_PAGES = ("agent_training", "agent_setup")

# Register all tools once at module load
register_meta_tools()
register_ghl_tools()
register_group_tools()
register_summary_tools()
register_compare_tools()
register_alert_tools()
register_meta_live_tools()
register_custom_metrics_tools()
register_unified_leads_tools()
register_multi_window_tools()
register_hp_tools()
register_call_analysis_tools()
register_report_designer_tools()
register_dashboard_tools()
register_agent_tools()
register_stock_card_tools()
register_stock_lesson_tools()
register_creative_tools()
register_render_ui_tools()
register_page_view_tools()
register_attribution_tools()


def _archive_ids(request: ChatRequest) -> dict:
    """The agent and card an agent chat is archived under; nothing for any other page."""
    if request.page not in HIDDEN_HISTORY_PAGES:
        return {}
    return {"agent_id": request.agent_id, "card_id": request.card_id if request.page == "stock_card" else None}


async def _stock_card_id(db, user_id: str, request: ChatRequest) -> str | None:
    """Page "stock_card": the card the chat acts on, after an owner check (404 card_not_found)."""
    if request.page != "stock_card":
        return None
    card = await load_chat_card(db, user_id, request.card_id, request.agent_id)
    if card is None:
        raise HTTPException(status_code=404, detail="card_not_found")
    return card["_id"]


async def _lessons_agent_id(db, user_id: str, request: ChatRequest) -> str | None:
    """Page "stock_lessons": the user's own stock agent Teach Birdy works on (404 agent_not_found)."""
    if request.page != "stock_lessons":
        return None
    agent = await load_lessons_agent(db, user_id, request.agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return agent["_id"]


async def _onboarding_complete(db, user_id: str) -> bool:
    user = await db["users"].find_one({"user_id": user_id}, {"onboarding.completed": 1}) or {}
    return bool((user.get("onboarding") or {}).get("completed"))


async def _checks(db, current_user: str, request: ChatRequest, mongo_client) -> tuple:
    """Owner checks, the credit gate and the provider: everything that answers with an
    HTTP status before a turn starts. Returns (provider, stock_card_id, lessons_agent_id)."""
    stock_card_id = await _stock_card_id(db, current_user, request)
    lessons_agent_id = await _lessons_agent_id(db, current_user, request)
    # Stopper: block when the user is out of Birdy Credits (raises 402
    # OUT_OF_CREDITS, which the frontend turns into a top-up prompt).
    # Except the report copilot during onboarding: it runs before
    # billing, and a new account must be able to finish setting up.
    if not (request.page == "report_designer"
            and not await _onboarding_complete(db, current_user)):
        await check_credits(current_user, mongo_client)
    provider = await get_provider_for_user(current_user, db)
    if lessons_agent_id is None and request.agent == team_defs.MEDIA_BUYER:
        # Media Buyer's own chat works on its lessons (the stock agent's memory).
        lessons_agent_id = await team_defs.stock_agent_id(db, current_user)
    return provider, stock_card_id, lessons_agent_id


async def _run_turn(db, mongo_client, current_user: str, request: ChatRequest, provider,
                    stock_card_id, lessons_agent_id, on_route=None) -> dict:
    """One chat turn with this request's ContextVars set, and reset afterwards."""
    agent_token = current_training_agent.set(
        request.agent_id if request.page in AGENT_CHAT_PAGES else None)
    card_token = current_stock_card.set(stock_card_id)
    read_only_token = current_card_read_only.set(bool(stock_card_id and request.card_read_only))
    lessons_token = current_lessons_agent.set(lessons_agent_id)
    try:
        return await run_chat(
            provider=provider,
            tool_registry=registry,
            db=db,
            user_id=current_user,
            message=request.message,
            session_id=request.session_id,
            page=request.page,
            mongo_client=mongo_client,
            client_group_id=request.client_group_id,
            client_name=request.client_name,
            dashboard_id=request.dashboard_id,
            dashboard_summary=request.summary,
            view_catalogues=request.view_catalogues,
            attachments=[a.model_dump(exclude_none=True) for a in request.attachments or []] or None,
            **({"agent": request.agent, "on_route": on_route} if request.agent else {}),
            **_archive_ids(request),
        )
    finally:
        current_lessons_agent.reset(lessons_token)
        current_card_read_only.reset(read_only_token)
        current_stock_card.reset(card_token)
        current_training_agent.reset(agent_token)


@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            provider, stock_card_id, lessons_agent_id = await _checks(db, current_user, request, mongo_client)
            result = await _run_turn(db, mongo_client, current_user, request, provider,
                                     stock_card_id, lessons_agent_id)
            return ChatResponse(**result)
        except HTTPException:
            # Preserve deliberate status codes (e.g. 402 OUT_OF_CREDITS) instead
            # of collapsing them into the generic 500 below.
            raise
        except NoAiCredentialError:
            raise HTTPException(status_code=412, detail="no_ai_credentials")
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to process chat message")


# ──────────────────────────────────────────────────────────────────────────
# Streaming (CHAT ROUTE CONTRACT v1)
#
# Same request as /api/chat. Events, as text/event-stream:
#   event: route   data = the whole current route object, every time it changes
#   event: final   data = exactly the JSON /api/chat would return
#   event: error   data = {"message": string}
# Checks that answer with a status (402 out of credits, 404 card/agent, 412 no
# AI key, 422 bad body) still do so before the stream opens. While a turn runs
# with nothing new to say, a comment line (": keep-alive") goes out every
# KEEPALIVE_SECONDS so proxies don't close an idle connection.
# ──────────────────────────────────────────────────────────────────────────

KEEPALIVE_SECONDS = 15


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n"


async def _event_stream(db, mongo_client, current_user: str, request: ChatRequest, provider,
                        stock_card_id, lessons_agent_id):
    queue: asyncio.Queue = asyncio.Queue()
    last = {"route": None}

    def on_route(route: dict) -> None:
        if route != last["route"]:
            last["route"] = route
            queue.put_nowait(("route", route))

    async def work():
        try:
            result = await _run_turn(db, mongo_client, current_user, request, provider,
                                     stock_card_id, lessons_agent_id, on_route=on_route)
            queue.put_nowait(("final", ChatResponse(**result).model_dump(mode="json")))
        except NoAiCredentialError:
            queue.put_nowait(("error", {"message": "no_ai_credentials"}))
        except Exception as e:
            logger.error(f"Chat stream error: {e}", exc_info=True)
            queue.put_nowait(("error", {"message": "Failed to process chat message"}))

    task = asyncio.create_task(work())
    try:
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield _sse(event, data)
            if event in ("final", "error"):
                break
    finally:
        if not task.done():
            # The client went away: stop the turn rather than spend credits on nobody.
            task.cancel()


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        try:
            provider, stock_card_id, lessons_agent_id = await _checks(db, current_user, request, mongo_client)
        except HTTPException:
            raise
        except NoAiCredentialError:
            raise HTTPException(status_code=412, detail="no_ai_credentials")
    # The Motor client is a process-wide singleton (dependencies.get_mongo_client),
    # so it stays usable after the context above has exited.
    return StreamingResponse(
        _event_stream(db, mongo_client, current_user, request, provider, stock_card_id, lessons_agent_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# ──────────────────────────────────────────────────────────────────────────
# Conversation history
#
# Reads the user's own turns out of ai_conversation_log — the permanent,
# append-only archive that already backs the Admin console. Working memory
# (ai_chat_sessions) expires after an hour and is capped at 50 messages, so it
# can never answer "what did I ask Birdy last week?"; the archive can.
#
# Scoped to source "birdy": the same log also captures the Slack bot, and those
# threads do not belong in the web app's history list.
# ──────────────────────────────────────────────────────────────────────────

MAX_CONVERSATIONS = 100
TITLE_LENGTH = 60
# Agent chats (a stock card, Teach Birdy, agent setup) are archived with their
# agent/card and reopened from that agent's page, never from the history list.
HIDDEN_HISTORY_PAGES = {"stock_card", "stock_lessons", "agent_setup"}


@router.get("/api/chat/conversations")
async def list_conversations(
    client_group_id: str | None = None,
    current_user: str = Depends(get_current_user),
):
    """
    The current user's conversations, most recently active first.

    Each entry: { session_id, title, message_count, created_at, updated_at }.
    The title is the opening user message, which is what the sidebar shows.

    With `client_group_id`, only threads opened from that client's own page.
    Threads logged before this field existed carry no client and so do not
    appear in a scoped list — they are still in the unscoped one.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            rows = await db["ai_conversation_log"].aggregate([
                {"$match": {
                    "user_id": current_user,
                    "source": "birdy",
                    "page": {"$nin": sorted(HIDDEN_HISTORY_PAGES)},
                    **({"client_group_id": client_group_id} if client_group_id else {}),
                }},
                {"$sort": {"created_at": 1}},
                {"$group": {
                    "_id": "$session_id",
                    "created_at": {"$first": "$created_at"},
                    "updated_at": {"$last": "$created_at"},
                    "message_count": {"$sum": 1},
                    # User turns only — assistant prose never titles a chat.
                    # $$REMOVE drops non-user rows instead of padding with nulls.
                    "user_turns": {"$push": {
                        "$cond": [{"$eq": ["$role", "user"]}, "$content", "$$REMOVE"]
                    }},
                    # Every distinct client this thread touched. Same rule as
                    # the orchestrator's _conversation_scope: exactly one →
                    # a client conversation; zero or several → global.
                    "group_ids": {"$addToSet": "$client_group_id"},
                }},
                {"$sort": {"updated_at": -1}},
                {"$limit": MAX_CONVERSATIONS},
                # The title only needs the opening turn; don't ship the rest.
                {"$set": {"user_turns": {"$slice": ["$user_turns", 3]}}},
            ]).to_list(length=MAX_CONVERSATIONS)

            # One name lookup for every client-scoped thread in the list,
            # rather than a query per row.
            scoped_ids = set()
            for r in rows:
                gids = [g for g in (r.get("group_ids") or []) if g]
                r["_scope_gid"] = gids[0] if len(gids) == 1 else None
                if r["_scope_gid"]:
                    scoped_ids.add(r["_scope_gid"])
            names = {}
            if scoped_ids:
                async for g in db["client_groups"].find(
                    {"user_id": current_user, "id": {"$in": list(scoped_ids)}},
                    {"id": 1, "name": 1},
                ):
                    names[g["id"]] = g.get("name")

            conversations = []
            for r in rows:
                # UI responses are protocol noise, not something to title a chat.
                opening = next(
                    (c for c in (r.get("user_turns") or [])
                     if c and not c.startswith("[UI_RESPONSE]")),
                    "",
                )
                title = (opening or "New Conversation")[:TITLE_LENGTH]
                if opening and len(opening) > TITLE_LENGTH:
                    title += "…"
                # A thread whose one tagged client no longer resolves (group
                # deleted) reads as global rather than wearing a blank badge.
                gid = r["_scope_gid"] if r.get("_scope_gid") in names else None
                conversations.append({
                    "session_id": r["_id"],
                    "title": title,
                    "scope": "client" if gid else "global",
                    "client_group_id": gid,
                    "client_name": names.get(gid),
                    "message_count": r.get("message_count", 0),
                    "created_at": (r.get("created_at") or "").isoformat()
                        if hasattr(r.get("created_at"), "isoformat") else r.get("created_at"),
                    "updated_at": (r.get("updated_at") or "").isoformat()
                        if hasattr(r.get("updated_at"), "isoformat") else r.get("updated_at"),
                })
            return {"conversations": conversations}
        except Exception as e:
            logger.error(f"Error listing conversations for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load conversations")


@router.get("/api/chat/conversations/{session_id}")
async def get_conversation(
    session_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    Every message in one conversation, oldest first.

    The user_id match is the authorisation check — a session belonging to
    someone else reads as empty and 404s rather than leaking its contents.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            rows = await db["ai_conversation_log"].find(
                {"session_id": session_id, "user_id": current_user, "source": "birdy"},
                {"role": 1, "content": 1, "tools_used": 1, "created_at": 1, "_id": 0},
            ).sort("created_at", 1).to_list(length=None)

            if not rows:
                raise HTTPException(status_code=404, detail="Conversation not found")

            return {
                "session_id": session_id,
                "messages": [{
                    "role": r["role"],
                    "content": r.get("content") or "",
                    "tools_used": r.get("tools_used") or [],
                } for r in rows],
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error loading conversation {session_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load conversation")


@router.delete("/api/chat/conversations/{session_id}")
async def delete_conversation(
    session_id: str,
    current_user: str = Depends(get_current_user),
):
    """
    Remove a conversation from the user's history, along with any live working
    memory for it. Scoped by user_id so one user cannot delete another's.
    """
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            result = await db["ai_conversation_log"].delete_many(
                {"session_id": session_id, "user_id": current_user, "source": "birdy"}
            )
            if result.deleted_count == 0:
                raise HTTPException(status_code=404, detail="Conversation not found")

            await db["ai_chat_sessions"].delete_one(
                {"_id": session_id, "user_id": current_user}
            )
            logger.info(
                f"Deleted conversation {session_id} for {current_user} "
                f"({result.deleted_count} messages)"
            )
            return {"success": True, "deleted": result.deleted_count}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting conversation {session_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete conversation")
