"""
services/ai_credential_service.py — BYOK save/get/remove lifecycle, and
ai/provider_factory.py::get_provider_for_user's "no fallback" behavior. `validate_credential`
(the live provider API call) is mocked in every test here — it's exercised
for real in tests/test_ai_credential_validation_live.py instead, which is
skipped unless a real API key is available.
"""

from unittest.mock import patch

import pytest

from services import ai_credential_service as svc
from services.ai_credential_service import CredentialValidationError


@pytest.fixture(autouse=True)
def mock_validation():
    with patch.object(svc, "validate_credential", return_value=None) as m:
        yield m


@pytest.mark.asyncio
async def test_save_then_get_status_never_exposes_key(mock_db):
    saved = await svc.save_ai_credential(mock_db, "alice@example.com", "anthropic", "sk-ant-secret-abcd", "claude-sonnet-5")
    assert saved["provider"] == "anthropic"
    assert saved["model"] == "claude-sonnet-5"
    assert saved["key_preview"] == "...abcd"
    assert "api_key" not in saved

    status = await svc.get_ai_credential_status(mock_db, "alice@example.com")
    assert status["provider"] == "anthropic"
    assert status["key_preview"] == "...abcd"
    assert "api_key" not in status
    assert "api_key_encrypted" not in status


@pytest.mark.asyncio
async def test_get_status_none_when_not_configured(mock_db):
    assert await svc.get_ai_credential_status(mock_db, "nobody@example.com") is None


@pytest.mark.asyncio
async def test_save_rejects_unsupported_provider(mock_db, mock_validation):
    with pytest.raises(CredentialValidationError, match="Unsupported provider"):
        await svc.save_ai_credential(mock_db, "alice@example.com", "cohere", "key", "some-model")
    mock_validation.assert_not_called()  # rejected before the live test call


@pytest.mark.asyncio
async def test_save_rejects_model_not_in_curated_list(mock_db, mock_validation):
    with pytest.raises(CredentialValidationError, match="not a supported"):
        await svc.save_ai_credential(mock_db, "alice@example.com", "anthropic", "key", "gpt-4o")
    mock_validation.assert_not_called()  # wrong-provider model rejected before the live call


@pytest.mark.asyncio
async def test_save_propagates_validation_failure_without_persisting(mock_db):
    with patch.object(svc, "validate_credential", side_effect=CredentialValidationError("Invalid Anthropic API key.")):
        with pytest.raises(CredentialValidationError, match="Invalid Anthropic API key"):
            await svc.save_ai_credential(mock_db, "alice@example.com", "anthropic", "bad-key", "claude-sonnet-5")

    assert await svc.get_ai_credential_status(mock_db, "alice@example.com") is None


@pytest.mark.asyncio
async def test_get_decrypted_credential_for_chat_round_trips(mock_db):
    await svc.save_ai_credential(mock_db, "alice@example.com", "openai", "sk-openai-real-secret", "gpt-4o")
    cred = await svc.get_decrypted_credential_for_chat(mock_db, "alice@example.com")
    assert cred == {"provider": "openai", "model": "gpt-4o", "api_key": "sk-openai-real-secret"}


@pytest.mark.asyncio
async def test_get_decrypted_credential_none_when_not_configured(mock_db):
    assert await svc.get_decrypted_credential_for_chat(mock_db, "nobody@example.com") is None


@pytest.mark.asyncio
async def test_remove_returns_false_for_unknown_user(mock_db):
    assert await svc.remove_ai_credential(mock_db, "nobody@example.com") is False


@pytest.mark.asyncio
async def test_save_then_remove_then_status_is_none(mock_db):
    await svc.save_ai_credential(mock_db, "alice@example.com", "anthropic", "sk-ant-abcd", "claude-sonnet-5")
    assert await svc.remove_ai_credential(mock_db, "alice@example.com") is True
    assert await svc.get_ai_credential_status(mock_db, "alice@example.com") is None
    assert await svc.get_decrypted_credential_for_chat(mock_db, "alice@example.com") is None


# ── ai/provider_factory.py::get_provider_for_user — no-fallback behavior ────

@pytest.mark.asyncio
async def test_get_provider_raises_when_not_configured(mock_db):
    from ai.provider_factory import get_provider_for_user as _get_provider, NoAiCredentialError

    with pytest.raises(NoAiCredentialError):
        await _get_provider("nobody@example.com", mock_db)


@pytest.mark.asyncio
async def test_get_provider_constructs_anthropic_from_saved_credential(mock_db):
    from ai.provider_factory import get_provider_for_user as _get_provider
    from ai.providers.anthropic_provider import AnthropicProvider

    await svc.save_ai_credential(mock_db, "alice@example.com", "anthropic", "sk-ant-abcd", "claude-sonnet-5")
    provider = await _get_provider("alice@example.com", mock_db)
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_get_provider_constructs_openai_from_saved_credential(mock_db):
    from ai.provider_factory import get_provider_for_user as _get_provider
    from ai.providers.openai_provider import OpenAIProvider

    await svc.save_ai_credential(mock_db, "bob@example.com", "openai", "sk-openai-abcd", "gpt-4o")
    provider = await _get_provider("bob@example.com", mock_db)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-4o"
