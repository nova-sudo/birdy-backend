"""
ai/tools/call_analysis_tools.py
-------------------------------
Chat tools for AI call-recording analysis (Whisper transcription).

Two-step flow, enforced twice (system prompt + the `confirmed` gate here):

  1. `get_call_recordings_summary` — cheap scoping pass, no audio touched.
     The agent reports what an analysis would cover (how many calls, minutes,
     which agents) and asks the user how many calls to analyze.
  2. `analyze_call_recordings` — only after the user explicitly confirms.
     Transcribes up to CALL_ANALYSIS_MAX_CALLS recordings and returns
     condensed per-call summaries for the chat model to diagnose from.

Heavy lifting lives in services/call_analysis_service.py.
"""

from ai.tools.registry import registry
from ai.config import CALL_ANALYSIS_MAX_CALLS
from services import call_analysis_service


async def get_call_recordings_summary(
    db,
    user_id,
    group_id,
    preset=None,
    start_date=None,
    end_date=None,
    direction=None,
    agent_name=None,
    min_duration_seconds=None,
    **_,
):
    return await call_analysis_service.summarize_analyzable_calls(
        db,
        user_id,
        group_id,
        preset=preset,
        start_date=start_date,
        end_date=end_date,
        direction=direction,
        agent_name=agent_name,
        min_duration_seconds=min_duration_seconds,
    )


async def analyze_call_recordings(
    db,
    user_id,
    group_id,
    focus,
    confirmed=False,
    limit=10,
    preset=None,
    start_date=None,
    end_date=None,
    direction=None,
    agent_name=None,
    min_duration_seconds=None,
    **_,
):
    # Hard gate, independent of the system prompt: transcription costs real
    # money and time, so it never runs until the user has said yes to a
    # concrete scope ("analyze 10 calls from last week").
    if confirmed is not True:
        return {
            "error": "confirmation_required",
            "message": (
                "Do NOT run this tool yet. First call get_call_recordings_summary, "
                "tell the user how many calls/minutes the analysis would cover, ask "
                "how many calls to analyze (offer e.g. 5 / 10 / 15), and only after "
                "the user explicitly confirms call this tool again with confirmed=true."
            ),
        }
    return await call_analysis_service.analyze_calls(
        db,
        user_id,
        group_id,
        focus or "What is going wrong on these calls?",
        limit=limit,
        preset=preset,
        start_date=start_date,
        end_date=end_date,
        direction=direction,
        agent_name=agent_name,
        min_duration_seconds=min_duration_seconds,
    )


# ── Schemas ─────────────────────────────────────────────────────────────────

_SCOPE_PROPS = {
    "group_id": {
        "type": "string",
        "description": "Client group id (from get_client_groups). Required — analysis is always scoped to one client.",
    },
    "preset": {
        "type": "string",
        "description": (
            "Date-window preset. One of: today, yesterday, last_7d, last_14d, last_30d, "
            "this_month, last_month, this_quarter, last_quarter, this_year, last_year, "
            "maximum. Default last_7d. Ignored when start_date/end_date are given."
        ),
    },
    "start_date": {"type": "string", "description": "Optional explicit window start, YYYY-MM-DD."},
    "end_date": {"type": "string", "description": "Optional explicit window end (inclusive), YYYY-MM-DD."},
    "direction": {
        "type": "string",
        "description": "Optional filter: 'inbound' or 'outbound'.",
    },
    "agent_name": {
        "type": "string",
        "description": "Optional case-insensitive filter on the calling agent's name (e.g. when the user asks about one rep's calls).",
    },
    "min_duration_seconds": {
        "type": "integer",
        "description": "Skip calls shorter than this (default 20s — filters out no-connects). Set 0 to include everything.",
    },
}


def register_call_analysis_tools():
    registry.register(
        name="get_call_recordings_summary",
        description=(
            "Step 1 of call-recording analysis (cheap, no audio processed): count the call "
            "recordings available for one client in a date window — total calls, how many "
            "have recordings, total recorded minutes, breakdown by agent and direction, and "
            "how many are already transcribed. ALWAYS call this first when the user wants "
            "calls listened to / analyzed / diagnosed, then report the scope and ask the "
            "user how many calls to analyze before calling analyze_call_recordings."
        ),
        parameters={
            "type": "object",
            "properties": dict(_SCOPE_PROPS),
            "required": ["group_id"],
        },
        executor=get_call_recordings_summary,
    )

    registry.register(
        name="analyze_call_recordings",
        description=(
            "Step 2 of call-recording analysis (slow, costs credits): download, transcribe "
            f"(Whisper) and summarize up to {CALL_ANALYSIS_MAX_CALLS} call recordings, newest first, and "
            "return a condensed per-call summary + outcome for each so you can diagnose "
            "what's going wrong (agent performance, lead quality, objections, no-answers). "
            "NEVER call this until the user has explicitly confirmed how many calls to "
            "analyze — run get_call_recordings_summary and ask first. If more calls remain "
            "afterwards, offer the user the next batch."
        ),
        parameters={
            "type": "object",
            "properties": {
                **_SCOPE_PROPS,
                "focus": {
                    "type": "string",
                    "description": (
                        "What the user wants diagnosed, in one sentence — steers every "
                        "per-call summary (e.g. 'why are calls not converting to appointments', "
                        "'is agent John handling objections properly')."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": f"How many calls to analyze this run (1-{CALL_ANALYSIS_MAX_CALLS}). Use exactly the number the user confirmed.",
                },
                "confirmed": {
                    "type": "boolean",
                    "description": (
                        "Set true ONLY after the user has explicitly confirmed the number of "
                        "calls to analyze in this conversation. Never set true on your own "
                        "initiative — the tool will refuse."
                    ),
                },
            },
            "required": ["group_id", "focus", "confirmed"],
        },
        executor=analyze_call_recordings,
    )
