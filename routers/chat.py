import logging

from fastapi import APIRouter, Depends, HTTPException

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
from ai.orchestrator import run_chat

logger = logging.getLogger(__name__)

router = APIRouter()

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


@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            # Stopper: block when the user is out of Birdy Credits (raises 402
            # OUT_OF_CREDITS, which the frontend turns into a top-up prompt).
            await check_credits(current_user, mongo_client)
            provider = await get_provider_for_user(current_user, db)
            result = await run_chat(
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
            )
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
                }},
                {"$sort": {"updated_at": -1}},
                {"$limit": MAX_CONVERSATIONS},
                # The title only needs the opening turn; don't ship the rest.
                {"$set": {"user_turns": {"$slice": ["$user_turns", 3]}}},
            ]).to_list(length=MAX_CONVERSATIONS)

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
                conversations.append({
                    "session_id": r["_id"],
                    "title": title,
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
