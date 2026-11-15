"""The MCP activity log: one `mcp_activity` row per external tool call.

Rows are written by the access middleware after a call (see ai/mcp/access.py). Everything stored is
redacted first: `confirm_code` is dropped at any depth and phones/emails are masked; message bodies stay.
Rate-limited refusals do not get a row each: one row per user per minute carries a `count`.
"""

import json
import logging
import re
from datetime import datetime, timezone

from ai.mcp.activity_labels import LABELS
from ai.mcp.outcome import current_activity_id
from core.database import DB_NAME

logger = logging.getLogger(__name__)

COLLECTION = "mcp_activity"
RETENTION_SECONDS = 180 * 24 * 3600
EXCERPT_CHARS = 2000
_MAX_CLIENTS = 5

_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# A phone-looking run: optional +, digits with spaces, dots, dashes or brackets between. It may be followed by
# an extension ("x22"), but not by more digits or a colon (a time). A candidate is masked with 10-15 digits when
# it has a +, a phone separator, or 10-12 bare digits (12 covers bare international numbers such as UK mobiles).
# Longer bare runs (ad ids, ms timestamps) are left alone. A run whose only separator is one dot is a decimal,
# and an ISO date inside the run means a datetime, so neither is masked. Known over-mask: a bare 10-digit
# unix timestamp looks exactly like a US number.
_PHONE = re.compile(r"(?<![\w])\+?\(?\d[\d\s().-]{8,}\d(?![\d:])")
_PHONE_KEYS = {"phone", "phone_number", "phonenumber", "mobile", "cell", "to_number", "from_number"}
_EMAIL_KEYS = {"email", "email_address", "emailaddress"}


def _mask_phone_digits(digits: str) -> str:
    return f"•••• {digits[-4:]}"


def _mask_email(match: re.Match) -> str:
    return f"{match.group(1)}•••@{match.group(2)}"


def _mask_phone(match: re.Match) -> str:
    raw = match.group(0)
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        return raw
    if re.search(r"\d{4}-\d{2}-\d{2}", raw) or (raw.count(".") == 1 and not re.search(r"[\s()+-]", raw)):
        return raw  # a datetime or a decimal
    if len(digits) > 12 and not raw.startswith("+") and not re.search(r"[\s().-]", raw):
        return raw  # a long bare id
    return _mask_phone_digits(digits)


def _mask_text(text: str) -> str:
    return _PHONE.sub(_mask_phone, _EMAIL.sub(_mask_email, text))


def redact(value):
    """A copy of `value` safe to store: no `confirm_code` at any depth, phones and emails masked."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "confirm_code":
                continue
            lowered = str(key).lower()
            if lowered in _PHONE_KEYS and isinstance(item, (str, int)) and not isinstance(item, bool):
                digits = re.sub(r"\D", "", str(item))
                out[key] = _mask_phone_digits(digits) if len(digits) >= 4 else "••••"
            elif lowered in _EMAIL_KEYS and isinstance(item, str):
                out[key] = _EMAIL.sub(_mask_email, item) if "@" in item else "•••"
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _mask_text(value)
    return value


def _ids(args: dict, *keys) -> list[str]:
    found = []
    for key in keys:
        value = (args or {}).get(key)
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, str) and v.strip():
                found.append(v.strip())
    return found


async def resolve_clients(db, user_id: str, args: dict) -> list[dict]:
    """[{"id", "name"}] for the clients a call touched, from the arg names tools use. Owned groups only."""
    from services.lead_detail import find_lead_contact

    args = args or {}
    group_ids = _ids(args, "client_group_id", "group_id", "group_ids", "client_group_ids", "target_group_ids")
    names = _ids(args, "client", "client_name")
    for card_id in _ids(args, "card_id"):
        card = await db["agent_cards"].find_one({"_id": card_id, "user_id": user_id}, {"client_group_id": 1})
        if card and card.get("client_group_id"):
            group_ids.append(card["client_group_id"])
    for contact_id in _ids(args, "contact_id"):
        contact = await find_lead_contact(db, user_id, contact_id)
        if contact and contact.get("client_group_id"):
            group_ids.append(contact["client_group_id"])
    for alert_id in _ids(args, "alert_id"):
        alert = await db["alerts"].find_one({"id": alert_id, "user_id": user_id}, {"target_group_ids": 1})
        group_ids.extend((alert or {}).get("target_group_ids") or [])
    if not group_ids and not names:
        return []
    groups = await db["client_groups"].find({"user_id": user_id}, {"id": 1, "name": 1}).to_list(length=None)
    by_id = {g.get("id"): g for g in groups}
    out, seen = [], set()
    for gid in group_ids:
        g = by_id.get(gid)
        if g and gid not in seen:
            seen.add(gid)
            out.append({"id": gid, "name": g.get("name")})
    for needle in names:
        g = by_id.get(needle) or next((x for x in groups if (x.get("name") or "").lower() == needle.lower()), None)
        if g and g.get("id") not in seen:
            seen.add(g["id"])
            out.append({"id": g["id"], "name": g.get("name")})
    return out[:_MAX_CLIENTS]


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def summarize(tool: str, args: dict, clients: list[dict], outcome: dict | None) -> str:
    """One plain sentence: the tool's label, plus client names; previews and refusals are worded as such."""
    label = (LABELS.get(tool) or {}).get("label") or tool.replace("_", " ").capitalize()
    kind = (outcome or {}).get("kind")
    names = [c["name"] for c in clients if c.get("name")]
    if names:
        shown = " and ".join(names[:2]) if len(names) <= 2 else f"{names[0]}, {names[1]} and {len(names) - 2} more"
        label = f"{label} for {shown}"
    if kind == "preview":
        return f"Previewed: {_lower_first(label)}. Nothing changed yet"
    if kind in ("refused", "bad_code", "state_changed"):
        return f"Could not do this: {_lower_first(label)}. Nothing changed"
    return label


def link_for(tool: str, args: dict) -> str | None:
    """The tool's Birdy page, filled from the args; None when it has no page or a placeholder has no value."""
    template = (LABELS.get(tool) or {}).get("link")
    if not template:
        return None
    try:
        values = {k: v for k, v in (args or {}).items() if isinstance(v, (str, int)) and str(v).strip()}
        return template.format(**values)
    except (KeyError, IndexError):
        return None


def _excerpt(result) -> str | None:
    if result is None:
        return None
    text = result if isinstance(result, str) else json.dumps(redact(result), default=str, ensure_ascii=False)
    if isinstance(result, str):
        text = _mask_text(text)
    return text[:EXCERPT_CHARS]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def record_call(db, *, user_id, grant_id, app_name, app_kind, access, tool, kind, refusal_reason, args, result,
                      result_status, credits, chars, duration_ms, confirmed_after, clients=None) -> str:
    """Insert one activity row and return its id (the activity id the middleware pre-allocated)."""
    from uuid import uuid4

    activity_id = current_activity_id() or uuid4().hex
    clients = await resolve_clients(db, user_id, args) if clients is None else clients
    await db[COLLECTION].insert_one({
        "id": activity_id,
        "user_id": user_id,
        "grant_id": grant_id,
        "app_name": app_name,
        "app_kind": app_kind,
        "access": access,
        "tool": tool,
        "kind": kind,
        "refusal_reason": refusal_reason,
        "client_group_ids": [c["id"] for c in clients],
        "client_names": [c.get("name") for c in clients],
        "summary": summarize(tool, args, clients, {"kind": kind}),
        "args": redact(args or {}),
        "result_status": result_status,
        "result_excerpt": _excerpt(result),
        "confirmed_after": confirmed_after,
        "link": link_for(tool, args),
        "credits": credits,
        "response_chars": chars,
        "duration_ms": duration_ms,
        "created_at": _now(),
    })
    return activity_id


async def record_rate_limited(db, user_id: str, grant_id: str | None, app_name: str | None, app_kind: str = "other"):
    """Count a rate-limited refusal on this user's row for the current minute (one row per user per minute)."""
    now = _now()
    minute = now.replace(second=0, microsecond=0)
    await db[COLLECTION].update_one(
        {"user_id": user_id, "kind": "refused", "refusal_reason": "rate_limit", "minute": minute},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {
                "id": current_activity_id(), "grant_id": grant_id, "app_name": app_name, "app_kind": app_kind,
                "access": None, "tool": None, "summary": "Too many requests in a minute. Some were refused",
                "args": {}, "result_status": "error", "result_excerpt": None, "confirmed_after": None, "link": None,
                "client_group_ids": [], "client_names": [],
                "credits": {"request": 0, "data": 0, "live": 0, "total": 0}, "response_chars": 0,
                "duration_ms": 0, "created_at": now,
            },
        },
        upsert=True,
    )


_APP_KINDS = (  # same mapping as the frontend's AppLogo.jsx, plus Claude Code ahead of Claude
    (re.compile(r"claude[\s_-]*code", re.I), "claude-code"),
    (re.compile(r"claude", re.I), "claude"),
    (re.compile(r"chatgpt|openai", re.I), "chatgpt"),
    (re.compile(r"cursor", re.I), "cursor"),
)


def app_kind_for(app_name: str | None) -> str:
    for pattern, kind in _APP_KINDS:
        if pattern.search(app_name or ""):
            return kind
    return "other"


async def create_mcp_activity_indexes(mongo_client):
    coll = mongo_client[DB_NAME][COLLECTION]
    await coll.create_index("created_at", expireAfterSeconds=RETENTION_SECONDS)
    await coll.create_index([("user_id", 1), ("created_at", -1)])
    await coll.create_index("id", unique=True)
