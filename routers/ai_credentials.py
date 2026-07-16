"""
routers/ai_credentials.py
----------------------------
Lets an authenticated Birdy user (via the normal auth_token cookie) connect
their own Anthropic/OpenAI API key + model for chat (BYOK) — see
services/ai_credential_service.py. Once configured, routers/chat.py uses
THIS credential exclusively; there is no fallback to Birdy's own
globally-configured provider.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.database import get_db
from core.models import (
    SaveAiCredentialRequest, AiCredentialStatusResponse,
    AiModelsResponse, AiModelOption,
)
from dependencies import get_mongo_client, get_current_user
from ai.config import AVAILABLE_AI_MODELS
from services.ai_credential_service import (
    save_ai_credential,
    get_ai_credential_status,
    remove_ai_credential,
    CredentialValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/integrations/ai/models", response_model=AiModelsResponse)
async def list_ai_models():
    """Curated tool-calling-capable models per provider, for the connect form's dropdown."""
    return AiModelsResponse(
        anthropic=[AiModelOption(**m) for m in AVAILABLE_AI_MODELS["anthropic"]],
        openai=[AiModelOption(**m) for m in AVAILABLE_AI_MODELS["openai"]],
    )


@router.get("/api/integrations/ai/status", response_model=AiCredentialStatusResponse)
async def get_ai_status(current_user: str = Depends(get_current_user)):
    """Current BYOK status — masked key preview only, never the full key."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        status = await get_ai_credential_status(db, current_user)
        if not status:
            return AiCredentialStatusResponse(configured=False)
        return AiCredentialStatusResponse(configured=True, **status)


@router.post("/api/integrations/ai/connect", response_model=AiCredentialStatusResponse)
async def connect_ai_credential(
    request: SaveAiCredentialRequest,
    current_user: str = Depends(get_current_user),
):
    """Live-validate (real API call, forced tool_choice) and save a user's own AI key + model."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        try:
            result = await save_ai_credential(
                db, current_user, request.provider, request.api_key, request.model
            )
        except CredentialValidationError as e:
            raise HTTPException(status_code=422, detail=e.message)
        except Exception as e:
            logger.error(f"Error saving AI credential for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save AI credential")
        logger.info(f"AI credential validated and saved for user: {current_user}")
        return AiCredentialStatusResponse(configured=True, **result)


@router.delete("/api/integrations/ai/remove")
async def remove_ai_credential_endpoint(current_user: str = Depends(get_current_user)):
    """Permanently remove the user's BYOK AI credential."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        try:
            removed = await remove_ai_credential(db, current_user)
            if not removed:
                raise HTTPException(status_code=404, detail="User not found")
            logger.info(f"Removed AI credential for user: {current_user}")
            return {"success": True, "message": "AI credential removed successfully"}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing AI credential for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to remove AI credential: {str(e)}")
