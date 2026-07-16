"""
Live tests for services/ai_credential_service.py::validate_credential — the
ONE place in this test suite that makes real calls to Anthropic/OpenAI.
Deliberately excluded from the default `pytest` run via the `live` marker
(see pytest.ini's `addopts = -m "not live"`) — run explicitly with
`pytest -m live tests/test_ai_credential_validation_live.py`.

Skipped automatically when the corresponding real API key isn't available
in the environment (.env), so CI / a fresh clone without production
credentials just skips rather than failing.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

from services.ai_credential_service import validate_credential, CredentialValidationError

pytestmark = [pytest.mark.asyncio, pytest.mark.live]


async def test_anthropic_rejects_obviously_fake_key():
    with pytest.raises(CredentialValidationError, match="Invalid Anthropic API key"):
        await validate_credential("anthropic", "sk-ant-totally-fake-key", "claude-sonnet-5")


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="no ANTHROPIC_API_KEY in .env")
async def test_anthropic_real_key_authenticates():
    """Proves the real key is genuinely valid (gets past auth), regardless of
    whether the account has enough credit balance to complete the call —
    that's an account/billing fact, not something this test can control."""
    try:
        await validate_credential("anthropic", os.getenv("ANTHROPIC_API_KEY"), "claude-sonnet-5")
    except CredentialValidationError as e:
        assert "Invalid Anthropic API key" not in e.message, f"Real key was rejected as invalid: {e.message}"


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="no OPENAI_API_KEY in .env")
async def test_openai_real_key_authenticates():
    try:
        await validate_credential("openai", os.getenv("OPENAI_API_KEY"), "gpt-4o")
    except CredentialValidationError as e:
        assert "Invalid OpenAI API key" not in e.message, f"Real key was rejected as invalid: {e.message}"
