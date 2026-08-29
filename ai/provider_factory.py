import logging

logger = logging.getLogger(__name__)

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
    """The provider every chat runs on: Birdy's own OpenAI account.

    Users no longer bring their own key. A wrong paste, or a key for a model
    that cannot call tools, degraded chat in ways the user could not diagnose,
    and the failure surfaced as Birdy being unable to answer rather than as a
    configuration problem.

    A stored credential is still honoured if one exists, so accounts that set
    one before this change keep working — but nothing writes new ones.
    """
    cred = await get_decrypted_credential_for_chat(db, user_id)

    if cred:
        if cred["provider"] == "anthropic":
            from ai.providers.anthropic_provider import AnthropicProvider
            return AnthropicProvider(api_key=cred["api_key"], model=cred["model"])
        if cred["provider"] == "openai":
            from ai.providers.openai_provider import OpenAIProvider
            return OpenAIProvider(api_key=cred["api_key"], model=cred["model"])
        # Provider is validated at save time, so an unknown one means the
        # stored document is bad. Fall through to the account key rather than
        # locking the user out of chat over it.
        logger.warning("Unknown stored AI provider %r for %s — using the account key",
                       cred.get("provider"), user_id)

    from ai.config import OPENAI_API_KEY, OPENAI_MODEL
    if not OPENAI_API_KEY:
        # Nothing to run on. Still raises the error the chat route turns into a
        # 412, but this is now an operator problem, not a user one.
        logger.error("OPENAI_API_KEY is not set — chat cannot run for any user")
        raise NoAiCredentialError()

    from ai.providers.openai_provider import OpenAIProvider
    return OpenAIProvider(model=OPENAI_MODEL)

    if cred["provider"] == "anthropic":
        from ai.providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=cred["api_key"], model=cred["model"])
    elif cred["provider"] == "openai":
        from ai.providers.openai_provider import OpenAIProvider
        return OpenAIProvider(api_key=cred["api_key"], model=cred["model"])
    else:
        # Shouldn't happen — provider is validated at save time — but fail loudly rather than guess.
        raise NoAiCredentialError()
