"""
services/lead_summary.py
------------------------
Cached AI summary of one lead for the Lead Detail page.

A plain-text brief is assembled from the lead detail, journey and messages and
sent to a small model once; the result is cached in `lead_summaries` (one doc
per user + contact) so viewing a lead again never charges again. Only the first
generation and an explicit refresh call the model. The cache keeps a hash of the
brief it was written from, so the page can say "new activity since this summary"
without regenerating.

Never logs the brief, the summary or any lead data.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from openai import AsyncOpenAI

from ai.config import LEAD_SUMMARY_MODEL, OPENAI_API_KEY
from core.database import get_db
from services.ghl_messages import fetch_lead_messages
from services.lead_detail import build_lead_detail
from services.lead_journey import build_lead_journey

logger = logging.getLogger(__name__)

COLLECTION = "lead_summaries"
OPENAI_TIMEOUT_SECONDS = 20
MAX_JOURNEY_EVENTS = 25
MAX_CALLS = 5
MAX_TRANSCRIPT_CHARS = 600
MAX_MESSAGES = 20
MAX_MESSAGE_CHARS = 300

SYSTEM_PROMPT = (
    "You summarise one sales lead for a marketing agency. Write 2–4 sentences in British "
    "English, plain text, no lists or headings. Cover where the lead came from, how they've "
    "engaged (calls, messages, bookings), and how ready they look to buy, ending with the "
    "clearest next step if one is obvious. Use only facts in the brief; never invent names, "
    "prices, times or outcomes. If the brief is thin, say so briefly."
)


async def create_lead_summary_indexes(mongo_client):
    """One cached summary per (user, contact). Called from core.indexes."""
    try:
        await get_db(mongo_client)[COLLECTION].create_index(
            [("user_id", 1), ("contact_id", 1)], unique=True)
    except Exception as e:
        logger.warning(f"Could not create lead_summaries indexes: {e}")


def _clip(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _line(*pairs) -> str:
    """'a: x; b: y' from (label, value) pairs, skipping empty values."""
    return "; ".join(f"{label}: {value}" for label, value in pairs
                     if value not in (None, "", [], {}))


def build_summary_input(detail: dict, journey: dict | None, messages: dict | None) -> str:
    """Compact plain-text brief. Empty sections are omitted entirely."""
    detail = detail or {}
    contact = detail.get("contact") or {}
    sections = []

    lead = _line(
        ("Name", contact.get("name")),
        ("Client", contact.get("group_name")),
        ("Created", contact.get("date_added")),
        ("Source", contact.get("source")),
        ("Type", contact.get("type")),
        ("Tags", ", ".join(contact.get("tags") or [])),
    )
    if lead:
        sections.append("LEAD\n" + lead)

    attr = detail.get("attribution") or {}
    utm = attr.get("utm") or {}
    origin = _line(
        ("Platform", attr.get("platform")),
        ("Ad", attr.get("ad_name")),
        ("Ad set", attr.get("adset_name")),
        ("Campaign", attr.get("campaign_name")),
        ("Submitted", attr.get("submitted_at")),
        ("UTM source", utm.get("source")),
        ("UTM medium", utm.get("medium")),
        ("UTM campaign", utm.get("campaign")),
    )
    if origin:
        sections.append("ATTRIBUTION\n" + origin)

    opps = [_line(("Pipeline", o.get("pipeline_name")), ("Stage", o.get("stage_name")),
                  ("Status", o.get("status")), ("Value", o.get("monetary_value")),
                  ("Changed", o.get("last_status_change_at") or o.get("last_stage_change_at")))
            for o in detail.get("opportunities") or []]
    opps = [o for o in opps if o]
    if opps:
        sections.append("PIPELINE\n" + "\n".join(f"- {o}" for o in opps))

    eng = detail.get("engagement") or {}
    engagement = _line(
        ("Calls", eng.get("calls")), ("Outbound dials", eng.get("dials")),
        ("Answered", eng.get("answered")), ("Touchpoints", eng.get("touchpoints")),
        ("Seconds to close", eng.get("time_to_close_seconds")),
    )
    if engagement:
        sections.append("ENGAGEMENT\n" + engagement)

    events = []
    for e in ((journey or {}).get("events") or [])[:MAX_JOURNEY_EVENTS]:
        text = _line(("Kind", e.get("kind")), ("Title", e.get("title")),
                     ("Detail", e.get("detail")), ("At", e.get("at")))
        if text:
            events.append(f"- {text}")
    if events:
        sections.append("JOURNEY (newest first)\n" + "\n".join(events))

    calls = []
    for c in (detail.get("calls") or [])[:MAX_CALLS]:
        text = _line(("Direction", c.get("direction")), ("Status", c.get("status")),
                     ("Seconds", c.get("duration_seconds")), ("At", c.get("started_at")),
                     ("Transcript", _clip(c.get("transcript"), MAX_TRANSCRIPT_CHARS)))
        if text:
            calls.append(f"- {text}")
    if calls:
        sections.append("CALLS (newest first)\n" + "\n".join(calls))

    msgs = []
    rows = (messages or {}).get("messages") or []  # oldest first
    for m in rows[-MAX_MESSAGES:]:
        text = _line(("Direction", m.get("direction")), ("Channel", m.get("channel")),
                     ("At", m.get("date")), ("Body", _clip(m.get("body"), MAX_MESSAGE_CHARS)))
        if text:
            msgs.append(f"- {text}")
    if msgs:
        sections.append("MESSAGES (oldest first, latest 20)\n" + "\n".join(msgs))

    return "\n\n".join(sections)


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)  # Mongo returns naive UTC
    return value.astimezone(timezone.utc).isoformat()


def _payload(status: str, cached: dict | None, current_hash: str, stale: bool | None = None) -> dict:
    text = (cached or {}).get("summary")
    if stale is None:
        stale = bool(cached) and cached.get("input_hash") != current_hash
    return {
        "status": status,
        "summary": text,
        "generated_at": _iso((cached or {}).get("generated_at")) if text else None,
        "stale": bool(stale),
    }


async def _gather_inputs(db, user_id: str, contact_id: str):
    detail = await build_lead_detail(db, user_id, contact_id)
    if detail is None:
        return None, None, None
    location_id = (detail.get("contact") or {}).get("location_id")

    async def _messages():
        if not location_id:
            return None
        return await fetch_lead_messages(db, user_id, location_id, contact_id)

    journey, messages = await asyncio.gather(
        build_lead_journey(db, user_id, contact_id), _messages(), return_exceptions=True)
    if isinstance(journey, BaseException):
        logger.warning("lead summary: journey unavailable (%s)", type(journey).__name__)
        journey = None
    if isinstance(messages, BaseException) or (messages or {}).get("status") != "ok":
        if isinstance(messages, BaseException):
            logger.warning("lead summary: messages unavailable (%s)", type(messages).__name__)
        messages = None
    return detail, journey, messages


async def _generate(brief: str):
    client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT_SECONDS)
    resp = await client.chat.completions.create(
        model=LEAD_SUMMARY_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": brief},
        ],
        temperature=0.2,
        max_tokens=220,
    )
    text = (resp.choices[0].message.content or "").strip()
    return text, getattr(resp, "usage", None)


async def get_lead_summary(db, user_id: str, contact_id: str, *, refresh: bool = False) -> dict | None:
    """None when the contact isn't the user's; else the status payload
    {"status": ok|blocked|unavailable|failed, "summary", "generated_at", "stale"}."""
    detail, journey, messages = await _gather_inputs(db, user_id, contact_id)
    if detail is None:
        return None

    brief = build_summary_input(detail, journey, messages)
    current = input_hash(brief)
    key = {"user_id": user_id, "contact_id": contact_id}
    cached = await db[COLLECTION].find_one(key)

    if cached and cached.get("summary") and not refresh:
        return _payload("ok", cached, current)

    from credits import is_blocked, record_usage
    if await is_blocked(db, user_id):
        return _payload("blocked", cached, current)
    if not OPENAI_API_KEY:
        return _payload("unavailable", cached, current)

    try:
        text, usage = await _generate(brief)
        if not text:
            raise ValueError("empty summary")
    except Exception as e:
        logger.warning("lead summary generation failed (%s)", type(e).__name__)
        return _payload("failed", cached, current)

    now = datetime.now(timezone.utc)
    doc = {"summary": text, "generated_at": now, "input_hash": current, "model": LEAD_SUMMARY_MODEL}
    await db[COLLECTION].update_one(key, {"$set": doc}, upsert=True)

    try:
        await record_usage(
            db, user_id,
            model=LEAD_SUMMARY_MODEL,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model_calls=1,
            mode="managed",
            feature="lead_summary",
        )
    except Exception:
        logger.debug("Lead summary credit metering skipped", exc_info=True)

    return _payload("ok", doc, current, stale=False)
