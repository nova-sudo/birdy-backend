"""
services/dashboards_briefing.py
-------------------------------
The live briefing behind dashkit's `briefing` widget: POST /api/dashboards/{id}/briefing.

Flow: load the caller's dashboard (404 if it is not theirs) -> serve a cached
briefing unless the reader pressed Refresh -> check the user's credits -> run the queries of the board's
visible data widgets through `run_queries` -> ask the model Birdy's chat uses
for this user (`get_provider_for_user`) for
`{lead, findings:[{text,tone}]}` over those numbers -> keep only what is grounded
in them -> cache and return.

Visible data widgets: the widgets that carry `bindings.query`. Text, markdown,
heading and briefing widgets carry none and are skipped. A window or filters in
the request body replace each query's own.

Grounding (services/dashboards_grounding.py): the model is told to use only the
numbers it is given, and then every figure in its answer is checked against the
cells it was shown (value, previous value, column format). Percentages must be a
percentage column's value or the signed change of one cell; "3x", "40pp" and
"30 days" have their own rules. A finding with an ungrounded figure, or with no
grounded figure at all, is dropped; a lead like that is replaced by a fixed
sentence.

Prompt safety: dashboard, widget, column and row names are sanitised (control
characters and angle brackets removed, whitespace collapsed, 80 characters) and
the facts go to the model as one JSON document inside a delimited block that the
system prompt declares to be data.

`parts`: the lead split into runs on `**bold**` markers; text between markers
is `{text}`, text inside them is `{text, bold: true}`.

Cache: Mongo collection `dashboard_briefings`, keyed by a hash of (user,
dashboard id, `meta.updatedAt`, window, filters, focus), TTL 6 h. The user is in
the key because dashboard ids are only unique per user. Rate limit: refresh
calls are recorded in `dashboard_briefing_calls` (TTL 1 h); a user may make 10
per rolling hour.
"""

import hashlib
import json
import logging
import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from ai.prompts.dashboard_briefing import TONES, build_messages
from core.database import DB_NAME
from services.dashboards_grounding import Cell, Grounding
from services.dashboards_query import run_queries

logger = logging.getLogger(__name__)

_DASHBOARDS = "dashboards"
_CACHE = "dashboard_briefings"
_CALLS = "dashboard_briefing_calls"

CACHE_TTL = timedelta(hours=6)
RATE_WINDOW = timedelta(hours=1)
MAX_REFRESHES_PER_HOUR = 10

MAX_QUERIES = 50       # same cap as POST /api/dashboards/query
MAX_FACT_ROWS = 12     # rows of a frame shown to the model; totals are always shown
MAX_FINDINGS = 5
MAX_FACT_CELLS = 800   # cells in the facts block, so a large board cannot flood the prompt
MAX_NAME = 80

NO_DATA_LEAD = "There is no data on this board to brief yet."


def _now() -> datetime:
    """Naive UTC, which is what Mongo stores and returns for datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- the board's queries ---------------------------------------------------------

def collect_queries(doc: dict, window: Optional[dict], filters: Optional[dict]) -> list[tuple[str, dict]]:
    """(widget title, query) per widget that carries a query, with the body's window / filters laid over it."""
    out = []
    for widget in (doc.get("widgets") if isinstance(doc, dict) else None) or []:
        bindings = widget.get("bindings") if isinstance(widget, dict) else None
        query = bindings.get("query") if isinstance(bindings, dict) else None
        if not isinstance(query, dict):
            continue
        query = dict(query)
        if window:
            query["window"] = window
        if filters is not None:
            query["filters"] = filters
        out.append((str(widget.get("title") or widget.get("type") or "Widget"), query))
        if len(out) >= MAX_QUERIES:
            break
    return out


def clean_name(value) -> str:
    """A name safe to put in the prompt: no control characters, newlines or angle brackets; one-spaced; 80 chars."""
    text = "".join(" " if unicodedata.category(c).startswith(("C", "Z")) or c in "<>" else c for c in str(value or ""))
    return " ".join(text.split())[:MAX_NAME]


def _num(value):
    """A frame value for the facts: None for missing / NaN / inf, an int when whole, else rounded to 4 places."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    value = round(float(value), 4)
    return int(value) if value == int(value) else value


def build_facts(titled_frames: list[tuple[str, dict]], window_label: str):
    """(facts JSON text, cells for grounding, row counts, windows) for the frames, using the same cells in both."""
    widgets, cells, counts, windows = [], [], [], []
    budget = MAX_FACT_CELLS
    for title, frame in titled_frames:
        columns = [c for c in frame.get("columns") or [] if isinstance(c, dict) and c.get("key")]
        previous_ok = frame.get("previousAvailable", True)
        rows = (frame.get("rows") or [])
        entries = [(None, frame.get("totals") or {})] + [
            (clean_name(r.get("label") or r.get("key")), r) for r in rows[:MAX_FACT_ROWS]]
        out = []
        for row_label, entry in entries:
            for column in columns:
                if budget <= 0:
                    break
                budget -= 1
                key = column["key"]
                value = _num((entry.get("values") or {}).get(key))
                previous = _num((entry.get("previous") or {}).get(key)) if previous_ok else None
                fmt = str(column.get("format") or "")
                out.append({"row": row_label, "column": clean_name(column.get("label") or key), "format": fmt,
                            "value": value, "previous": previous})
                cells.append(Cell(value, previous, fmt))
        widgets.append({"widget": clean_name(title), "row_count": len(rows), "cells": out})
        counts.append(len(rows))
        window = frame.get("window") or {}
        if window.get("from") and window.get("to"):
            windows.append((window["from"], window["to"]))
    text = json.dumps({"period": clean_name(window_label), "widgets": widgets}, ensure_ascii=False)
    return text, cells, counts, windows


# --- the model's answer ------------------------------------------------------------

def parse_model_json(content: Optional[str]) -> Optional[dict]:
    """The JSON object in a model reply, tolerating ``` fences and prose around it; None when there is none."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def split_bold(lead: str) -> list[dict]:
    """The lead as `{text, bold?}` runs: text inside `**…**` is bold, the rest plain; stray markers are dropped."""
    parts = []
    for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", lead, flags=re.DOTALL)):
        chunk = chunk.replace("**", "")
        if not chunk:
            continue
        parts.append({"text": chunk, "bold": True} if i % 2 else {"text": chunk})
    return parts


def ground_answer(data: dict, grounding: Grounding, names: set[str], window_label: str) -> tuple[list[dict], list[dict]]:
    """(parts, findings) with every ungrounded finding dropped and an ungrounded lead replaced."""
    lead = data.get("lead")
    lead = lead.strip() if isinstance(lead, str) else ""
    plain_lead = lead.replace("**", "")
    if not plain_lead or not grounding.is_grounded(plain_lead, names):
        if plain_lead:
            logger.info("briefing: dropped an ungrounded lead")
        parts = [{"text": f"Here's how this board looks for {window_label}."}]
    else:
        parts = split_bold(lead)

    findings = []
    raw = data.get("findings")
    for item in raw if isinstance(raw, list) else []:
        text = item.get("text") if isinstance(item, dict) else None
        text = text.replace("**", "").strip() if isinstance(text, str) else ""
        if not text:
            continue
        if not grounding.is_grounded(text, names):
            logger.info("briefing: dropped an ungrounded finding")
            continue
        tone = item.get("tone")
        findings.append({"text": text, "tone": tone if tone in TONES else "neutral"})
        if len(findings) >= MAX_FINDINGS:
            break
    return parts, findings


# --- cache and rate limit ---------------------------------------------------------

def cache_key(user_id: str, dashboard_id: str, updated_at, window, filters, focus) -> str:
    payload = json.dumps([user_id, dashboard_id, updated_at, window, filters, focus],
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _take_refresh(db, user_id: str) -> None:
    """Counts a refresh against the user's hourly allowance; 429 when it is used up."""
    since = _now() - RATE_WINDOW
    used = await db[_CALLS].count_documents({"user_id": user_id, "at": {"$gte": since}})
    if used >= MAX_REFRESHES_PER_HOUR:
        raise HTTPException(status_code=429, detail="Too many briefing refreshes; try again later.")
    await db[_CALLS].insert_one({"user_id": user_id, "at": _now()})


def _served(entry: dict, cached: bool) -> dict:
    return {"parts": entry["parts"], "findings": entry["findings"],
            "generated_at": entry["generated_at"], "cached": cached}


# --- entry point -----------------------------------------------------------------

async def _ask_model(db, user_id: str, messages: list[dict]) -> Optional[dict]:
    """The model's JSON answer from the provider Birdy's chat uses for this user (their stored
    key, else Birdy's OpenAI account); raises 502 on any failure."""
    from ai.provider_factory import get_provider_for_user
    from credits import record_usage
    try:
        provider = await get_provider_for_user(user_id, db)
        response = await provider.chat_completion(messages=messages, tools=None, temperature=0.2, max_tokens=700)
    except Exception as e:
        logger.warning("briefing: LLM call failed for %s: %s: %s", user_id, type(e).__name__, e)
        raise HTTPException(status_code=502, detail="Couldn't write the briefing right now.")
    await record_usage(
        db, user_id, model=getattr(provider, "model", None),
        prompt_tokens=getattr(response.usage, "input_tokens", 0) or 0,
        completion_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        source="dashboards", feature="dashboard_briefing",
    )
    data = parse_model_json(response.content)
    if data is None:
        logger.warning("briefing: unparseable model reply for %s", user_id)
        raise HTTPException(status_code=502, detail="Couldn't write the briefing right now.")
    return data


async def generate_briefing(db, mongo_client, user_id: str, dashboard_id: str, *, window: Optional[dict] = None,
                            filters: Optional[dict] = None, focus: Optional[str] = None,
                            refresh: bool = False) -> dict:
    """`{parts, findings, generated_at, cached}` for one of the caller's dashboards."""
    focus = focus.strip() if isinstance(focus, str) else None
    focus = focus or None
    row = await db[_DASHBOARDS].find_one({"id": dashboard_id, "user_id": user_id}, {"_id": 0, "doc": 1})
    if not row:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    doc = row.get("doc") or {}

    key = cache_key(user_id, dashboard_id, (doc.get("meta") or {}).get("updatedAt"), window, filters, focus)
    if refresh:
        await _take_refresh(db, user_id)
    else:
        hit = await db[_CACHE].find_one({"_id": key})
        if hit and hit.get("created_at") and hit["created_at"] > _now() - CACHE_TTL:
            return _served(hit, True)

    queries = collect_queries(doc, window, filters)
    frames = await run_queries(db, user_id, [q for _, q in queries]) if queries else []
    titled = [(title, frame) for (title, _), frame in zip(queries, frames) if frame]
    if not titled:
        return {"parts": [{"text": NO_DATA_LEAD}], "findings": [], "generated_at": _iso(_now()), "cached": False}

    # A cache miss costs tokens: stop an out-of-credits user here (402 from check_credits).
    from credits_middleware import check_credits
    await check_credits(user_id, mongo_client)

    window_label = next((f["window"]["label"] for _, f in titled if (f.get("window") or {}).get("label")), "this period")
    facts, cells, counts, windows = build_facts(titled, window_label)
    data = await _ask_model(db, user_id, build_messages(facts, clean_name(window_label), focus))

    names = {clean_name(doc.get("title"))}
    for title, frame in titled:
        names.add(clean_name(title))
        names.update(clean_name(c.get("label")) for c in frame.get("columns") or [] if isinstance(c, dict))
        names.update(clean_name(r.get("label")) for r in frame.get("rows") or [])
    names.discard("")

    parts, findings = ground_answer(data, Grounding(cells, counts, windows), names, clean_name(window_label))
    now = _now()
    entry = {"parts": parts, "findings": findings, "generated_at": _iso(now)}
    await db[_CACHE].replace_one({"_id": key}, {**entry, "_id": key, "user_id": user_id, "created_at": now}, upsert=True)
    return _served(entry, False)


async def create_dashboard_briefing_indexes(mongo_client) -> None:
    """TTL indexes: a briefing lives 6 h, a refresh counts toward the rate limit for 1 h."""
    db = mongo_client[DB_NAME]
    await db[_CACHE].create_index("created_at", name="ttl_dashboard_briefings",
                                  expireAfterSeconds=int(CACHE_TTL.total_seconds()), background=True)
    await db[_CALLS].create_index("at", name="ttl_dashboard_briefing_calls",
                                  expireAfterSeconds=int(RATE_WINDOW.total_seconds()), background=True)
    await db[_CALLS].create_index([("user_id", 1), ("at", -1)], name="user_at", background=True)
