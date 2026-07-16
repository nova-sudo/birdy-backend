"""
ai/provider_factory.py
-------------------------
Shared BYOK provider construction — used by both routers/chat.py (the web
app's /api/chat) and the Slack bot (routers/slack_events.py,
routers/slack_interactions.py), so both entry points build the LLM provider
the exact same way instead of duplicating this logic.

There is intentionally NO fallback to a Birdy-global provider — chat only
works once a user has connected their own Anthropic/OpenAI key via
routers/ai_credentials.py.
"""

from services.ai_credential_service import get_decrypted_credential_for_chat


class NoAiCredentialError(Exception):
    """Raised when the user hasn't connected their own AI provider credential."""


async def get_provider_for_user(user_id: str, db):
    """Construct the AI provider from the user's own BYOK credential."""
    cred = await get_decrypted_credential_for_chat(db, user_id)
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
