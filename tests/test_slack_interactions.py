"""
routers/slack_interactions.py — block_actions (inline field accumulation ->
submit -> reconstructed [UI_RESPONSE] JSON) and view_submission (modal
answers -> same reconstruction). Uses the real (mongomock) DB for the
pending-interaction store, since that's the whole point of this handler,
and mocks run_chat/AsyncWebClient the same way tests/test_slack_events.py does.
"""

import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from routers import slack_interactions
from services.slack_interaction_store import create_pending_interaction, get_pending_interaction

SIGNING_SECRET = "test-interactions-secret"


def _sign(body: bytes, ts: str) -> str:
    basestring = f"v0:{ts}:{body.decode()}".encode()
    return "v0=" + hmac.new(SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(slack_interactions.router)
    return a


@pytest.fixture
def signed_post(app):
    async def _post(payload_dict: dict):
        # httpx url-encodes form data itself when given a dict; here we hand-encode
        # to control the exact body bytes the signature is computed over.
        import urllib.parse
        body = f"payload={urllib.parse.quote(json.dumps(payload_dict))}".encode()
        ts = str(int(time.time()))
        headers = {
            "X-Slack-Signature": _sign(body, ts),
            "X-Slack-Request-Timestamp": ts,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/slack/interactions", content=body, headers=headers)
    return _post


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setattr("services.slack_signature.SLACK_SIGNING_SECRET", SIGNING_SECRET)


@pytest.fixture
def db_provider(mock_db, mock_mongo_client):
    # The route calls get_db(mongo_client) itself (mongo_client[DB_NAME]), so
    # the fake context manager must yield the raw CLIENT, not the
    # already-selected database — mock_db is mock_mongo_client[DB_NAME].
    @asynccontextmanager
    async def _fake_mongo_client():
        yield mock_mongo_client
    with patch("routers.slack_interactions.get_mongo_client", _fake_mongo_client):
        yield mock_db


FIELDS = [
    {"id": "type", "type": "radio", "label": "Type", "required": True,
     "options": [{"value": "warning", "label": "Warning"}]},
    {"id": "period", "type": "select", "label": "Period",
     "options": [{"value": "week", "label": "This week"}]},
]


@pytest.mark.asyncio
async def test_block_actions_field_change_accumulates_answer(signed_post, db_provider):
    await create_pending_interaction(
        db_provider, "chat_abc:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_abc",
        fields=FIELDS, mode="inline",
    )
    resp = await signed_post({
        "type": "block_actions", "user": {"id": "U1"}, "trigger_id": "trig123",
        "actions": [{"block_id": "ui|chat_abc:0|type", "type": "radio_buttons",
                      "selected_option": {"value": "warning"}}],
    })
    assert resp.status_code == 200
    doc = await get_pending_interaction(db_provider, "chat_abc:0")
    assert doc["answers"] == {"type": "warning"}


@pytest.mark.asyncio
async def test_submit_missing_required_field_does_not_call_run_chat(signed_post, db_provider):
    await create_pending_interaction(
        db_provider, "chat_abc:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_abc",
        fields=FIELDS, mode="inline",
    )
    # required "type" field never answered
    mock_post_message = AsyncMock()
    with patch("routers.slack_interactions.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message):
        resp = await signed_post({
            "type": "block_actions", "user": {"id": "U1"},
            "actions": [{"block_id": "ui|chat_abc:0|__submit__", "type": "button", "value": "chat_abc:0"}],
            "message": {"ts": "1.1"},
        })
    assert resp.status_code == 200
    mock_post_message.assert_awaited_once()
    assert "required" in mock_post_message.call_args.kwargs["text"].lower()
    # Pending doc must still exist — not consumed by a failed submit
    assert await get_pending_interaction(db_provider, "chat_abc:0") is not None


@pytest.mark.asyncio
async def test_submit_with_all_answers_reconstructs_ui_response_and_calls_run_chat(signed_post, db_provider):
    await create_pending_interaction(
        db_provider, "chat_abc:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_abc",
        fields=FIELDS, mode="inline",
    )
    from services.slack_interaction_store import record_answers
    await record_answers(db_provider, "chat_abc:0", {"type": "warning", "period": "week"})

    fake_provider = object()
    mock_run_chat = AsyncMock(return_value={"reply": "Alert created!", "tools_used": [], "session_id": "chat_abc"})
    mock_post_message = AsyncMock()
    mock_update = AsyncMock()

    with patch("routers.slack_interactions.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_interactions.get_provider_for_user", AsyncMock(return_value=fake_provider)), \
         patch("routers.slack_interactions.run_chat", mock_run_chat), \
         patch("routers.slack_interactions.save_thread_session_id", AsyncMock()), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", mock_update):
        resp = await signed_post({
            "type": "block_actions", "user": {"id": "U1"},
            "actions": [{"block_id": "ui|chat_abc:0|__submit__", "type": "button", "value": "chat_abc:0"}],
            "message": {"ts": "1.1"},
        })

    assert resp.status_code == 200
    mock_run_chat.assert_awaited_once()
    sent_message = mock_run_chat.call_args.kwargs["message"]
    assert sent_message.startswith("[UI_RESPONSE] ")
    assert json.loads(sent_message[len("[UI_RESPONSE] "):]) == {"type": "warning", "period": "week"}

    mock_update.assert_awaited_once()  # original message replaced with "Submitted"
    mock_post_message.assert_awaited_once()
    assert "Alert created!" in mock_post_message.call_args.kwargs["text"]

    # Pending doc consumed — prevents double-submit
    assert await get_pending_interaction(db_provider, "chat_abc:0") is None


@pytest.mark.asyncio
async def test_open_modal_button_calls_views_open_with_trigger_id(signed_post, db_provider):
    modal_fields = [{"id": "name", "type": "text", "label": "Name", "required": True}]
    await create_pending_interaction(
        db_provider, "chat_xyz:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_xyz",
        fields=modal_fields, mode="modal",
    )
    mock_views_open = AsyncMock()
    with patch("routers.slack_interactions.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("slack_sdk.web.async_client.AsyncWebClient.views_open", mock_views_open):
        resp = await signed_post({
            "type": "block_actions", "user": {"id": "U1"}, "trigger_id": "trig456",
            "actions": [{"block_id": "ui|chat_xyz:0|__open_modal__", "type": "button", "value": "chat_xyz:0"}],
        })
    assert resp.status_code == 200
    mock_views_open.assert_awaited_once()
    assert mock_views_open.call_args.kwargs["trigger_id"] == "trig456"
    assert mock_views_open.call_args.kwargs["view"]["private_metadata"] == "chat_xyz:0"


@pytest.mark.asyncio
async def test_view_submission_missing_required_returns_errors_response(signed_post, db_provider):
    modal_fields = [{"id": "name", "type": "text", "label": "Name", "required": True}]
    await create_pending_interaction(
        db_provider, "chat_xyz:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_xyz",
        fields=modal_fields, mode="modal",
    )
    resp = await signed_post({
        "type": "view_submission",
        "view": {"private_metadata": "chat_xyz:0", "state": {"values": {
            "name": {"value": {"type": "plain_text_input", "value": ""}},
        }}},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["response_action"] == "errors"
    assert "name" in body["errors"]
    # Not consumed — user can fix and resubmit
    assert await get_pending_interaction(db_provider, "chat_xyz:0") is not None


@pytest.mark.asyncio
async def test_view_submission_success_reconstructs_ui_response(signed_post, db_provider):
    modal_fields = [{"id": "name", "type": "text", "label": "Name", "required": True}]
    await create_pending_interaction(
        db_provider, "chat_xyz:0", team_id="T1", channel_id="C1", thread_ts="1.1",
        slack_user_id="U1", birdy_user_id="alice@example.com", session_id="chat_xyz",
        fields=modal_fields, mode="modal",
    )
    fake_provider = object()
    mock_run_chat = AsyncMock(return_value={"reply": "Done!", "tools_used": [], "session_id": "chat_xyz"})
    mock_post_message = AsyncMock()

    with patch("routers.slack_interactions.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_interactions.get_provider_for_user", AsyncMock(return_value=fake_provider)), \
         patch("routers.slack_interactions.run_chat", mock_run_chat), \
         patch("routers.slack_interactions.save_thread_session_id", AsyncMock()), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message):
        resp = await signed_post({
            "type": "view_submission",
            "view": {"private_metadata": "chat_xyz:0", "state": {"values": {
                "name": {"value": {"type": "plain_text_input", "value": "My Alert"}},
            }}},
        })

    assert resp.status_code == 200
    assert resp.json() == {}
    sent_message = mock_run_chat.call_args.kwargs["message"]
    assert json.loads(sent_message[len("[UI_RESPONSE] "):]) == {"name": "My Alert"}
    assert await get_pending_interaction(db_provider, "chat_xyz:0") is None
