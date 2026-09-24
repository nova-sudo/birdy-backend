"""
tests/test_slack_reconnect.py
-----------------------------
What Birdy does when Slack stops accepting an installation's token.

`installed` only ever meant "a row exists in the database". Nothing checked
that the token in it still worked, and the first screen that tried was the
channel picker — three steps after onboarding had already announced "Slack
connected" and waved the user on. On the account that surfaced this, Slack was
answering `invalid_auth`, and the picker reported it as a missing channel
scope: it sent someone to re-grant a permission they already had, and stayed
broken afterwards.

So two things are pinned. A refused token has to be remembered, so every
screen after it stops claiming the integration is healthy. And it has to be
told apart from a Slack outage, because flagging a blip asks the user to redo
an OAuth hop for nothing.
"""

import pytest

from core.database import DB_NAME
from services import slack_bot_service as svc

USER = "owner@example.com"
TEAM = "T_TEST"


@pytest.fixture
def db(mock_mongo_client):
    return mock_mongo_client[DB_NAME]


async def _install(db, **extra):
    await db["users"].insert_one({
        "user_id": USER,
        "integrations": {"slack_bot": {
            "team_id": TEAM, "team_name": "Acme", "bot_user_id": "U1",
            "bot_token_encrypted": "x", **extra,
        }},
    })


async def test_a_healthy_install_is_not_flagged(db):
    await _install(db)

    status = await svc.get_slack_bot_status(db, USER)

    assert status["needs_reconnect"] is False


async def test_a_refused_token_is_remembered(db):
    await _install(db)

    await svc.mark_needs_reconnect(db, USER, "invalid_auth")
    status = await svc.get_slack_bot_status(db, USER)

    assert status["needs_reconnect"] is True
    assert status["reconnect_reason"] == "invalid_auth"


async def test_the_install_survives_being_flagged(db):
    """Flagging must not delete the installation. It still holds the team and
    the channel the user picked, and losing those would mean reconnecting cost
    them their configuration on top of the OAuth hop."""
    await _install(db, notify_channel_id="C1", notify_channel_name="general")

    await svc.mark_needs_reconnect(db, USER, "token_revoked")
    status = await svc.get_slack_bot_status(db, USER)

    assert status["team_id"] == TEAM
    assert status["notify_channel_id"] == "C1"


@pytest.mark.parametrize("code", sorted(svc.DEAD_TOKEN_ERRORS))
def test_every_dead_token_error_is_terminal(code):
    """Each of these means the token will not work again however often it is
    retried — which is what separates them from a rate limit."""
    assert code in svc.DEAD_TOKEN_ERRORS


def test_transient_slack_failures_are_not_dead_tokens():
    """A blip must not raise a badge telling someone to redo an OAuth hop."""
    for code in ("ratelimited", "service_unavailable", "fatal_error", "internal_error"):
        assert code not in svc.DEAD_TOKEN_ERRORS


async def test_reconnecting_clears_the_flag(db):
    await _install(db)
    await svc.mark_needs_reconnect(db, USER, "invalid_auth")

    await svc.save_slack_bot_installation(
        db, USER, team_id=TEAM, bot_token="new-token", bot_user_id="U1", team_name="Acme",
    )
    status = await svc.get_slack_bot_status(db, USER)

    assert status["needs_reconnect"] is False
    assert status["reconnect_reason"] is None


async def test_reconnecting_keeps_the_channel_already_chosen(db):
    """The install write replaces the whole sub-document, so a reinstall used
    to clear where Birdy posts — briefs would just stop arriving, with the
    integration still showing as connected. Survivable while reconnecting was
    rare; it stopped being rare once a refused token started asking for it."""
    await _install(db, notify_channel_id="C1", notify_channel_name="general")

    await svc.save_slack_bot_installation(
        db, USER, team_id=TEAM, bot_token="new-token", bot_user_id="U1", team_name="Acme",
    )
    status = await svc.get_slack_bot_status(db, USER)

    assert status["notify_channel_id"] == "C1"
    assert status["notify_channel_name"] == "general"


async def test_connecting_a_different_workspace_does_not_inherit_its_channel(db):
    """A channel id belongs to the workspace it came from. Carrying it into a
    different team would point Birdy at a channel that does not exist there."""
    await _install(db, notify_channel_id="C1", notify_channel_name="general")

    await svc.save_slack_bot_installation(
        db, USER, team_id="T_OTHER", bot_token="t", bot_user_id="U2", team_name="Other",
    )
    status = await svc.get_slack_bot_status(db, USER)

    assert status["team_id"] == "T_OTHER"
    assert status["notify_channel_id"] is None
