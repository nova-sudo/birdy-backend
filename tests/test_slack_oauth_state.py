"""routers/slack.py's OAuth install `state` param — the CSRF defense for the
callback (verified server-side rather than relying on cookie survival
through the Slack redirect, unlike the existing GHL/Meta callbacks)."""

import time

import jwt as pyjwt
import pytest

from core.config import JWT_SECRET, JWT_ALGORITHM
from routers.slack import _mint_install_state, _verify_install_state


def test_mint_then_verify_round_trips():
    state = _mint_install_state("alice@example.com")
    assert _verify_install_state(state) == "alice@example.com"


def test_wrong_purpose_rejected():
    token = pyjwt.encode(
        {"sub": "alice@example.com", "purpose": "not_slack_install", "exp": int(time.time()) + 600},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ValueError, match="Wrong token purpose"):
        _verify_install_state(token)


def test_missing_purpose_rejected():
    token = pyjwt.encode(
        {"sub": "alice@example.com", "exp": int(time.time()) + 600},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(ValueError, match="Wrong token purpose"):
        _verify_install_state(token)


def test_expired_token_rejected():
    token = pyjwt.encode(
        {"sub": "alice@example.com", "purpose": "slack_install", "exp": int(time.time()) - 10},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        _verify_install_state(token)


def test_tampered_signature_rejected():
    state = _mint_install_state("alice@example.com")
    with pytest.raises(pyjwt.InvalidSignatureError):
        _verify_install_state(state[:-1] + ("A" if state[-1] != "A" else "B"))
