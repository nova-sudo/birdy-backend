"""
tests/test_call_analysis.py
---------------------------
Call-recording analysis: scoping counts, filters, the confirmation gate, and
the analyze pass with cached transcripts (no network — OpenAI/httpx untouched).
"""

from datetime import datetime, timedelta

import pytest

from services import call_analysis_service
from ai.tools.call_analysis_tools import analyze_call_recordings

USER = "user-1"
GROUP = "grp-1"
LOC = "loc-1"


@pytest.fixture
def db(mock_db):
    return mock_db


async def _seed(db, calls):
    await db["client_groups"].insert_one({
        "id": GROUP, "user_id": USER, "name": "Acme",
        "ghl_location_id": LOC, "call_log_provider": "hotprospector",
    })
    now = datetime.utcnow()
    docs = []
    for i, c in enumerate(calls):
        docs.append({
            "user_id": USER,
            "location_id": LOC,
            "source": "hotprospector",
            "source_event_id": f"ev-{i}",
            "started_at": now - timedelta(days=c.get("days_ago", 1)),
            "direction": c.get("direction", "outbound"),
            "duration_seconds": c.get("duration", 120),
            "recording_url": c.get("url", f"https://rec.example/{i}.mp3"),
            "raw_payload": {"caller_name": c.get("agent", "Alice")},
            **({"transcript": c["transcript"]} if "transcript" in c else {}),
        })
    await db["call_logs"].insert_many(docs)


# ── summarize_analyzable_calls ──────────────────────────────────────────────

async def test_summary_counts_recordings_and_transcripts(db):
    await _seed(db, [
        {},                                   # recorded, not transcribed
        {"transcript": "hello"},              # recorded + transcribed
        {"url": None},                        # no recording
        {"duration": 5},                      # under min duration → excluded
    ])
    out = await call_analysis_service.summarize_analyzable_calls(db, USER, GROUP, preset="last_7d")
    assert out["matching_calls"] == 3        # 5s call filtered out
    assert out["with_recording"] == 2
    assert out["already_transcribed"] == 1
    assert out["total_recorded_minutes"] == 4.0
    assert out["by_agent"] == [{"agent": "Alice", "calls": 2, "minutes": 4.0}]
    assert out["by_direction"] == {"outbound": 2}


async def test_summary_filters_by_agent_and_direction(db):
    await _seed(db, [
        {"agent": "Alice", "direction": "outbound"},
        {"agent": "Bob", "direction": "inbound"},
        {"agent": "Bobby Junior", "direction": "outbound"},
    ])
    out = await call_analysis_service.summarize_analyzable_calls(db, USER, GROUP, agent_name="bob")
    assert out["with_recording"] == 2        # case-insensitive substring: Bob + Bobby
    out = await call_analysis_service.summarize_analyzable_calls(db, USER, GROUP, direction="inbound")
    assert out["with_recording"] == 1


async def test_summary_respects_date_window(db):
    await _seed(db, [{"days_ago": 1}, {"days_ago": 30}])
    out = await call_analysis_service.summarize_analyzable_calls(db, USER, GROUP, preset="last_7d")
    assert out["matching_calls"] == 1
    out = await call_analysis_service.summarize_analyzable_calls(db, USER, GROUP, preset="last_30d")
    assert out["matching_calls"] == 2


async def test_summary_unknown_group_raises(db):
    with pytest.raises(ValueError, match="not found"):
        await call_analysis_service.summarize_analyzable_calls(db, USER, "nope")


async def test_summary_group_without_location_raises(db):
    await db["client_groups"].insert_one({"id": "g2", "user_id": USER, "name": "NoLoc"})
    with pytest.raises(ValueError, match="no linked GHL location"):
        await call_analysis_service.summarize_analyzable_calls(db, USER, "g2")


# ── confirmation gate ───────────────────────────────────────────────────────

async def test_analyze_tool_refuses_without_confirmation(db):
    out = await analyze_call_recordings(db, USER, GROUP, focus="why no conversions", confirmed=False)
    assert out["error"] == "confirmation_required"
    out = await analyze_call_recordings(db, USER, GROUP, focus="why no conversions")
    assert out["error"] == "confirmation_required"
    # Anything but literal True is refused (the model can't sneak "yes" through)
    out = await analyze_call_recordings(db, USER, GROUP, focus="x", confirmed="true")
    assert out["error"] == "confirmation_required"


# ── analyze_calls (cached transcripts — no network) ─────────────────────────

async def test_analyze_uses_cached_transcripts_and_caps_limit(db, monkeypatch):
    await _seed(db, [
        {"transcript": "call one", "days_ago": 1},
        {"transcript": "call two", "days_ago": 2},
        {"transcript": "call three", "days_ago": 3},
    ])
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")

    summarized = []

    async def fake_summarize(client, transcript, focus, meta):
        summarized.append(transcript)
        return f"summary of: {transcript}", 10, 5

    monkeypatch.setattr(call_analysis_service, "_summarize_one", fake_summarize)

    out = await call_analysis_service.analyze_calls(
        db, USER, GROUP, "why aren't calls converting", limit=2, preset="last_7d",
    )
    assert out["counts"]["selected"] == 2
    assert out["counts"]["analyzed"] == 2
    assert out["counts"]["remaining_in_window"] == 1
    # Newest first
    assert summarized == ["call one", "call two"]
    assert all("summary" in r and "call_id" in r for r in out["calls"])
    assert out["calls"][0]["agent"] == "Alice"


async def test_analyze_limit_hard_capped(db, monkeypatch):
    await _seed(db, [{"transcript": f"t{i}", "days_ago": 1} for i in range(20)])
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")

    async def fake_summarize(client, transcript, focus, meta):
        return "s", 1, 1

    monkeypatch.setattr(call_analysis_service, "_summarize_one", fake_summarize)

    out = await call_analysis_service.analyze_calls(db, USER, GROUP, "focus", limit=99)
    assert out["counts"]["selected"] == call_analysis_service.CALL_ANALYSIS_MAX_CALLS


async def test_analyze_reports_transcription_failures(db, monkeypatch):
    await _seed(db, [{"days_ago": 1}])  # recorded but not transcribed
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")

    async def fake_transcribe(client, http, coll, doc):
        return {"error": "download failed", "audio_seconds": 0.0, "transcribed_now": False}

    monkeypatch.setattr(call_analysis_service, "_transcribe_one", fake_transcribe)

    out = await call_analysis_service.analyze_calls(db, USER, GROUP, "focus", limit=5)
    assert out["counts"]["analyzed"] == 0
    assert out["counts"]["failed"] == 1
    assert "could not transcribe" in out["calls"][0]["error"]


async def test_analyze_requires_openai_key(db, monkeypatch):
    await _seed(db, [{}])
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", None)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await call_analysis_service.analyze_calls(db, USER, GROUP, "focus")


# ── analyze_single_call ─────────────────────────────────────────────────────

async def test_single_call_returns_cached_analysis_without_network(db, monkeypatch):
    await _seed(db, [{"transcript": "hello", "url": "https://rec.example/a.mp3"}])
    await db["call_logs"].update_one(
        {"recording_url": "https://rec.example/a.mp3"},
        {"$set": {"ai_analysis": "WHAT HAPPENED: cached verdict."}},
    )
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")

    out = await call_analysis_service.analyze_single_call(
        db, USER, recording_url="https://rec.example/a.mp3",
    )
    assert out["cached"] is True
    assert out["analysis"] == "WHAT HAPPENED: cached verdict."
    assert out["agent"] == "Alice"


async def test_single_call_rejects_other_users_call(db, monkeypatch):
    await _seed(db, [{"url": "https://rec.example/a.mp3"}])
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")
    doc = await db["call_logs"].find_one({})
    with pytest.raises(ValueError, match="not found"):
        await call_analysis_service.analyze_single_call(db, "someone-else", call_id=str(doc["_id"]))


async def test_single_call_requires_id_or_url(db, monkeypatch):
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")
    with pytest.raises(ValueError, match="call_id or recording_url"):
        await call_analysis_service.analyze_single_call(db, USER)
