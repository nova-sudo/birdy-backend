"""services/slack_bot_service.py — Slack bot installation CRUD, the
critical team_id -> user_id lookup, the one-workspace-one-account conflict
guard, and the thread -> session mapping (required because
ai/session_store.py::get_or_create() only reuses a session_id that's
already a live key in its in-process dict)."""

import pytest

from services import slack_bot_service as svc


@pytest.mark.asyncio
async def test_save_then_status_never_exposes_bot_token(mock_db):
    saved = await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-secret",
        bot_user_id="U999", team_name="Acme Inc",
    )
    assert saved["team_id"] == "T123"
    assert "bot_token" not in saved and "bot_token_encrypted" not in saved

    status = await svc.get_slack_bot_status(mock_db, "alice@example.com")
    assert status["team_id"] == "T123"
    assert status["team_name"] == "Acme Inc"
    assert "bot_token" not in status and "bot_token_encrypted" not in status


@pytest.mark.asyncio
async def test_status_none_when_not_installed(mock_db):
    assert await svc.get_slack_bot_status(mock_db, "nobody@example.com") is None


@pytest.mark.asyncio
async def test_get_user_id_by_team_id_resolves_and_returns_none_for_unknown(mock_db):
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-secret", bot_user_id="U999",
    )
    assert await svc.get_user_id_by_team_id(mock_db, "T123") == "alice@example.com"
    assert await svc.get_user_id_by_team_id(mock_db, "T_UNKNOWN") is None


@pytest.mark.asyncio
async def test_bot_token_encrypt_decrypt_round_trip(mock_db):
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-real-secret-value", bot_user_id="U999",
    )
    token = await svc.get_decrypted_bot_token_for_user(mock_db, "alice@example.com")
    assert token == "xoxb-real-secret-value"


@pytest.mark.asyncio
async def test_decrypted_token_none_when_not_installed(mock_db):
    assert await svc.get_decrypted_bot_token_for_user(mock_db, "nobody@example.com") is None


@pytest.mark.asyncio
async def test_reinstall_by_same_user_is_idempotent(mock_db):
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-old", bot_user_id="U999",
    )
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-new", bot_user_id="U999",
    )
    token = await svc.get_decrypted_bot_token_for_user(mock_db, "alice@example.com")
    assert token == "xoxb-new"


@pytest.mark.asyncio
async def test_conflicting_team_id_rejected_for_different_user(mock_db):
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-alice", bot_user_id="U999",
    )
    with pytest.raises(svc.SlackTeamAlreadyLinkedError):
        await svc.save_slack_bot_installation(
            mock_db, "bob@example.com", team_id="T123", bot_token="xoxb-bob", bot_user_id="U000",
        )
    # Alice's installation must be untouched
    assert await svc.get_user_id_by_team_id(mock_db, "T123") == "alice@example.com"


@pytest.mark.asyncio
async def test_remove_then_status_and_lookup_are_gone(mock_db):
    await svc.save_slack_bot_installation(
        mock_db, "alice@example.com", team_id="T123", bot_token="xoxb-secret", bot_user_id="U999",
    )
    assert await svc.remove_slack_bot_installation(mock_db, "alice@example.com") is True
    assert await svc.get_slack_bot_status(mock_db, "alice@example.com") is None
    assert await svc.get_user_id_by_team_id(mock_db, "T123") is None


@pytest.mark.asyncio
async def test_remove_returns_false_for_unknown_user(mock_db):
    assert await svc.remove_slack_bot_installation(mock_db, "nobody@example.com") is False


# ── Thread -> session mapping ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_thread_session_none_before_first_turn(mock_db):
    assert await svc.get_thread_session_id(mock_db, "T1", "C1", "1234.5678") is None


@pytest.mark.asyncio
async def test_thread_session_reused_across_calls(mock_db):
    await svc.save_thread_session_id(mock_db, "T1", "C1", "1234.5678", "alice@example.com", "chat_abcdef")
    assert await svc.get_thread_session_id(mock_db, "T1", "C1", "1234.5678") == "chat_abcdef"

    # A different thread in the same channel must not collide
    assert await svc.get_thread_session_id(mock_db, "T1", "C1", "9999.0000") is None
