"""Chat attachments: validation, session storage, vision parts for one turn, and the
draft_creative_test(attachment_index=...) read path."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import routers.chat as chat_router
from ai.orchestrator import run_chat
from ai.providers.anthropic_provider import AnthropicProvider
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
from ai.tools import creative_tools as t
from ai.tools.registry import ToolRegistry
from core.models import ChatAttachment, ChatRequest
from services.creatives import store
from test_creative_tools import DRAFT_ARGS, adset, group, meta  # noqa: F401  (meta is a fixture)

U = "u1@example.com"
HOST = "https://x.public.blob.vercel-storage.com"
IMG = {"blob_url": f"{HOST}/a.png", "mime": "image/png", "bytes": 1000, "width": 1080, "height": 1350}
VID = {"blob_url": f"{HOST}/v.mp4", "mime": "video/mp4", "bytes": 5000, "duration_s": 27,
       "thumbnail_url": f"{HOST}/v.jpg"}


class Scripted(BaseLLMProvider):
    def __init__(self, responses, vision):
        self._r, self.model, self.supports_vision, self.seen = list(responses), "stub", vision, []

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
        self.seen.append(json.loads(json.dumps(messages)))
        return self._r.pop(0)


@pytest.fixture(autouse=True)
def no_mcp(monkeypatch):
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)


def reg():
    r = ToolRegistry()
    t.register_creative_tools(r)
    return r


# ── validation ────────────────────────────────────────────────────────────────

def test_non_blob_host_is_422(monkeypatch):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[chat_router.get_current_user] = lambda: U
    client = TestClient(app)
    for url in ("https://evil.com/a.png", "https://evil.com/?x=.public.blob.vercel-storage.com",
                "https://a.public.blob.vercel-storage.com.evil.com/a.png",
                "http://x.public.blob.vercel-storage.com/a.png"):
        body = {"message": "hi", "attachments": [{**IMG, "blob_url": url}]}
        assert client.post("/api/chat", json=body).status_code == 422, url


def test_limits_and_types():
    assert ChatRequest(message="m", attachments=[IMG, VID]).attachments[1].duration_s == 27
    with pytest.raises(ValidationError):
        ChatRequest(message="m", attachments=[IMG] * 5)
    with pytest.raises(ValidationError):
        ChatAttachment(**{**IMG, "mime": "image/gif"})
    with pytest.raises(ValidationError):
        ChatAttachment(**{**IMG, "bytes": 31 * 1024 * 1024})
    with pytest.raises(ValidationError):
        ChatAttachment(**{**IMG, "thumbnail_url": "https://evil.com/t.jpg"})
    ChatAttachment(**{**VID, "bytes": 500 * 1024 * 1024})


# ── history / vision ──────────────────────────────────────────────────────────

async def test_history_keeps_summary_not_parts(mock_db):
    p = Scripted([ProviderResponse(content="ok")], vision=True)
    out = await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="look",
                         attachments=[IMG, VID])
    sent = p.seen[0][-1]["content"]
    assert [x["type"] for x in sent] == ["text", "image_url", "image_url"]
    assert sent[2]["image_url"]["url"] == VID["thumbnail_url"]
    doc = await mock_db["ai_chat_sessions"].find_one({"_id": out["session_id"]})
    user_msg = next(m for m in doc["messages"] if m["role"] == "user")
    assert user_msg["content"] == "look\n[Attached: 1 image (1080×1350), 1 video (0:27) \u2014 attachment_index 0, 1]"
    assert doc["session_attachments"] == [IMG, VID]


async def test_non_vision_provider_gets_text_only(mock_db):
    p = Scripted([ProviderResponse(content="ok")], vision=False)
    await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="look", attachments=[IMG])
    assert p.seen[0][-1]["content"] == "look\n[Attached: 1 image (1080×1350) \u2014 attachment_index 0]"


async def test_parts_only_for_this_turn(mock_db):
    p = Scripted([ProviderResponse(content="a"), ProviderResponse(content="b")], vision=True)
    first = await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="one",
                           attachments=[IMG])
    await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="two",
                   session_id=first["session_id"])
    prior = [m for m in p.seen[1] if m["role"] == "user"][0]
    assert isinstance(prior["content"], str)


async def test_invalid_attachment_not_persisted(mock_db):
    p = Scripted([ProviderResponse(content="ok")], vision=True)
    with pytest.raises(ValidationError):
        await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="x",
                       attachments=[{**IMG, "blob_url": "https://evil.com/a.png"}])
    assert await mock_db["ai_chat_sessions"].count_documents({"session_attachments": {"$exists": True}}) == 0


async def test_attachments_only_on_owners_session(mock_db):
    other = await run_chat(provider=Scripted([ProviderResponse(content="o")], True), tool_registry=reg(),
                           db=mock_db, user_id="o@x.com", message="hi")
    await run_chat(provider=Scripted([ProviderResponse(content="o")], True), tool_registry=reg(),
                   db=mock_db, user_id=U, message="hi", session_id=other["session_id"], attachments=[IMG])
    doc = await mock_db["ai_chat_sessions"].find_one({"_id": other["session_id"]})
    assert not doc.get("session_attachments")


def test_anthropic_conversion_emits_image_block():
    out = AnthropicProvider._convert_messages(AnthropicProvider.__new__(AnthropicProvider), [
        {"role": "user", "content": [{"type": "text", "text": "hi"},
                                     {"type": "image_url", "image_url": {"url": IMG["blob_url"]}}]}])
    assert out[0]["content"] == [{"type": "text", "text": "hi"},
                                 {"type": "image", "source": {"type": "url", "url": IMG["blob_url"]}}]


# ── end to end with draft_creative_test ───────────────────────────────────────

async def test_draft_reads_attachment_stored_by_run_chat(mock_db, meta):
    await group(mock_db, "g1", "Acme", "act_1", meta_campaign_ids=["c1"])
    meta.adsets["act_1"] = [adset(1)]
    args = {k: v for k, v in DRAFT_ARGS.items() if k != "session_id"}
    call = ProviderResponse(content=None, finish_reason="tool_calls", tool_calls=[
        ToolCall(id="c", name="draft_creative_test", arguments=json.dumps(args))])
    p = Scripted([call, ProviderResponse(content="drafted")], vision=True)
    r = ToolRegistry()
    t.register_creative_tools(r)
    out = await run_chat(provider=p, tool_registry=r, db=mock_db, user_id=U, message="test this",
                         page="campaigns", attachments=[IMG])
    tests = await mock_db[store.TESTS].find({"user_id": U}).to_list(10)
    assert out["reply"] == "drafted" and len(tests) == 1
    assert tests[0]["creative"]["media"]["blob_url"] == IMG["blob_url"]


async def test_indexes_on_second_turn_continue_session_positions(mock_db):
    p = Scripted([ProviderResponse(content="a"), ProviderResponse(content="b")], vision=False)
    first = await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="one",
                           attachments=[IMG, VID])
    await run_chat(provider=p, tool_registry=reg(), db=mock_db, user_id=U, message="two",
                   session_id=first["session_id"], attachments=[IMG, VID, IMG])
    assert p.seen[1][-1]["content"].endswith("\u2014 attachment_index 2, 3, 4]")
    doc = await mock_db["ai_chat_sessions"].find_one({"_id": first["session_id"]})
    assert len(doc["session_attachments"]) == 5
    assert doc["session_attachments"][2:] == [IMG, VID, IMG]
