import logging

from fastapi import APIRouter, Depends, HTTPException

from core.models import ChatRequest, ChatResponse
from core.database import get_db
from dependencies import get_current_user, get_mongo_client

from ai.config import AI_PROVIDER
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


def _get_provider():
    """Return the configured AI provider instance."""
    if AI_PROVIDER == "anthropic":
        from ai.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    elif AI_PROVIDER == "openrouter":
        from ai.providers.openrouter_provider import OpenRouterProvider
        return OpenRouterProvider()
    elif AI_PROVIDER == "mistral":
        from ai.providers.mistral_provider import MistralProvider
        return MistralProvider()
    else:
        from ai.providers.groq_provider import GroqProvider
        return GroqProvider()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            provider = _get_provider()
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
        except ValueError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error(f"Chat error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to process chat message")
