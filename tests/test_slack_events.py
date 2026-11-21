"""
routers/slack_events.py — the Events API webhook. Tests the route via a
real (in-process, no socket) HTTP round-trip against a minimal throwaway
FastAPI app containing just this router, with every DB/Slack/LLM-touching
function mocked — the point is proving the routing/dispatch logic
(url_verification, app_mention/DM handling, bot_id/edit/retry skips, unknown
team_id no-op), not re-testing run_chat or the Slack SDK themselves.
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
from unittest.mock import MagicMock

from routers import slack_events

SIGNING_SECRET = "test-events-secret"


def _sign(body: bytes, ts: str) -> str:
    basestring = f"v0:{ts}:{body.decode()}".encode()
    return "v0=" + hmac.new(SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(slack_events.router)
    return a


@pytest.fixture
def signed_post(app):
    """Returns an async fn(payload_dict) -> httpx.Response with a valid Slack signature."""
    async def _post(payload: dict, extra_headers: dict | None = None):
        body = json.dumps(payload).encode()
        ts = str(int(time.time()))
        headers = {
            "X-Slack-Signature": _sign(body, ts),
            "X-Slack-Request-Timestamp": ts,
            "Content-Type": "application/json",
            **(extra_headers or {}),
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/slack/events", content=body, headers=headers)
    return _post


@pytest.fixture(autouse=True)
def signing_secret(monkeypatch):
    monkeypatch.setattr("services.slack_signature.SLACK_SIGNING_SECRET", SIGNING_SECRET)


@asynccontextmanager
async def _fake_mongo_client():
    # get_db(mongo_client) does mongo_client[DB_NAME] — a MagicMock tolerates
    # subscripting. The actual db object is never touched since every
    # function that would use it is mocked in these tests.
    yield MagicMock()


@pytest.mark.asyncio
async def test_url_verification_handshake(signed_post):
    resp = await signed_post({"type": "url_verification", "challenge": "abc123"})
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "abc123"}


@pytest.mark.asyncio
async def test_retry_delivery_is_skipped(signed_post):
    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client):
        resp = await signed_post(
            {"type": "event_callback", "team_id": "T1", "event": {"type": "app_mention"}},
            extra_headers={"X-Slack-Retry-Num": "1"},
        )
    assert resp.status_code == 200
    assert resp.json() == {}


@pytest.mark.asyncio
async def test_bot_message_is_ignored(signed_post):
    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock()) as mock_lookup:
        resp = await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {"type": "message", "channel_type": "im", "bot_id": "B123", "text": "hi"},
        })
    assert resp.status_code == 200
    mock_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_team_id_is_a_noop(signed_post):
    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value=None)), \
         patch("routers.slack_events.get_provider_for_user", AsyncMock()) as mock_provider:
        resp = await signed_post({
            "type": "event_callback", "team_id": "T_UNKNOWN",
            "event": {"type": "app_mention", "channel": "C1", "ts": "1.1", "text": "<@BOT> hi", "user": "U1"},
        })
    assert resp.status_code == 200
    mock_provider.assert_not_called()


@pytest.mark.asyncio
async def test_app_mention_strips_mention_prefix_and_calls_run_chat(signed_post):
    fake_provider = object()
    mock_run_chat = AsyncMock(return_value={"reply": "The answer is 42.", "tools_used": [], "session_id": "chat_new123"})
    mock_post_message = AsyncMock(return_value={"ts": "ack-ts-123"})
    mock_update = AsyncMock()

    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")), \
         patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_events.get_provider_for_user", AsyncMock(return_value=fake_provider)), \
         patch("routers.slack_events.get_thread_session_id", AsyncMock(return_value=None)), \
         patch("routers.slack_events.save_thread_session_id", AsyncMock()) as mock_save_session, \
         patch("routers.slack_events.run_chat", mock_run_chat), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", mock_update):
        resp = await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {
                "type": "app_mention", "channel": "C1", "ts": "1111.2222",
                "text": "<@U0BOTID> what is the answer?", "user": "U_HUMAN",
            },
        })

    assert resp.status_code == 200
    mock_run_chat.assert_awaited_once()
    call_kwargs = mock_run_chat.call_args.kwargs
    assert call_kwargs["message"] == "what is the answer?"  # mention prefix stripped
    assert call_kwargs["user_id"] == "alice@example.com"
    assert call_kwargs["session_id"] is None  # first turn in this thread

    # Ack posted immediately, once, as a new message in the thread
    mock_post_message.assert_awaited_once()
    ack_kwargs = mock_post_message.call_args.kwargs
    assert ack_kwargs["channel"] == "C1"
    assert ack_kwargs["thread_ts"] == "1111.2222"
    assert "Thinking" in ack_kwargs["text"]

    # Real reply edits that same ack message in place, not a second post
    mock_update.assert_awaited_once()
    update_kwargs = mock_update.call_args.kwargs
    assert update_kwargs["channel"] == "C1"
    assert update_kwargs["ts"] == "ack-ts-123"
    assert "The answer is 42." in update_kwargs["text"]

    mock_save_session.assert_awaited_once()
    assert mock_save_session.call_args.args[-1] == "chat_new123"


@pytest.mark.asyncio
async def test_ack_posted_before_run_chat_even_starts(signed_post):
    """The 'thinking' ack must be visible immediately, not after run_chat
    finishes — otherwise it's not actually an ack."""
    call_order = []
    mock_run_chat = AsyncMock(return_value={"reply": "done", "tools_used": [], "session_id": "chat_x"})
    mock_run_chat.side_effect = lambda **kw: call_order.append("run_chat") or mock_run_chat.return_value

    async def _fake_post(**kwargs):
        call_order.append("post_message")
        return {"ts": "ack-ts"}

    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")), \
         patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_events.get_provider_for_user", AsyncMock(return_value=object())), \
         patch("routers.slack_events.get_thread_session_id", AsyncMock(return_value=None)), \
         patch("routers.slack_events.save_thread_session_id", AsyncMock()), \
         patch("routers.slack_events.run_chat", mock_run_chat), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", side_effect=_fake_post), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", AsyncMock()):
        await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {"type": "app_mention", "channel": "C1", "ts": "1.1", "text": "<@BOT> hi", "user": "U1"},
        })

    assert call_order == ["post_message", "run_chat"]


@pytest.mark.asyncio
async def test_ack_post_failure_falls_back_to_plain_post_for_final_reply(signed_post):
    """If the ack itself fails to post, the real reply must still go out as a
    fresh message rather than being silently lost."""
    mock_run_chat = AsyncMock(return_value={"reply": "The answer is 42.", "tools_used": [], "session_id": "chat_x"})
    mock_post_message = AsyncMock(side_effect=[Exception("network blip"), {"ts": "ignored"}])

    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")), \
         patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_events.get_provider_for_user", AsyncMock(return_value=object())), \
         patch("routers.slack_events.get_thread_session_id", AsyncMock(return_value=None)), \
         patch("routers.slack_events.save_thread_session_id", AsyncMock()), \
         patch("routers.slack_events.run_chat", mock_run_chat), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message):
        resp = await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {"type": "app_mention", "channel": "C1", "ts": "1.1", "text": "<@BOT> hi", "user": "U1"},
        })

    assert resp.status_code == 200
    assert mock_post_message.await_count == 2  # failed ack attempt, then the real reply
    final_call_kwargs = mock_post_message.call_args.kwargs
    assert "The answer is 42." in final_call_kwargs["text"]


@pytest.mark.asyncio
async def test_dm_message_without_channel_type_im_is_ignored(signed_post):
    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock()) as mock_lookup:
        resp = await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {"type": "message", "channel_type": "channel", "text": "hi", "channel": "C1", "ts": "1.1"},
        })
    assert resp.status_code == 200
    mock_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_no_ai_credential_posts_helpful_message_instead_of_erroring(signed_post):
    from ai.provider_factory import NoAiCredentialError
    mock_post_message = AsyncMock(return_value={"ts": "ack-ts-123"})
    mock_update = AsyncMock()

    with patch("routers.slack_events.get_mongo_client", _fake_mongo_client), \
         patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")), \
         patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
         patch("routers.slack_events.get_provider_for_user", AsyncMock(side_effect=NoAiCredentialError())), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message), \
         patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", mock_update):
        resp = await signed_post({
            "type": "event_callback", "team_id": "T1",
            "event": {"type": "message", "channel_type": "im", "channel": "C1", "ts": "1.1", "text": "hi", "user": "U1"},
        })

    assert resp.status_code == 200
    mock_post_message.assert_awaited_once()  # just the ack
    mock_update.assert_awaited_once()  # the actual "connect your key" message
    assert "connect" in mock_update.call_args.kwargs["text"].lower()


# ── Slack talks to the agent team (ai/team): every message goes to Birdy the orchestrator ──

def _team_patches(mock_run_chat, mock_post_message, mock_update):
    return [
        patch("routers.slack_events.get_mongo_client", _fake_mongo_client),
        patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")),
        patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")),
        patch("routers.slack_events.get_provider_for_user", AsyncMock(return_value=object())),
        patch("routers.slack_events.get_thread_session_id", AsyncMock(return_value="chat_t1")),
        patch("routers.slack_events.save_thread_session_id", AsyncMock()),
        patch("credits.is_blocked", AsyncMock(return_value=False)),
        patch("routers.slack_events.run_chat", mock_run_chat),
        patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post_message),
        patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", mock_update),
    ]


async def _post_with(signed_post, patches, payload, headers=None):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return await signed_post(payload, extra_headers=headers)


def _dm(text):
    return {"type": "event_callback", "team_id": "T1",
            "event": {"type": "message", "channel_type": "im", "channel": "D1", "ts": "5.5", "text": text, "user": "U1"}}


DELEGATED = {
    "reply": "Calls are fine, the drop is in ad spend.",
    "tools_used": [], "session_id": "chat_t1",
    "route": {"status": "done", "seconds": 12.4, "credits": 41.2, "limit_hit": None, "steps": [
        {"agent": "birdy", "name": "Birdy", "status": "done", "asked_by": None, "tools": []},
        {"agent": "sales_coach", "name": "Sales Coach", "status": "done", "asked_by": "birdy", "tools": []},
    ]},
    "proposals": [{"id": "p_1", "agent": "media_buyer", "title": "Pause ad 'Spring promo' in Aura",
                   "detail": "CPL is 3x the account average over 7 days.", "kind": "pause_ad",
                   "status": "pending", "proposed_by": "sales_coach"}],
}


@pytest.mark.asyncio
async def test_message_goes_to_birdy_the_orchestrator(signed_post):
    mock_run_chat = AsyncMock(return_value={"reply": "Hi.", "tools_used": [], "session_id": "chat_t1"})
    await _post_with(signed_post, _team_patches(mock_run_chat, AsyncMock(return_value={"ts": "a"}), AsyncMock()),
                     _dm("how is Aura doing?"))
    kw = mock_run_chat.call_args.kwargs
    assert kw["agent"] == "birdy"
    assert kw["source"] == "slack"
    assert kw["session_id"] == "chat_t1"


@pytest.mark.asyncio
async def test_naming_an_agent_still_goes_to_birdy_with_the_words_kept(signed_post):
    mock_run_chat = AsyncMock(return_value={"reply": "Asked.", "tools_used": [], "session_id": "chat_t1"})
    await _post_with(signed_post, _team_patches(mock_run_chat, AsyncMock(return_value={"ts": "a"}), AsyncMock()),
                     _dm("media buyer, what should I pause in Aura?"))
    kw = mock_run_chat.call_args.kwargs
    assert kw["agent"] == "birdy"
    assert kw["message"] == "media buyer, what should I pause in Aura?"


@pytest.mark.asyncio
async def test_delegated_reply_shows_route_line_and_proposal_in_one_edit(signed_post):
    mock_post, mock_update = AsyncMock(return_value={"ts": "ack-1"}), AsyncMock()
    await _post_with(signed_post, _team_patches(AsyncMock(return_value=DELEGATED), mock_post, mock_update),
                     _dm("why are leads down?"))
    mock_post.assert_awaited_once()      # the ack only
    mock_update.assert_awaited_once()    # the reply replaces it, once
    blocks = mock_update.call_args.kwargs["blocks"]
    assert blocks[-1] == {"type": "context", "block_id": "team_route",
                          "elements": [{"type": "mrkdwn", "text": "Birdy asked Sales Coach · 12s · 41 credits"}]}
    texts = [b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"]
    assert any("Pause ad 'Spring promo' in Aura" in t and "CPL is 3x" in t for t in texts)
    note = next(b for b in blocks if b.get("block_id") == "team_proposals_note")["elements"][0]["text"]
    assert "approval in Birdy" in note and "/media-buyer/suggestions|" in note
    assert not any(b["type"] == "actions" for b in blocks)   # no approve buttons in Slack


@pytest.mark.asyncio
async def test_retry_delivery_never_runs_the_team(signed_post):
    mock_run_chat, mock_post, mock_update = AsyncMock(), AsyncMock(), AsyncMock()
    resp = await _post_with(signed_post, _team_patches(mock_run_chat, mock_post, mock_update),
                            _dm("hi"), headers={"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"})
    assert resp.json() == {}
    mock_run_chat.assert_not_called()
    mock_post.assert_not_called()
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_out_of_credits_never_runs_the_team(signed_post):
    mock_run_chat, mock_update = AsyncMock(), AsyncMock()
    patches = _team_patches(mock_run_chat, AsyncMock(return_value={"ts": "a"}), mock_update)
    patches = [p for p in patches if getattr(p, "attribute", None) != "is_blocked"]
    patches.append(patch("credits.is_blocked", AsyncMock(return_value=True)))
    await _post_with(signed_post, patches, _dm("hi"))
    mock_run_chat.assert_not_called()
    assert "out of Birdy Credits" in mock_update.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_end_to_end_team_turn_bills_once_and_archives_route_and_proposal(signed_post, mock_db, mock_mongo_client,
                                                                               monkeypatch):
    """The real run_chat behind the Slack route, on mongomock, with a scripted provider: Birdy asks
    Media Buyer, which proposes a change. One debit for the whole reply (source slack, with the
    per-agent breakdown), the archive row carries the route and the proposal, the app lists the
    proposal as pending, and the Slack reply ends with the route line."""
    import credits
    from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall, Usage
    from ai.team import proposals as team_proposals
    from ai.tools.registry import ToolRegistry

    def call(name, **args):
        return ProviderResponse(content=None, tool_calls=[ToolCall(id=f"tc-{name}", name=name, arguments=json.dumps(args))],
                                finish_reason="tool_calls", usage=Usage(1000, 100))

    def say(text):
        return ProviderResponse(content=text, usage=Usage(1000, 100))

    class Scripted(BaseLLMProvider):
        model = "gpt-4o"

        def __init__(self):
            self.scripts = {
                "Your role: Birdy": [call("ask_media_buyer", question="Why is Aura's CPL up?", client="agency"),
                                     say("One ad doubled its CPL. Media Buyer proposed pausing it.")],
                "Your role: Media Buyer": [call("propose_action", title="Pause ad 'Spring promo'",
                                                detail="CPL doubled in 7 days", kind="other"),
                                           say("One ad doubled its CPL.\nSUMMARY: 1 ad checked")],
            }

        async def chat_completion(self, messages, tools=None, **kw):
            system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            for marker, script in self.scripts.items():
                if marker in system and script:
                    return script.pop(0)
            return say("nothing more")

    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    credits._settings_cache["data"] = None
    await mock_db["users"].insert_one({"user_id": "alice@example.com",
                                       "credits": {**credits._effective_credits({})[0], "topup_balance": 1000.0}})

    @asynccontextmanager
    async def _client():
        yield mock_mongo_client

    mock_post, mock_update = AsyncMock(return_value={"ts": "ack-1"}), AsyncMock()
    try:
        with patch("routers.slack_events.get_mongo_client", _client), \
             patch("routers.slack_events.get_user_id_by_team_id", AsyncMock(return_value="alice@example.com")), \
             patch("routers.slack_events.get_decrypted_bot_token_for_user", AsyncMock(return_value="xoxb-fake")), \
             patch("routers.slack_events.get_provider_for_user", AsyncMock(return_value=Scripted())), \
             patch("routers.slack_events.registry", ToolRegistry()), \
             patch("slack_sdk.web.async_client.AsyncWebClient.chat_postMessage", mock_post), \
             patch("slack_sdk.web.async_client.AsyncWebClient.chat_update", mock_update):
            await signed_post(_dm("media buyer, why is Aura's CPL up?"))
    finally:
        credits._settings_cache["data"] = None

    usage = await mock_db["ai_usage"].find({}).to_list(None)
    assert len(usage) == 1
    row = usage[0]
    assert row["source"] == "slack" and row["agent"] == "birdy" and row["feature"] == "ask_birdy"
    assert [a["agent"] for a in row["agents"]] == ["birdy", "media_buyer"]

    archived = await mock_db["ai_conversation_log"].find_one({"role": "assistant"})
    assert archived["source"] == "slack" and archived["agent"] == "birdy"
    assert [s["agent"] for s in archived["route"]["steps"]] == ["birdy", "media_buyer"]
    pending = await team_proposals.list_proposals(mock_db, "alice@example.com")
    assert [p["title"] for p in pending] == ["Pause ad 'Spring promo'"]

    mock_update.assert_awaited_once()
    blocks = mock_update.call_args.kwargs["blocks"]
    line = blocks[-1]["elements"][0]["text"]
    assert line.startswith("Birdy asked Media Buyer · ") and line.endswith(" credits")
    assert any("Pause ad 'Spring promo'" in (b.get("text") or {}).get("text", "") for b in blocks)
    # the thread now points at this session, so the next message continues it
    saved = await mock_db["slack_thread_sessions"].find_one({"_id": "T1:D1:5.5"})
    assert saved["session_id"] == archived["session_id"]
