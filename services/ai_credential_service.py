"""
services/ai_credential_service.py
------------------------------------
BYOK (bring-your-own-key) AI credentials. Each user configures their own
Anthropic or OpenAI API key + model in Settings -> Integrations; Birdy's
chat feature (routers/chat.py) then calls Anthropic/OpenAI using THAT key
instead of Birdy's own globally-configured provider (ai/config.py's
AI_PROVIDER / ANTHROPIC_API_KEY / etc., which remain in place only as
Groq/Mistral/OpenRouter's internal config — unused by user-facing chat now).

Keys are encrypted at rest (core/crypto.py, Fernet) — only the ciphertext,
a short preview, and non-secret metadata (provider, model, validated,
timestamps) are stored on integrations.ai_credential.
"""

import logging
from datetime import datetime

from core.crypto import encrypt, decrypt
from ai.config import AVAILABLE_AI_MODELS

logger = logging.getLogger(__name__)

_DUMMY_TOOL_NAME = "ping"
_DUMMY_TOOL_DESCRIPTION = "A trivial no-op tool used only to verify this model supports tool calling."
_DUMMY_TOOL_PARAMS = {
    "type": "object",
    "properties": {"reason": {"type": "string", "description": "Any short string."}},
    "required": ["reason"],
}
_VALIDATION_PROMPT = "Call the ping tool now with any reason."


class CredentialValidationError(Exception):
    """Raised when the live tool-use test call fails. `.message` is
    user-readable and safe to return directly in an API response."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


async def validate_credential(provider: str, api_key: str, model: str) -> None:
    """Makes ONE real API call to the provider with a forced tool_choice.
    Raises CredentialValidationError with a specific message on any failure;
    returns None (success) if the model actually responds with a tool call."""
    if provider == "anthropic":
        await _validate_anthropic(api_key, model)
    elif provider == "openai":
        await _validate_openai(api_key, model)
    else:
        raise CredentialValidationError(f"Unsupported provider '{provider}'")


async def _validate_anthropic(api_key: str, model: str) -> None:
    from anthropic import (
        AsyncAnthropic, AuthenticationError, NotFoundError,
        PermissionDeniedError, BadRequestError, APIStatusError,
    )

    client = AsyncAnthropic(api_key=api_key)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": _VALIDATION_PROMPT}],
            tools=[{
                "name": _DUMMY_TOOL_NAME,
                "description": _DUMMY_TOOL_DESCRIPTION,
                "input_schema": _DUMMY_TOOL_PARAMS,
            }],
            tool_choice={"type": "any"},  # forces a tool call, not "auto"
        )
    except AuthenticationError:
        raise CredentialValidationError("Invalid Anthropic API key.")
    except NotFoundError:
        raise CredentialValidationError(f"Model '{model}' was not found or isn't accessible with this key.")
    except PermissionDeniedError:
        raise CredentialValidationError(f"This API key doesn't have access to '{model}'.")
    except BadRequestError as e:
        raise CredentialValidationError(f"Anthropic rejected the request: {e}")
    except APIStatusError as e:
        raise CredentialValidationError(f"Anthropic API error ({e.status_code}): {e}")

    has_tool_use = any(block.type == "tool_use" for block in response.content)
    if not has_tool_use:
        raise CredentialValidationError(
            f"'{model}' did not respond with a tool call — it may not support tool use."
        )


async def _validate_openai(api_key: str, model: str) -> None:
    from openai import (
        AsyncOpenAI, AuthenticationError, NotFoundError,
        PermissionDeniedError, BadRequestError, APIStatusError,
    )

    client = AsyncOpenAI(api_key=api_key)
    try:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": _VALIDATION_PROMPT}],
            tools=[{
                "type": "function",
                "function": {
                    "name": _DUMMY_TOOL_NAME,
                    "description": _DUMMY_TOOL_DESCRIPTION,
                    "parameters": _DUMMY_TOOL_PARAMS,
                },
            }],
            tool_choice="required",  # forces a tool call, not "auto"
        )
    except AuthenticationError:
        raise CredentialValidationError("Invalid OpenAI API key.")
    except NotFoundError:
        raise CredentialValidationError(f"Model '{model}' was not found or isn't accessible with this key.")
    except PermissionDeniedError:
        raise CredentialValidationError(f"This API key doesn't have access to '{model}'.")
    except BadRequestError as e:
        raise CredentialValidationError(f"OpenAI rejected the request: {e}")
    except APIStatusError as e:
        raise CredentialValidationError(f"OpenAI API error ({e.status_code}): {e}")

    message = response.choices[0].message
    if not message.tool_calls:
        raise CredentialValidationError(
            f"'{model}' did not respond with a tool call — it may not support tool use."
        )


async def save_ai_credential(db, user_id: str, provider: str, api_key: str, model: str) -> dict:
    if provider not in AVAILABLE_AI_MODELS:
        raise CredentialValidationError(f"Unsupported provider '{provider}'")
    if model not in {m["id"] for m in AVAILABLE_AI_MODELS[provider]}:
        raise CredentialValidationError(f"'{model}' is not a supported {provider} model")

    await validate_credential(provider, api_key, model)

    now = datetime.utcnow()
    doc = {
        "provider": provider,
        "model": model,
        "api_key_encrypted": encrypt(api_key),
        "key_preview": f"...{api_key[-4:]}" if len(api_key) >= 4 else "...****",
        "validated": True,
        "validated_at": now,
        "created_at": now,
        "updated_at": now,
    }
    await db["users"].update_one(
        {"user_id": user_id},
        {"$set": {"integrations.ai_credential": doc, "updated_at": now}},
        upsert=True,
    )
    logger.info(f"Saved AI credential ({provider}/{model}) for user: {user_id}")
    return {
        "provider": provider,
        "model": model,
        "key_preview": doc["key_preview"],
        "validated": True,
        "validated_at": now.isoformat(),
    }


async def get_ai_credential_status(db, user_id: str) -> dict | None:
    user_doc = await db["users"].find_one(
        {"user_id": user_id},
        projection={"integrations.ai_credential": 1},
    )
    cred = ((user_doc or {}).get("integrations") or {}).get("ai_credential")
    if not cred:
        return None
    validated_at = cred.get("validated_at")
    return {
        "provider": cred["provider"],
        "model": cred["model"],
        "key_preview": cred["key_preview"],
        "validated": cred.get("validated", False),
        "validated_at": validated_at.isoformat() if validated_at else None,
    }


async def remove_ai_credential(db, user_id: str) -> bool:
    result = await db["users"].update_one(
        {"user_id": user_id},
        {"$unset": {"integrations.ai_credential": ""}, "$set": {"updated_at": datetime.utcnow()}},
    )
    return result.matched_count > 0


async def get_decrypted_credential_for_chat(db, user_id: str) -> dict | None:
    """Internal use only (routers/chat.py). Returns the decrypted api_key —
    never expose this dict through an API response."""
    user_doc = await db["users"].find_one(
        {"user_id": user_id},
        projection={"integrations.ai_credential": 1},
    )
    cred = ((user_doc or {}).get("integrations") or {}).get("ai_credential")
    if not cred:
        return None
    return {
        "provider": cred["provider"],
        "model": cred["model"],
        "api_key": decrypt(cred["api_key_encrypted"]),
    }
