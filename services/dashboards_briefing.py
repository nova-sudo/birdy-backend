"""
services/dashboards_briefing.py
-------------------------------
The live briefing behind dashkit's `briefing` widget: POST /api/dashboards/{id}/briefing.

Flow: load the caller's dashboard (404 if it is not theirs) -> serve a cached
briefing unless the reader pressed Refresh -> check the user's credits -> run the queries of the board's
visible data widgets through `run_queries` -> ask Claude Sonnet
(AnthropicProvider, Birdy's server key, `ai.config.ANTHROPIC_MODEL`) for
`{lead, findings:[{text,tone}]}` over those numbers -> keep only what is grounded
in them -> cache and return.

Visible data widgets: the widgets that carry `bindings.query`. Text, markdown,
heading and briefing widgets carry none and are skipped. A window or filters in
the request body replace each query's own.

Grounding: the model is told to use only the numbers it is given, and then every
number in its answer is checked. A number is grounded when it is within 1%
(relative) of a frame value (totals, rows, previous values), or of a percentage
change between two values of the same frame column, or when it equals the value
rounded to the precision it is shown at ("$1.2k" matches 1234). Years, dates and
durations ("Oct 20", "2026", "last 30 days") are not numbers to ground, and
neither are digits inside the board's own names ("Campaign 2"). A finding with
an ungrounded number is dropped; a lead with one is replaced by a fixed
sentence.

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
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException

from ai.prompts.dashboard_briefing import TONES, build_messages
from core.database import DB_NAME
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
MAX_PAIR_POOL = 24     # values per frame column considered for derived percentage changes

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


def _number(value) -> str:
    """A frame value as the model reads it: whole numbers plain, others to 2 decimals (4 for tiny ones)."""
    if value is None:
        return "no data"
    if float(value) == int(value):
        return str(int(value))
    return f"{value:.4f}".rstrip("0") if abs(value) < 0.01 else f"{value:.2f}"


def build_facts(titled_frames: list[tuple[str, dict]]) -> str:
    """`widget title → column label: value (previous: value)`, one line per number the model may use."""
    lines = []
    for title, frame in titled_frames:
        columns = frame.get("columns") or []
        previous_ok = frame.get("previousAvailable", True)

        def line(prefix: str, entry: dict, column: dict):
            key = column["key"]
            value = (entry.get("values") or {}).get(key)
            text = f"{title} → {prefix}{column['label']}: {_number(value)}"
            prev = (entry.get("previous") or {}).get(key) if previous_ok else None
            if prev is not None:
                text += f" (previous: {_number(prev)})"
            lines.append(text)

        for column in columns:
            line("", frame.get("totals") or {}, column)
        for row in (frame.get("rows") or [])[:MAX_FACT_ROWS]:
            for column in columns:
                line(f"{row.get('label') or row.get('key')} / ", row, column)
    return "\n".join(lines)


# --- grounding -------------------------------------------------------------------

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
           r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")

# Spans that look like numbers but are dates, years or durations; blanked before extraction.
_NOT_NUMBERS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b\d{4}-\d{2}-\d{2}\b",                                    # 2026-10-20
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",                         # 10/20, 10/20/2026
    r"\b\d{1,2}-\d{1,2}-\d{2,4}\b",                              # 20-10-2026
    rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?",   # Oct 20, October 20th, 2026
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTHS}\b(?:,?\s+\d{{4}})?",  # 20 Oct, 20th of October
    r"(?<![\w.,$])(?:19|20)\d{2}(?![\w%]|[.,]\d)",               # a bare 4-digit year
    r"\b\d+(?:\.\d+)?[\s-]*(?:day|week|month|quarter|year|hour|minute)s?\b",  # 30 days, 7-day
)]

_NUMBER = re.compile(
    r"(?<![\w.])[$€£]?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?P<suf>[kKmMbB]|%)?(?![A-Za-z0-9])"
)
_MULTIPLIER = {"k": 1e3, "m": 1e6, "b": 1e9}
REL_TOL = 0.01


def _blank_names(text: str, names: set[str]) -> str:
    """Remove the board's own names, longest first: the digits in "Campaign 2" are a name, not a figure."""
    for name in sorted((n for n in names if n), key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def extract_numbers(text: str, names: frozenset | set = frozenset()) -> list[tuple[float, float, bool]]:
    """(value, rounding half-width, is_percent) per figure in `text`.

    The value has its k/m/b suffix applied; the half-width is half a unit of the
    last shown digit, so "1.2k" is 1200 ± 50 and "12" is 12 ± 0.5.
    """
    text = _blank_names(text, set(names))
    for pattern in _NOT_NUMBERS:
        text = pattern.sub(" ", text)
    found = []
    for m in _NUMBER.finditer(text):
        digits = m.group("num").replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        suffix = (m.group("suf") or "").lower()
        mult = _MULTIPLIER.get(suffix, 1.0)
        found.append((float(digits) * mult, 0.5 * 10 ** -decimals * mult, suffix == "%"))
    return found


def _close(n: float, ref: float, half_width: float) -> bool:
    return abs(n - ref) <= max(REL_TOL * abs(ref), half_width) + 1e-9


class Grounding:
    """What a number in the model's answer may equal: the frames' values and changes between them."""

    def __init__(self, frames: list[dict]):
        self.values: list[float] = []
        self._pools: list[list[float]] = []   # per frame column: every value of it, current and previous
        for frame in frames:
            entries = [frame.get("totals") or {}] + list(frame.get("rows") or [])
            # a row count is a figure the model may cite ("3 clients")
            self.values.append(float(len(frame.get("rows") or [])))
            keys = {c["key"] for c in frame.get("columns") or []}
            for key in keys:
                pool = []
                for entry in entries:
                    for side in ("values", "previous"):
                        v = (entry.get(side) or {}).get(key)
                        if isinstance(v, (int, float)) and not isinstance(v, bool):
                            pool.append(float(v))
                self.values.extend(pool)
                self._pools.append(sorted(set(pool), key=abs, reverse=True)[:MAX_PAIR_POOL])

    def _is_change(self, n: float, half_width: float) -> bool:
        for pool in self._pools:
            for a in pool:
                for b in pool:
                    if a != b and b != 0 and _close(n, abs((a - b) / b) * 100, half_width):
                        return True
        return False

    def holds(self, n: float, half_width: float, percent: bool) -> bool:
        n = abs(n)
        for v in self.values:
            if _close(n, abs(v), half_width):
                return True
            if percent and _close(n, abs(v) * 100, half_width):  # a fraction-scale ratio shown as a percent
                return True
        return percent and self._is_change(n, half_width)


def is_grounded(text: str, grounding: Grounding, names: set[str]) -> bool:
    return all(grounding.holds(n, hw, pct) for n, hw, pct in extract_numbers(text, names))


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
    if not plain_lead or not is_grounded(plain_lead, grounding, names):
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
        if not is_grounded(text, grounding, names):
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
    """The model's JSON answer from Claude Sonnet via AnthropicProvider; raises 502 on any failure."""
    from ai.config import ANTHROPIC_MODEL
    from ai.providers.anthropic_provider import AnthropicProvider
    from credits import record_usage
    try:
        # Birdy's own Anthropic key (the provider reads it from the environment); it raises
        # ValueError when none is configured, which is a 502 like any other LLM failure.
        provider = AnthropicProvider(model=ANTHROPIC_MODEL)
        response = await provider.chat_completion(messages=messages, tools=None, temperature=0.2, max_tokens=700)
    except Exception as e:
        logger.warning("briefing: LLM call failed for %s: %s", user_id, e)
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
    data = await _ask_model(db, user_id, build_messages(build_facts(titled), window_label, focus))

    names = {doc.get("title"), window_label}
    for title, frame in titled:
        names.add(title)
        names.add((frame.get("window") or {}).get("label"))
        names.update(c.get("label") for c in frame.get("columns") or [])
        names.update(r.get("label") for r in frame.get("rows") or [])
    names = {str(n) for n in names if n}

    parts, findings = ground_answer(data, Grounding([f for _, f in titled]), names, window_label)
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
