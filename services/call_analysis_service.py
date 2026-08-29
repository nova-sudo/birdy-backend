"""
services/call_analysis_service.py
---------------------------------
AI call-recording analysis: scope → transcribe (Whisper) → condense.

Backs the two chat tools in ai/tools/call_analysis_tools.py:

  * summarize_analyzable_calls — the cheap scoping pass. Counts what's in the
    window (calls, recordings, minutes, agents) so the agent can tell the user
    what an analysis would cover and ask for confirmation BEFORE any audio is
    touched.

  * analyze_calls — the heavy pass. Downloads each recording, transcribes it
    with OpenAI Whisper (always on Birdy's account OPENAI_API_KEY, whatever
    chat provider the user runs on), condenses each transcript with a cheap
    model, and returns per-call summaries the chat model can reason over.

Design constraints this file lives under:
  - Runs synchronously inside the chat request on Vercel serverless — batches
    are hard-capped (CALL_ANALYSIS_MAX_CALLS) and bounded by a wall-clock
    budget; anything unfinished is reported honestly as "not yet transcribed".
  - Tool results are truncated at MAX_RESULT_CHARS (8000) — raw transcripts
    never go back to the chat model, only ~3-sentence condensed summaries.
  - Transcripts are cached on the call_logs doc (`transcript`,
    `transcribed_at`) so re-runs and "next batch" follow-ups don't re-pay
    Whisper. The prune-call-payloads cron only strips raw_payload/headers,
    so cached transcripts survive retention pruning.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import httpx

from core.constants import PRESET_ALIAS, ghl_date_bounds
from ai.config import (
    OPENAI_API_KEY,
    WHISPER_MODEL,
    CALL_ANALYSIS_SUMMARY_MODEL,
    CALL_ANALYSIS_MAX_CALLS,
)

logger = logging.getLogger(__name__)

# Calls shorter than this are almost always no-connects (voicemail beep,
# instant hang-up) — nothing to transcribe. Callers can override per request.
DEFAULT_MIN_DURATION_SECONDS = 20

DOWNLOAD_TIMEOUT_SECONDS = 20.0
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Whisper's own upload limit
TRANSCRIBE_CONCURRENCY = 4
# Wall-clock budget for the whole transcribe+summarize phase. Kept under the
# serverless function ceiling so the chat request itself never times out;
# unfinished calls are surfaced as pending, not silently dropped.
ANALYSIS_TIME_BUDGET_SECONDS = 120.0
# Transcript slice handed to the summary model — enough for a ~20 min call;
# keeps the per-call summarization cost bounded.
MAX_TRANSCRIPT_CHARS_FOR_SUMMARY = 16000


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

async def _resolve_group(db, user_id: str, group_id: str) -> dict:
    """The user's client group, or a structured error the LLM can act on."""
    group = await db["client_groups"].find_one(
        {"id": group_id, "user_id": user_id},
        {"id": 1, "name": 1, "ghl_location_id": 1, "call_log_provider": 1},
    )
    if not group:
        raise ValueError(f"Client group {group_id!r} not found. Call get_client_groups for valid ids.")
    if not group.get("ghl_location_id"):
        raise ValueError(f"Client group {group.get('name') or group_id!r} has no linked GHL location — no call logs exist for it.")
    return group


def _window_bounds(preset: str | None, start_date: str | None, end_date: str | None):
    """(start_dt, end_dt, label) — explicit dates win over the preset."""
    if start_date or end_date:
        s = datetime.fromisoformat(start_date) if start_date else None
        e = datetime.fromisoformat(end_date) + timedelta(days=1) if end_date else None
        return s, e, f"{start_date or '…'} → {end_date or 'today'}"
    resolved = PRESET_ALIAS.get(preset or "last_7d", "last_7d")
    s_iso, e_iso = ghl_date_bounds(resolved)
    s = datetime.fromisoformat(s_iso) if s_iso else None
    # end bound is inclusive date → exclusive datetime at next midnight
    e = datetime.fromisoformat(e_iso) + timedelta(days=1) if e_iso else None
    return s, e, resolved


def _build_match(
    user_id: str,
    location_id: str,
    *,
    start_dt=None,
    end_dt=None,
    direction: str | None = None,
    agent_name: str | None = None,
    min_duration_seconds: int | None = None,
) -> dict:
    match: dict = {"user_id": user_id, "location_id": location_id}
    if start_dt or end_dt:
        rng = {}
        if start_dt:
            rng["$gte"] = start_dt
        if end_dt:
            rng["$lt"] = end_dt
        match["started_at"] = rng
    if direction:
        match["direction"] = direction.strip().lower()
    if agent_name:
        # Agent name lives on the HP raw payload (caller_name). Rows past the
        # retention window have raw_payload stripped and simply won't match.
        match["raw_payload.caller_name"] = {"$regex": agent_name.strip(), "$options": "i"}
    min_dur = DEFAULT_MIN_DURATION_SECONDS if min_duration_seconds is None else min_duration_seconds
    if min_dur > 0:
        match["duration_seconds"] = {"$gte": min_dur}
    return match


# ---------------------------------------------------------------------------
# Scoping pass (no audio touched)
# ---------------------------------------------------------------------------

async def summarize_analyzable_calls(
    db,
    user_id: str,
    group_id: str,
    *,
    preset: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    direction: str | None = None,
    agent_name: str | None = None,
    min_duration_seconds: int | None = None,
) -> dict:
    group = await _resolve_group(db, user_id, group_id)
    start_dt, end_dt, window_label = _window_bounds(preset, start_date, end_date)
    match = _build_match(
        user_id, group["ghl_location_id"],
        start_dt=start_dt, end_dt=end_dt, direction=direction,
        agent_name=agent_name, min_duration_seconds=min_duration_seconds,
    )

    coll = db["call_logs"]
    rec_match = {**match, "recording_url": {"$exists": True, "$nin": [None, ""]}}

    total = await coll.count_documents(match)
    with_recording = await coll.count_documents(rec_match)
    already_transcribed = await coll.count_documents({**rec_match, "transcript": {"$exists": True, "$nin": [None, ""]}})

    duration_rows = await coll.aggregate([
        {"$match": rec_match},
        {"$group": {"_id": None, "seconds": {"$sum": {"$ifNull": ["$duration_seconds", 0]}}}},
    ]).to_list(1)
    total_seconds = duration_rows[0]["seconds"] if duration_rows else 0

    by_agent = await coll.aggregate([
        {"$match": rec_match},
        {"$group": {
            "_id": {"$ifNull": ["$raw_payload.caller_name", "unknown"]},
            "calls": {"$sum": 1},
            "seconds": {"$sum": {"$ifNull": ["$duration_seconds", 0]}},
        }},
        {"$sort": {"calls": -1}},
        {"$limit": 15},
    ]).to_list(15)

    by_direction = await coll.aggregate([
        {"$match": rec_match},
        {"$group": {"_id": {"$ifNull": ["$direction", "unknown"]}, "calls": {"$sum": 1}}},
    ]).to_list(None)

    return {
        "group_id": group["id"],
        "group_name": group.get("name"),
        "window": window_label,
        "filters": {
            "direction": direction,
            "agent_name": agent_name,
            "min_duration_seconds": DEFAULT_MIN_DURATION_SECONDS if min_duration_seconds is None else min_duration_seconds,
        },
        "matching_calls": total,
        "with_recording": with_recording,
        "already_transcribed": already_transcribed,
        "total_recorded_minutes": round(total_seconds / 60.0, 1),
        "by_agent": [
            {"agent": a["_id"], "calls": a["calls"], "minutes": round(a["seconds"] / 60.0, 1)}
            for a in by_agent
        ],
        "by_direction": {d["_id"]: d["calls"] for d in by_direction},
        "max_calls_per_analysis": CALL_ANALYSIS_MAX_CALLS,
    }


# ---------------------------------------------------------------------------
# Transcription + per-call condensation
# ---------------------------------------------------------------------------

def _audio_filename(url: str) -> str:
    """Whisper sniffs format from the filename — keep the URL's extension."""
    name = (urlparse(url).path.rsplit("/", 1)[-1] or "audio").strip()
    if "." not in name:
        name += ".mp3"
    return name


async def _transcribe_one(openai_client, http, coll, doc: dict) -> dict:
    """Ensure `doc` has a transcript; returns {transcript | error, audio_seconds, transcribed_now}.

    `doc["_id"]` may be None for ad-hoc rows (a recording the HP cron hasn't
    persisted to call_logs yet) — then the transcript is returned but not cached.
    """
    if isinstance(doc.get("transcript"), str) and doc["transcript"].strip():
        return {"transcript": doc["transcript"], "audio_seconds": 0.0, "transcribed_now": False}

    url = doc.get("recording_url")
    try:
        resp = await http.get(url, follow_redirects=True)
        resp.raise_for_status()
        audio = resp.content
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError(f"recording is {len(audio) // (1024 * 1024)}MB — over Whisper's 25MB limit")
        if not audio:
            raise ValueError("recording download was empty")

        tr = await openai_client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=(_audio_filename(url), audio),
            response_format="verbose_json",
        )
        text = (getattr(tr, "text", "") or "").strip()
        # Billing basis: Whisper's own measured duration; fall back to the
        # provider-reported call duration if the field is ever missing.
        seconds = float(getattr(tr, "duration", 0) or doc.get("duration_seconds") or 0)

        if doc.get("_id") is not None:
            await coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "transcript": text,
                    "transcript_audio_seconds": seconds,
                    "transcribed_at": datetime.utcnow(),
                },
                 "$unset": {"transcription_error": ""}},
            )
        return {"transcript": text, "audio_seconds": seconds, "transcribed_now": True}
    except Exception as e:  # noqa: BLE001 — every failure becomes a per-call row
        msg = str(e)[:200]
        logger.warning("Transcription failed for call %s: %s", doc.get("_id"), msg)
        try:
            if doc.get("_id") is not None:
                await coll.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"transcription_error": msg, "transcription_failed_at": datetime.utcnow()}},
                )
        except Exception:
            pass
        return {"error": msg, "audio_seconds": 0.0, "transcribed_now": False}


_SUMMARY_SYSTEM = (
    "You condense call-center recording transcripts for a marketing analyst. "
    "Given one transcript, reply with AT MOST 3 short sentences describing what "
    "happened on the call, focused on the analyst's question. Then a final line: "
    "OUTCOME: one of connected | voicemail | no_answer | wrong_number | "
    "callback_promised | appointment_set | not_interested | other. "
    "Be concrete (objections raised, what the agent said/missed). Never invent detail."
)


async def _summarize_one(openai_client, transcript: str, focus: str, meta: dict) -> tuple[str, int, int]:
    """(summary_text, prompt_tokens, completion_tokens) for one transcript."""
    if not transcript:
        return "Recording contained no speech.", 0, 0
    header = (
        f"Analyst's question: {focus}\n"
        f"Call meta: agent={meta.get('agent') or 'unknown'}, direction={meta.get('direction') or 'unknown'}, "
        f"duration={meta.get('duration_seconds') or 0}s\n\nTranscript:\n"
    )
    resp = await openai_client.chat.completions.create(
        model=CALL_ANALYSIS_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": header + transcript[:MAX_TRANSCRIPT_CHARS_FOR_SUMMARY]},
        ],
        temperature=0.2,
        max_tokens=160,
    )
    u = getattr(resp, "usage", None)
    return (
        (resp.choices[0].message.content or "").strip(),
        getattr(u, "prompt_tokens", 0) or 0,
        getattr(u, "completion_tokens", 0) or 0,
    )


# ---------------------------------------------------------------------------
# Single-call analysis (Sales-Hub "AI analyze" button)
# ---------------------------------------------------------------------------

_SINGLE_CALL_SYSTEM = (
    "You are a call-center QA analyst reviewing ONE call recording transcript "
    "for a marketing agency. Reply with EXACTLY these sections, each 1-3 short "
    "sentences, plain text:\n"
    "WHAT HAPPENED: what actually occurred on the call.\n"
    "WHAT WENT WRONG: the main problem, if any (say 'Nothing notable' if the "
    "call went well).\n"
    "COACHING: one concrete thing the agent could do better next time (or "
    "'None' if not applicable, e.g. voicemail).\n"
    "OUTCOME: one of connected | voicemail | no_answer | wrong_number | "
    "callback_promised | appointment_set | not_interested | other.\n"
    "Be concrete and quote short fragments when useful. Never invent detail "
    "that is not in the transcript."
)


async def analyze_single_call(
    db,
    user_id: str,
    call_id: str | None = None,
    recording_url: str | None = None,
    *,
    extra_meta: dict | None = None,
    force: bool = False,
    session_id: str | None = None,
) -> dict:
    """Transcribe + deep-analyze ONE call (ownership-checked).

    Address the call by Mongo `call_id` OR by `recording_url` — the Sales-Hub
    Calls tab rows come from the HotProspector cache and carry no Mongo id,
    only the recording URL. When a matching call_logs doc exists, transcript
    and analysis are cached on it (`ai_analysis`, `ai_analyzed_at`) so a second
    click is instant and free; if the HP cron hasn't persisted the call yet,
    the analysis still runs ad-hoc (nothing cached). `force=True` re-runs the
    analysis (the transcript itself is always reused once present).
    """
    from bson import ObjectId
    from bson.errors import InvalidId
    from openai import AsyncOpenAI

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured — call analysis requires it.")
    if not call_id and not recording_url:
        raise ValueError("Provide call_id or recording_url.")

    coll = db["call_logs"]
    # user_id in every filter is the authorization check — someone else's call
    # reads as missing, never as forbidden-but-present.
    doc = None
    if call_id:
        try:
            doc = await coll.find_one({"_id": ObjectId(call_id), "user_id": user_id})
        except (InvalidId, TypeError):
            raise ValueError(f"Invalid call id: {call_id!r}")
        if not doc:
            raise ValueError("Call not found.")
    else:
        doc = await coll.find_one(
            {"user_id": user_id, "recording_url": recording_url},
            sort=[("started_at", -1)],
        )
        if not doc:
            # Not yet synced into call_logs — analyze straight off the URL.
            doc = {"_id": None, "recording_url": recording_url, **(extra_meta or {})}

    meta = {
        "call_id": str(doc["_id"]),
        "started_at": doc["started_at"].isoformat() if isinstance(doc.get("started_at"), datetime) else doc.get("started_at"),
        "agent": (doc.get("raw_payload") or {}).get("caller_name"),
        "lead_name": (doc.get("raw_payload") or {}).get("lead_name"),
        "direction": doc.get("direction"),
        "duration_seconds": doc.get("duration_seconds"),
        "contact_phone": doc.get("contact_phone"),
    }

    if doc.get("ai_analysis") and not force:
        return {**meta, "analysis": doc["ai_analysis"], "cached": True}

    if not doc.get("recording_url"):
        raise ValueError("This call has no recording to analyze.")

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS) as http:
        t = await _transcribe_one(openai_client, http, coll, doc)
    if "error" in t:
        raise ValueError(f"Could not transcribe the recording: {t['error']}")

    header = (
        f"Call meta: agent={meta['agent'] or 'unknown'}, lead={meta['lead_name'] or 'unknown'}, "
        f"direction={meta['direction'] or 'unknown'}, duration={meta['duration_seconds'] or 0}s\n\nTranscript:\n"
    )
    resp = await openai_client.chat.completions.create(
        model=CALL_ANALYSIS_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": _SINGLE_CALL_SYSTEM},
            {"role": "user", "content": header + t["transcript"][:MAX_TRANSCRIPT_CHARS_FOR_SUMMARY]},
        ],
        temperature=0.2,
        max_tokens=320,
    )
    u = getattr(resp, "usage", None)
    analysis_text = (resp.choices[0].message.content or "").strip()

    await coll.update_one(
        {"_id": doc["_id"]},
        {"$set": {"ai_analysis": analysis_text, "ai_analyzed_at": datetime.utcnow()}},
    )

    # ── Metering (best-effort, same treatment as the batch path) ────────────
    try:
        from credits import record_audio_usage, record_usage
        if t["audio_seconds"] > 0:
            await record_audio_usage(
                db, user_id,
                audio_seconds=t["audio_seconds"],
                model=WHISPER_MODEL,
                feature="call_analysis",
                session_id=session_id,
            )
        await record_usage(
            db, user_id,
            model=CALL_ANALYSIS_SUMMARY_MODEL,
            prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(u, "completion_tokens", 0) or 0,
            model_calls=1,
            mode="managed",
            feature="call_analysis",
            session_id=session_id,
        )
    except Exception:
        logger.debug("Single-call credit metering skipped", exc_info=True)

    return {**meta, "analysis": analysis_text, "cached": False}


# ---------------------------------------------------------------------------
# Analysis pass
# ---------------------------------------------------------------------------

async def analyze_calls(
    db,
    user_id: str,
    group_id: str,
    focus: str,
    *,
    limit: int = 10,
    preset: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    direction: str | None = None,
    agent_name: str | None = None,
    min_duration_seconds: int | None = None,
    session_id: str | None = None,
) -> dict:
    from openai import AsyncOpenAI

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured — call analysis requires it.")

    group = await _resolve_group(db, user_id, group_id)
    start_dt, end_dt, window_label = _window_bounds(preset, start_date, end_date)
    limit = max(1, min(int(limit or 10), CALL_ANALYSIS_MAX_CALLS))

    match = _build_match(
        user_id, group["ghl_location_id"],
        start_dt=start_dt, end_dt=end_dt, direction=direction,
        agent_name=agent_name, min_duration_seconds=min_duration_seconds,
    )
    match["recording_url"] = {"$exists": True, "$nin": [None, ""]}

    coll = db["call_logs"]
    eligible_total = await coll.count_documents(match)
    docs = await coll.find(
        match,
        {
            "_id": 1, "started_at": 1, "direction": 1, "duration_seconds": 1,
            "recording_url": 1, "transcript": 1, "contact_phone": 1,
            "raw_payload.caller_name": 1,
        },
    ).sort("started_at", -1).limit(limit).to_list(limit)

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)
    audio_seconds_total = 0.0
    tok_in_total = 0
    tok_out_total = 0

    async def _process(doc: dict) -> dict:
        nonlocal audio_seconds_total, tok_in_total, tok_out_total
        meta = {
            "call_id": str(doc["_id"]),
            "started_at": doc["started_at"].isoformat() if isinstance(doc.get("started_at"), datetime) else doc.get("started_at"),
            "agent": (doc.get("raw_payload") or {}).get("caller_name"),
            "direction": doc.get("direction"),
            "duration_seconds": doc.get("duration_seconds"),
            "contact_phone": doc.get("contact_phone"),
        }
        async with sem:
            t = await _transcribe_one(openai_client, http, coll, doc)
            audio_seconds_total += t["audio_seconds"]
            if "error" in t:
                return {**meta, "error": f"could not transcribe: {t['error']}"}
            summary, tin, tout = await _summarize_one(openai_client, t["transcript"], focus, meta)
            tok_in_total += tin
            tok_out_total += tout
            return {**meta, "summary": summary}

    rows: list[dict] = []
    pending_count = 0
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS) as http:
        tasks = [asyncio.ensure_future(_process(d)) for d in docs]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=ANALYSIS_TIME_BUDGET_SECONDS)
            for p in pending:
                p.cancel()
            # Let cancellations land before the HTTP client closes underneath them.
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            pending_count = len(pending)
            # Preserve newest-first doc order among the finished tasks.
            finished = {}
            for d in done:
                try:
                    row = d.result()
                    finished[row["call_id"]] = row
                except Exception as e:  # noqa: BLE001
                    logger.error("Call analysis task failed: %s", e, exc_info=True)
            rows = [finished[str(d["_id"])] for d in docs if str(d["_id"]) in finished]

    # ── Metering: Whisper minutes + summarization tokens (best-effort) ──────
    try:
        from credits import record_audio_usage, record_usage
        if audio_seconds_total > 0:
            await record_audio_usage(
                db, user_id,
                audio_seconds=audio_seconds_total,
                model=WHISPER_MODEL,
                feature="call_analysis",
                session_id=session_id,
            )
        if tok_in_total or tok_out_total:
            await record_usage(
                db, user_id,
                model=CALL_ANALYSIS_SUMMARY_MODEL,
                prompt_tokens=tok_in_total,
                completion_tokens=tok_out_total,
                model_calls=sum(1 for r in rows if "summary" in r),
                mode="managed",  # always Birdy's key, whatever the chat rate mode
                feature="call_analysis",
                session_id=session_id,
            )
    except Exception:
        logger.debug("Call-analysis credit metering skipped", exc_info=True)

    analyzed = [r for r in rows if "summary" in r]
    failed = [r for r in rows if "error" in r]
    return {
        "group_name": group.get("name"),
        "window": window_label,
        "focus": focus,
        "calls": rows,
        "counts": {
            "eligible_in_window": eligible_total,
            "selected": len(docs),
            "analyzed": len(analyzed),
            "failed": len(failed),
            "not_finished_in_time": pending_count,
            "remaining_in_window": max(0, eligible_total - len(docs)),
        },
        "note": (
            "Summaries are condensed per call — base your diagnosis only on them. "
            "If remaining_in_window > 0 you may offer the user another batch. "
            "Calls listed with an error or not finished in time were NOT analyzed; "
            "never guess their content."
        ),
    }


# ---------------------------------------------------------------------------
# Member-scoped analysis (Sales-Hub Members tab: "analyze this agent's last N")
# ---------------------------------------------------------------------------
# The Members tab is account-wide (one row per HotProspector agent, no client
# scope), so these select by the agent's name across ALL of the user's
# locations. Agent identity: call_logs.raw_payload.caller_name — anchored
# case-insensitive match against the member's display name.

# The user picks 1..50 calls; each API request still processes at most
# CALL_ANALYSIS_MAX_CALLS — the frontend chains batches via `before`.
MEMBER_ANALYSIS_MAX_TOTAL = 50

# Estimate-only constant: observed gpt-4o-mini cost per summarized call is
# ~0.04 credits (live test: 10 calls → 0.43); rounded up to stay conservative.
SUMMARY_CREDITS_PER_CALL = 0.05


def _member_match(user_id: str, agent_name: str, *, min_duration_seconds: int | None = None) -> dict:
    import re
    min_dur = DEFAULT_MIN_DURATION_SECONDS if min_duration_seconds is None else min_duration_seconds
    match = {
        "user_id": user_id,
        "raw_payload.caller_name": {"$regex": f"^{re.escape(agent_name.strip())}$", "$options": "i"},
        "recording_url": {"$exists": True, "$nin": [None, ""]},
    }
    if min_dur > 0:
        match["duration_seconds"] = {"$gte": min_dur}
    return match


async def estimate_member_analysis(db, user_id: str, agent_name: str, limit: int) -> dict:
    """Cost preview for 'analyze this member's last N calls' — no audio touched.

    Whisper is the dominant cost and only UNCACHED calls pay it, so the
    estimate prices cached (already-transcribed) minutes at zero and adds a
    small flat per-call summarization overhead.
    """
    from credits import get_credits_settings, WHISPER_CENTS_PER_MINUTE

    if not (agent_name or "").strip():
        raise ValueError("agent_name is required.")
    limit = max(1, min(int(limit or 10), MEMBER_ANALYSIS_MAX_TOTAL))

    docs = await db["call_logs"].find(
        _member_match(user_id, agent_name),
        {"duration_seconds": 1, "transcript": 1},
    ).sort("started_at", -1).limit(limit).to_list(limit)

    total_seconds = sum(d.get("duration_seconds") or 0 for d in docs)
    cached = sum(1 for d in docs if isinstance(d.get("transcript"), str) and d["transcript"].strip())
    uncached_seconds = sum(
        d.get("duration_seconds") or 0 for d in docs
        if not (isinstance(d.get("transcript"), str) and d["transcript"].strip())
    )

    settings = await get_credits_settings(db)
    whisper_credits = (uncached_seconds / 60.0) * WHISPER_CENTS_PER_MINUTE * settings["markup"]
    estimated = round(whisper_credits + len(docs) * SUMMARY_CREDITS_PER_CALL, 1)

    return {
        "agent_name": agent_name,
        "requested": limit,
        "calls_found": len(docs),
        "total_minutes": round(total_seconds / 60.0, 1),
        "already_transcribed": cached,
        "estimated_credits": estimated,
        "batch_size": CALL_ANALYSIS_MAX_CALLS,
        "batches": max(1, -(-len(docs) // CALL_ANALYSIS_MAX_CALLS)) if docs else 0,
    }


async def analyze_member_calls(
    db,
    user_id: str,
    agent_name: str,
    *,
    limit: int = 10,
    before: str | None = None,
    focus: str | None = None,
    session_id: str | None = None,
) -> dict:
    """One batch (≤ CALL_ANALYSIS_MAX_CALLS) of a member's most recent recorded
    calls, newest first. `before` (ISO datetime) is the paging cursor: pass the
    previous batch's `next_before` to continue backwards through history."""
    from openai import AsyncOpenAI

    if not OPENAI_API_KEY:
        raise ValueError("OPENAI_API_KEY is not configured — call analysis requires it.")
    if not (agent_name or "").strip():
        raise ValueError("agent_name is required.")

    limit = max(1, min(int(limit or 10), CALL_ANALYSIS_MAX_CALLS))
    focus = (focus or "").strip() or f"How is {agent_name} performing on calls, and what is going wrong?"

    match = _member_match(user_id, agent_name)
    if before:
        match["started_at"] = {"$lt": datetime.fromisoformat(before)}

    coll = db["call_logs"]
    eligible_total = await coll.count_documents(match)
    docs = await coll.find(
        match,
        {
            "_id": 1, "started_at": 1, "direction": 1, "duration_seconds": 1,
            "recording_url": 1, "transcript": 1, "contact_phone": 1,
            "raw_payload.caller_name": 1, "raw_payload.lead_name": 1,
        },
    ).sort("started_at", -1).limit(limit).to_list(limit)

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)
    audio_seconds_total = 0.0
    tok_in_total = 0
    tok_out_total = 0

    async def _process(doc: dict) -> dict:
        nonlocal audio_seconds_total, tok_in_total, tok_out_total
        meta = {
            "call_id": str(doc["_id"]),
            "started_at": doc["started_at"].isoformat() if isinstance(doc.get("started_at"), datetime) else doc.get("started_at"),
            "agent": (doc.get("raw_payload") or {}).get("caller_name"),
            "lead_name": (doc.get("raw_payload") or {}).get("lead_name"),
            "direction": doc.get("direction"),
            "duration_seconds": doc.get("duration_seconds"),
            "contact_phone": doc.get("contact_phone"),
        }
        async with sem:
            t = await _transcribe_one(openai_client, http, coll, doc)
            audio_seconds_total += t["audio_seconds"]
            if "error" in t:
                return {**meta, "error": f"could not transcribe: {t['error']}"}
            summary, tin, tout = await _summarize_one(openai_client, t["transcript"], focus, meta)
            tok_in_total += tin
            tok_out_total += tout
            return {**meta, "summary": summary}

    rows: list[dict] = []
    pending_count = 0
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS) as http:
        tasks = [asyncio.ensure_future(_process(d)) for d in docs]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=ANALYSIS_TIME_BUDGET_SECONDS)
            for p in pending:
                p.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            pending_count = len(pending)
            finished = {}
            for d in done:
                try:
                    row = d.result()
                    finished[row["call_id"]] = row
                except Exception as e:  # noqa: BLE001
                    logger.error("Member call analysis task failed: %s", e, exc_info=True)
            rows = [finished[str(d["_id"])] for d in docs if str(d["_id"]) in finished]

    try:
        from credits import record_audio_usage, record_usage
        if audio_seconds_total > 0:
            await record_audio_usage(
                db, user_id,
                audio_seconds=audio_seconds_total,
                model=WHISPER_MODEL,
                feature="call_analysis",
                session_id=session_id,
            )
        if tok_in_total or tok_out_total:
            await record_usage(
                db, user_id,
                model=CALL_ANALYSIS_SUMMARY_MODEL,
                prompt_tokens=tok_in_total,
                completion_tokens=tok_out_total,
                model_calls=sum(1 for r in rows if "summary" in r),
                mode="managed",
                feature="call_analysis",
                session_id=session_id,
            )
    except Exception:
        logger.debug("Member call-analysis credit metering skipped", exc_info=True)

    # Cursor: the oldest doc actually SELECTED this batch (processed or not) —
    # the next request continues strictly before it, so nothing is re-billed.
    next_before = None
    if docs:
        oldest = docs[-1].get("started_at")
        next_before = oldest.isoformat() if isinstance(oldest, datetime) else oldest

    analyzed = [r for r in rows if "summary" in r]
    failed = [r for r in rows if "error" in r]
    return {
        "agent_name": agent_name,
        "focus": focus,
        "calls": rows,
        "counts": {
            "eligible": eligible_total,
            "selected": len(docs),
            "analyzed": len(analyzed),
            "failed": len(failed),
            "not_finished_in_time": pending_count,
            "remaining": max(0, eligible_total - len(docs)),
        },
        "next_before": next_before,
    }
