"""
tests/test_lead_summary.py
--------------------------
services/lead_summary.py: the brief, the cache and the metering. AsyncOpenAI and
the data builders are mocked; nothing touches the network.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.lead_summary as ls
from core.database import DB_NAME

USER, CID = "u1", "c1"

DETAIL = {
    "contact": {"id": CID, "name": "Ada Lovelace", "group_name": "Acme Dental", "date_added": "2026-10-01",
                "source": "Facebook", "tags": [], "location_id": "loc1", "type": "lead"},
    "opportunities": [{"pipeline_name": "Sales", "stage_name": "Booked", "status": "open", "monetary_value": 900}],
    "attribution": {"platform": "fb", "ad_name": "Ad 1", "campaign_name": "Camp", "utm": {}},
    "calls": [{"direction": "outbound", "status": "completed", "duration_seconds": 61,
               "started_at": "2026-10-02T10:00:00+00:00", "transcript": "x" * 900}],
    "engagement": {"calls": 1, "dials": 1, "answered": 1, "touchpoints": 1, "time_to_close_seconds": None},
}
JOURNEY = {"events": [{"kind": "call", "title": "Outbound completed", "detail": "Call", "at": "2026-10-02T10:00:00+00:00"}]}
MESSAGES = {"status": "ok", "messages": [
    {"direction": "inbound", "channel": "SMS", "date": "2026-10-03T09:00:00+00:00", "body": "Hi there"}]}


@pytest.fixture
def env(mock_mongo_client, monkeypatch):
    db = mock_mongo_client[DB_NAME]
    state = SimpleNamespace(
        detail=AsyncMock(return_value=DETAIL),
        journey=AsyncMock(return_value=JOURNEY),
        messages=AsyncMock(return_value=MESSAGES),
        create=AsyncMock(),
        blocked=AsyncMock(return_value=False),
        usage=AsyncMock(),
    )
    state.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="  Ada came from a Facebook ad.  "))],
        usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40))
    client = MagicMock()
    client.chat.completions.create = state.create
    state.client_cls = MagicMock(return_value=client)
    monkeypatch.setattr(ls, "build_lead_detail", state.detail)
    monkeypatch.setattr(ls, "build_lead_journey", state.journey)
    monkeypatch.setattr(ls, "fetch_lead_messages", state.messages)
    monkeypatch.setattr(ls, "AsyncOpenAI", state.client_cls)
    monkeypatch.setattr(ls, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr("credits.is_blocked", state.blocked)
    monkeypatch.setattr("credits.record_usage", state.usage)
    state.db = db
    return state


# ── brief ────────────────────────────────────────────────────────────────────

def test_brief_omits_empty_sections():
    brief = ls.build_summary_input({"contact": {"name": "Ada"}}, None, None)
    assert "Ada" in brief
    for header in ("ATTRIBUTION", "PIPELINE", "ENGAGEMENT", "JOURNEY", "CALLS", "MESSAGES"):
        assert header not in brief


def test_brief_truncates_and_caps():
    detail = {"contact": {"name": "Ada"}, "calls": [
        {"direction": "inbound", "status": "completed", "transcript": f"call{i} " + "t" * 800} for i in range(8)]}
    journey = {"events": [{"kind": "k", "title": f"ev{i}", "detail": "d", "at": "2026-10-01T00:00:00+00:00"}
                          for i in range(40)]}
    messages = {"messages": [{"direction": "inbound", "channel": "SMS", "date": f"d{i}", "body": "b" * 500}
                             for i in range(30)]}
    brief = ls.build_summary_input(detail, journey, messages)
    assert brief.count("Transcript:") == 5 and "call5" not in brief
    assert "t" * 600 not in brief.replace("call0 ", "")  # clipped to 600 incl. prefix
    assert "ev24" in brief and "ev25" not in brief
    assert brief.count("Body:") == 20 and "d29" in brief and "d9;" not in brief
    assert "b" * 301 not in brief


def test_hash_is_stable_sha256():
    assert ls.input_hash("a") == ls.input_hash("a") != ls.input_hash("b")
    assert len(ls.input_hash("a")) == 64


# ── get_lead_summary ─────────────────────────────────────────────────────────

async def test_unknown_contact_is_none(env):
    env.detail.return_value = None
    assert await ls.get_lead_summary(env.db, USER, CID) is None
    env.create.assert_not_called()


async def test_first_view_generates_and_caches(env):
    out = await ls.get_lead_summary(env.db, USER, CID)
    assert out["status"] == "ok" and out["summary"] == "Ada came from a Facebook ad."
    assert out["stale"] is False and out["generated_at"]
    kwargs = env.create.call_args.kwargs
    assert kwargs["model"] == ls.LEAD_SUMMARY_MODEL and kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 220
    assert kwargs["messages"][0]["content"] == ls.SYSTEM_PROMPT
    assert "Ada Lovelace" in kwargs["messages"][1]["content"]
    assert env.client_cls.call_args.kwargs["timeout"] == 20
    doc = await env.db["lead_summaries"].find_one({"user_id": USER, "contact_id": CID})
    assert doc["summary"] == out["summary"] and doc["model"] == ls.LEAD_SUMMARY_MODEL
    assert len(doc["input_hash"]) == 64


async def test_second_view_uses_cache_without_model_or_charge(env):
    await ls.get_lead_summary(env.db, USER, CID)
    again = await ls.get_lead_summary(env.db, USER, CID)
    assert again["status"] == "ok" and again["stale"] is False
    assert env.create.await_count == 1 and env.usage.await_count == 1


async def test_cached_is_stale_when_inputs_change(env):
    await ls.get_lead_summary(env.db, USER, CID)
    env.messages.return_value = {"status": "ok", "messages": MESSAGES["messages"] + [
        {"direction": "inbound", "channel": "SMS", "date": "2026-10-04", "body": "Still keen"}]}
    out = await ls.get_lead_summary(env.db, USER, CID)
    assert out["stale"] is True and out["summary"] == "Ada came from a Facebook ad."
    assert env.create.await_count == 1


async def test_refresh_calls_model_and_updates_cache(env):
    await ls.get_lead_summary(env.db, USER, CID)
    env.create.return_value.choices[0].message.content = "Updated."
    out = await ls.get_lead_summary(env.db, USER, CID, refresh=True)
    assert out["summary"] == "Updated." and out["stale"] is False
    assert env.create.await_count == 2
    assert await env.db["lead_summaries"].count_documents({"user_id": USER, "contact_id": CID}) == 1
    doc = await env.db["lead_summaries"].find_one({"contact_id": CID})
    assert doc["summary"] == "Updated."


async def test_blocked_skips_model_and_keeps_cache(env):
    await ls.get_lead_summary(env.db, USER, CID)
    env.blocked.return_value = True
    out = await ls.get_lead_summary(env.db, USER, CID, refresh=True)
    assert out["status"] == "blocked" and out["summary"] == "Ada came from a Facebook ad."
    assert env.create.await_count == 1


async def test_blocked_with_nothing_cached(env):
    env.blocked.return_value = True
    out = await ls.get_lead_summary(env.db, USER, CID)
    assert out == {"status": "blocked", "summary": None, "generated_at": None, "stale": False}
    env.create.assert_not_called()


async def test_no_api_key_is_unavailable(env, monkeypatch):
    monkeypatch.setattr(ls, "OPENAI_API_KEY", None)
    out = await ls.get_lead_summary(env.db, USER, CID)
    assert out["status"] == "unavailable" and out["summary"] is None
    env.client_cls.assert_not_called()


async def test_model_error_is_failed_and_keeps_cache(env):
    await ls.get_lead_summary(env.db, USER, CID)
    env.create.side_effect = RuntimeError("boom with PII ada@example.com")
    out = await ls.get_lead_summary(env.db, USER, CID, refresh=True)
    assert out["status"] == "failed" and out["summary"] == "Ada came from a Facebook ad."
    assert env.usage.await_count == 1


async def test_usage_recorded_as_lead_summary(env):
    await ls.get_lead_summary(env.db, USER, CID)
    env.usage.assert_awaited_once()
    assert env.usage.call_args.args[1] == USER
    assert env.usage.call_args.kwargs == dict(
        model=ls.LEAD_SUMMARY_MODEL, prompt_tokens=300, completion_tokens=40,
        model_calls=1, mode="managed", feature="lead_summary")


async def test_usage_failure_does_not_break_result(env):
    env.usage.side_effect = RuntimeError("db down")
    assert (await ls.get_lead_summary(env.db, USER, CID))["status"] == "ok"


async def test_journey_and_messages_failures_drop_sections(env):
    env.journey.side_effect = RuntimeError("x")
    env.messages.return_value = {"status": "reconnect", "messages": []}
    out = await ls.get_lead_summary(env.db, USER, CID)
    assert out["status"] == "ok"
    brief = env.create.call_args.kwargs["messages"][1]["content"]
    assert "JOURNEY" not in brief and "MESSAGES" not in brief and "CALLS" in brief


async def test_no_location_skips_message_fetch(env):
    env.detail.return_value = {**DETAIL, "contact": {**DETAIL["contact"], "location_id": None}}
    await ls.get_lead_summary(env.db, USER, CID)
    env.messages.assert_not_called()


async def test_index_creator_declares_unique_index(mock_mongo_client):
    await ls.create_lead_summary_indexes(mock_mongo_client)
    info = await mock_mongo_client[DB_NAME]["lead_summaries"].index_information()
    assert any(v.get("unique") and v["key"] == [("user_id", 1), ("contact_id", 1)] for v in info.values())


def test_index_creator_registered():
    from core.indexes import _load_creators
    assert ls.create_lead_summary_indexes in _load_creators()
