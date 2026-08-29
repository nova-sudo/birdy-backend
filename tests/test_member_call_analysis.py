"""
tests/test_member_call_analysis.py
----------------------------------
Member-scoped call analysis (Sales-Hub Members tab): cost estimate math,
exact-name agent matching, and the batched analyze pass with its `before`
cursor. No network — cached transcripts + monkeypatched summarizer.
"""

from datetime import datetime, timedelta

import pytest

from services import call_analysis_service

USER = "user-1"


async def _seed_calls(db, agent, n, *, duration=120, transcript=None, start=None):
    start = start or datetime.utcnow()
    docs = []
    for i in range(n):
        docs.append({
            "user_id": USER,
            "location_id": f"loc-{i % 3}",  # member scope spans locations
            "started_at": start - timedelta(hours=i),
            "direction": "outbound",
            "duration_seconds": duration,
            "recording_url": f"https://rec.example/{agent}-{i}.mp3",
            "raw_payload": {"caller_name": agent, "lead_name": f"Lead {i}"},
            **({"transcript": transcript} if transcript else {}),
        })
    await db["call_logs"].insert_many(docs)


# ── estimate ────────────────────────────────────────────────────────────────

async def test_estimate_scopes_to_exact_agent(mock_db):
    await _seed_calls(mock_db, "Bob", 3)
    await _seed_calls(mock_db, "Bobby Junior", 5)
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "Bob", 50)
    assert out["calls_found"] == 3  # anchored match — "Bobby Junior" excluded
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "bob", 50)
    assert out["calls_found"] == 3  # case-insensitive


async def test_estimate_credit_math(mock_db):
    # 2 uncached calls × 120s = 4 min × 0.6¢ × markup 4.0 = 9.6 + 2 × 0.05 = 9.7
    await _seed_calls(mock_db, "Alice", 2)
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "Alice", 10)
    assert out["calls_found"] == 2
    assert out["total_minutes"] == 4.0
    assert out["already_transcribed"] == 0
    assert out["estimated_credits"] == pytest.approx(9.7, abs=0.05)


async def test_estimate_cached_calls_are_cheap(mock_db):
    await _seed_calls(mock_db, "Alice", 2, transcript="hello there")
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "Alice", 10)
    assert out["already_transcribed"] == 2
    # No Whisper cost — only the per-call summary overhead remains.
    assert out["estimated_credits"] == pytest.approx(0.1, abs=0.01)


async def test_estimate_caps_at_available_and_requested(mock_db):
    await _seed_calls(mock_db, "Alice", 5)
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "Alice", 3)
    assert out["requested"] == 3
    assert out["calls_found"] == 3
    out = await call_analysis_service.estimate_member_analysis(mock_db, USER, "Alice", 999)
    assert out["requested"] == call_analysis_service.MEMBER_ANALYSIS_MAX_TOTAL
    assert out["calls_found"] == 5


# ── analyze (batched, cursor-paged) ─────────────────────────────────────────

@pytest.fixture
def summarizer(monkeypatch):
    monkeypatch.setattr(call_analysis_service, "OPENAI_API_KEY", "test-key")
    seen = []

    async def fake_summarize(client, transcript, focus, meta):
        seen.append((transcript, focus))
        return "did fine\nOUTCOME: connected", 10, 5

    monkeypatch.setattr(call_analysis_service, "_summarize_one", fake_summarize)
    return seen


async def test_analyze_member_batch_and_cursor(mock_db, summarizer):
    await _seed_calls(mock_db, "Alice", 20, transcript="cached words")

    first = await call_analysis_service.analyze_member_calls(mock_db, USER, "Alice", limit=15)
    assert first["counts"]["selected"] == 15
    assert first["counts"]["analyzed"] == 15
    assert first["counts"]["remaining"] == 5
    assert first["next_before"] is not None

    second = await call_analysis_service.analyze_member_calls(
        mock_db, USER, "Alice", limit=15, before=first["next_before"],
    )
    assert second["counts"]["selected"] == 5
    assert second["counts"]["remaining"] == 0
    # No overlap between batches — nothing double-billed.
    ids1 = {c["call_id"] for c in first["calls"]}
    ids2 = {c["call_id"] for c in second["calls"]}
    assert not (ids1 & ids2)


async def test_analyze_member_limit_hard_capped_per_request(mock_db, summarizer):
    await _seed_calls(mock_db, "Alice", 20, transcript="cached words")
    out = await call_analysis_service.analyze_member_calls(mock_db, USER, "Alice", limit=99)
    assert out["counts"]["selected"] == call_analysis_service.CALL_ANALYSIS_MAX_CALLS


async def test_analyze_member_default_focus_mentions_agent(mock_db, summarizer):
    await _seed_calls(mock_db, "Alice", 1, transcript="cached words")
    await call_analysis_service.analyze_member_calls(mock_db, USER, "Alice", limit=1)
    assert "Alice" in summarizer[0][1]


async def test_analyze_member_requires_agent_name(mock_db, summarizer):
    with pytest.raises(ValueError, match="agent_name"):
        await call_analysis_service.analyze_member_calls(mock_db, USER, "  ", limit=5)
