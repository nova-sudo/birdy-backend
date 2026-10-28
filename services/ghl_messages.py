"""Live GHL messages for one lead (fetch + send). Nothing is stored."""
import html
import logging
import re
from datetime import datetime, timezone

from integrations.gohighlevel import ghl_integration, get_subaccount_tokens
from services.lead_detail import _parse_dt

logger = logging.getLogger(__name__)

MAX_PAGES = 4
SMS_TYPES = {
    "TYPE_SMS", "TYPE_CUSTOM_SMS", "TYPE_CAMPAIGN_SMS", "TYPE_CAMPAIGN_MANUAL_SMS",
    "TYPE_CUSTOM_PROVIDER_SMS", "TYPE_RCS",
}
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"(?i)</?(?:p|div|br|li|tr|h[1-6]|blockquote)[^>]*>")


class MessagingUnavailable(Exception):
    """No location or no GHL token for this lead."""


class MessagingReconnect(Exception):
    """GHL rejected the token (401/403); the agency must reconnect."""


class MessagingFailed(Exception):
    """GHL returned another error, or the request failed."""


def channel_for(message_type: str) -> str:
    t = (message_type or "").upper()
    if t in SMS_TYPES:
        return "SMS"
    if "EMAIL" in t:
        return "Email"
    if t == "TYPE_WHATSAPP":
        return "WhatsApp"
    if t.startswith("TYPE_INSTAGRAM"):
        return "Instagram"
    if t.startswith("TYPE_FACEBOOK"):
        return "Facebook"
    if t in ("TYPE_LIVE_CHAT", "TYPE_WEBCHAT"):
        return "Chat"
    return "Other"


def _plain_text(body: str) -> str:
    text = _BLOCK_RE.sub(" ", body or "")
    text = _TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _normalize(raw: dict):
    mtype = (raw.get("messageType") or "").upper()
    if "CALL" in mtype or "VOICEMAIL" in mtype:
        return None  # calls already come from call_logs
    channel = channel_for(mtype)
    body = raw.get("body") or ""
    subject = None
    if channel == "Email":
        body = _plain_text(body)
        email_meta = (raw.get("meta") or {}).get("email")
        if isinstance(email_meta, dict):
            subject = email_meta.get("subject") or None
    dt = _parse_dt(raw.get("dateAdded") or raw.get("createdAt"))
    return {
        "id": raw.get("id"),
        "channel": channel,
        "direction": raw.get("direction"),
        "body": body,
        "subject": subject,
        "status": raw.get("status"),
        "date": dt.isoformat() if dt else None,
        "attachments": [a for a in (raw.get("attachments") or []) if isinstance(a, str)],
        "source": raw.get("source"),
    }


async def _token_for(db, user_id: str, location_id):
    if not location_id:
        return None
    tokens = await get_subaccount_tokens(user_id, db.client)
    return (tokens or {}).get(location_id, {}).get("access_token")


async def _export_all(token: str, location_id: str, contact_id: str, channel):
    """Pages for one channel call. Returns (status, raw_messages)."""
    raw, cursor = [], None
    for _ in range(MAX_PAGES):
        code, body = await ghl_integration.export_messages(
            location_id, token, contact_id, channel=channel, cursor=cursor)
        if code != 200:
            return code, []
        raw.extend(body.get("messages") or [])
        cursor = body.get("nextCursor")
        if not cursor:
            break
    return 200, raw


async def fetch_lead_messages(db, user_id: str, location_id, contact_id: str) -> dict:
    """{"status": ok|reconnect|unavailable, "messages": [...]}. Never raises."""
    try:
        token = await _token_for(db, user_id, location_id)
        if not token:
            return {"status": "unavailable", "messages": []}

        raw, failure = [], None
        for channel in (None, "Email"):
            code, rows = await _export_all(token, location_id, contact_id, channel)
            if code in (401, 403):
                return {"status": "reconnect", "messages": []}
            if code != 200:
                failure = code
                continue
            raw.extend(rows)
        if failure is not None:
            return {"status": "unavailable", "messages": []}

        seen, messages = set(), []
        for r in raw:
            m = _normalize(r) if isinstance(r, dict) else None
            if m is None or m["id"] in seen:
                continue
            seen.add(m["id"])
            messages.append(m)
        messages.sort(key=lambda m: m["date"] or "")
        return {"status": "ok", "messages": messages}
    except Exception as e:
        logger.warning("fetch_lead_messages failed for location %s: %s", location_id, type(e).__name__)
        return {"status": "unavailable", "messages": []}


async def send_lead_message(db, user_id: str, location_id, contact_id: str, channel: str,
                            message: str, subject: str = None) -> dict:
    token = await _token_for(db, user_id, location_id)
    if not token:
        raise MessagingUnavailable()

    payload = {"type": channel, "contactId": contact_id, "message": message}
    if channel == "Email":
        payload["subject"] = subject
        payload["html"] = html.escape(message).replace("\n", "<br>")

    code, body = await ghl_integration.send_message(token, payload)
    if code in (401, 403):
        raise MessagingReconnect()
    if not 200 <= code < 300:
        raise MessagingFailed()

    return {
        "id": body.get("messageId"),
        "channel": channel,
        "direction": "outbound",
        "body": message,
        "subject": subject if channel == "Email" else None,
        "status": "sent",
        "date": datetime.now(timezone.utc).isoformat(),
        "attachments": [],
        "source": "api",
    }
