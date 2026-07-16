import logging

from fastapi import APIRouter, Depends, HTTPException

from core.models import ChatRequest, ChatResponse
from core.database import get_db
from dependencies import get_current_user, get_mongo_client
from services.ai_credential_service import get_decrypted_credential_for_chat

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


class NoAiCredentialError(Exception):
    """Raised when the user hasn't connected their own AI provider credential."""


async def _get_provider(current_user: str, db):
    """Construct the AI provider from the user's own BYOK credential.

    There is intentionally NO fallback to a Birdy-global provider — chat
    only works once a user has connected their own Anthropic/OpenAI key via
    routers/ai_credentials.py.
    """
    cred = await get_decrypted_credential_for_chat(db, current_user)
    if not cred:
        raise NoAiCredentialError()

    if cred["provider"] == "anthropic":
        from ai.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=cred["api_key"], model=cred["model"])
    elif cred["provider"] == "openai":
        from ai.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=cred["api_key"], model=cred["model"])
    else:
        # Shouldn't happen — provider is validated at save time — but fail loudly rather than guess.
        raise NoAiCredentialError()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            provider = await _get_provider(current_user, db)
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
        except NoAiCredentialError:
            raise HTTPException(status_code=412, detail="no_ai_credentials")
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to process chat message")
