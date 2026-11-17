"""
ai/mcp/call_analysis_mcp.py
----------------------------
Call-recording analysis. get_call_recordings_summary is a cheap scoping pass; analyze_call_recordings transcribes
audio (paid, metered exactly as in Ask Birdy), so it is two-step: a preview and a confirm_code first. Both wrap
ai/tools/call_analysis_tools.py; user_id always comes from the token.
"""

from typing import Literal

from ai.mcp.access import READ, WRITE
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import call_analysis_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


@mcp.tool(**READ)
async def get_call_recordings_summary(group_id: str, preset: str | None = None, start_date: str | None = None,
                                      end_date: str | None = None, direction: Literal["inbound", "outbound"] | None = None,
                                      agent_name: str | None = None, min_duration_seconds: int | None = None) -> dict:
    """Count the call recordings available for one client in a date window, without touching any audio: total calls, how many have recordings, recorded minutes, the split by agent and direction, and how many are already transcribed.

    Use it first whenever the owner wants calls listened to, analyzed or diagnosed, then tell them the scope before
    calling analyze_call_recordings.

    Args:
        group_id: Client group id (from get_client_groups). Required; analysis is always for one client.
        preset: today, yesterday, last_7d (default), last_14d, last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year or maximum. Ignored when start_date/end_date are given.
        start_date: Optional explicit window start, YYYY-MM-DD.
        end_date: Optional explicit window end (inclusive), YYYY-MM-DD.
        direction: Optional filter, inbound or outbound.
        agent_name: Optional case-insensitive filter on the calling agent's name.
        min_duration_seconds: Skip calls shorter than this (default 20 seconds). 0 includes everything.
    """
    try:
        return await call_analysis_tools.get_call_recordings_summary(
            get_db(get_shared_mongo_client()), _current_user_id(), group_id=group_id, preset=preset,
            start_date=start_date, end_date=end_date, direction=direction, agent_name=agent_name,
            min_duration_seconds=min_duration_seconds)
    except ValueError as e:
        return _error(str(e))


@mcp.tool(**WRITE)
async def analyze_call_recordings(group_id: str, focus: str, limit: int = 10, preset: str | None = None,
                                  start_date: str | None = None, end_date: str | None = None,
                                  direction: Literal["inbound", "outbound"] | None = None,
                                  agent_name: str | None = None, min_duration_seconds: int | None = None,
                                  confirm_code: str | None = None) -> dict:
    """Download, transcribe (Whisper) and summarize up to 15 of a client's call recordings, newest first, and return a condensed summary and outcome per call to diagnose what is going wrong (agent performance, lead quality, objections, no-answers).

    This is slow and uses Birdy credits: transcription is paid work, metered the same as in the Birdy app. Call
    get_call_recordings_summary first. The first call returns a preview and a confirm_code and does nothing.
    Show the preview to the user and only call again with confirm_code after they say yes.
    Pass the same arguments with it. If calls remain afterwards, offer the next batch.

    Args:
        group_id: Client group id (from get_client_groups).
        focus: What to diagnose, in one sentence; it steers every per-call summary (e.g. "why are calls not converting to appointments").
        limit: How many calls to analyze this run (1-15, default 10). Use the number the user agreed to.
        preset: Date-window preset as in get_call_recordings_summary (default last_7d).
        start_date: Optional explicit window start, YYYY-MM-DD.
        end_date: Optional explicit window end (inclusive), YYYY-MM-DD.
        direction: Optional filter, inbound or outbound.
        agent_name: Optional case-insensitive filter on the calling agent's name.
        min_duration_seconds: Skip calls shorter than this (default 20 seconds).
        confirm_code: The code from the preview, after the user agreed.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    scope = {"preset": preset, "start_date": start_date, "end_date": end_date, "direction": direction,
             "agent_name": agent_name, "min_duration_seconds": min_duration_seconds}
    args = {"group_id": group_id, "focus": focus, "limit": limit, **scope}

    summary: dict = {}
    if not confirm_code:
        # Validate first: an unknown client or no recordings means nothing would happen, so no code is issued.
        try:
            summary = await call_analysis_tools.get_call_recordings_summary(db, user_id, group_id=group_id, **scope)
        except ValueError as e:
            return _error(str(e))
        if not summary.get("with_recording"):
            return {**_error("There are no call recordings for that client and window, so there is nothing to "
                             "analyze. Try a wider window or other filters."), "summary": summary}

    async def preview() -> dict:
        n = max(1, min(int(limit or 10), summary.get("max_calls_per_analysis") or 15, summary["with_recording"]))
        return {"action": "Transcribe and analyze call recordings", "client": summary.get("group_name"),
                "window": summary.get("window"), "focus": focus, "calls_to_transcribe": n,
                "recordings_available": summary["with_recording"],
                "already_transcribed": summary.get("already_transcribed"),
                "total_recorded_minutes": summary.get("total_recorded_minutes"),
                "cost": "Transcription is paid. It uses Birdy credits, metered exactly as in the Birdy app, "
                        "and takes a while."}

    async def execute() -> dict:
        try:
            return await call_analysis_tools.analyze_call_recordings(
                db, user_id, group_id=group_id, focus=focus, confirmed=True, limit=limit, **scope)
        except ValueError as e:
            return _error(str(e))

    return await two_step(db, user_id, "analyze_call_recordings", args, confirm_code, preview, execute)
