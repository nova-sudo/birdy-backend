"""Live GHL messages for one lead (fetch + send). Nothing is stored."""
import asyncio
import html
import logging
import re
from datetime import datetime, timezone

from integrations.gohighlevel import ghl_integration
from services.ghl_live import token_for as _token_for
from services.lead_detail import _parse_dt

logger = logging.getLogger(__name__)

MAX_PAGES = 4
# Not conversation messages: activity log, team-only notes, system rows.
NON_MESSAGE_TYPES = {
    "TYPE_INTERNAL_COMMENT", "TYPE_LIVE_CHAT_INFO_MESSAGE", "TYPE_FORM_SUBMISSION", "TYPE_SMS_REACTION",
}
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"(?i)</?(?:p|div|br|li|tr|h[1-6]|blockquote)[^>]*>")


class MessagingUnavailable(Exception):
    """No location or no GHL token for this lead."""


class MessagingReconnect(Exception):
    """GHL rejected the token (401/403); the agency must reconnect."""


class MessagingFailed(Exception):
    """GHL definitely rejected the message (4xx), or it was never sent."""


class MessagingUnconfirmed(Exception):
    """Network error, timeout or GHL 5xx: the message may still have been sent."""


class RecordingNotFound(Exception):
    """No such call for this lead, or GHL holds no recording for it."""


def channel_for(message_type: str) -> str:
    t = (message_type or "").upper()
    if t == "TYPE_RCS" or "SMS" in t:
        return "SMS"
    if "EMAIL" in t:
        return "Email"
    if t == "TYPE_WHATSAPP":
        return "WhatsApp"
    if "INSTAGRAM" in t:
        return "Instagram"
    if "FACEBOOK" in t:
        return "Facebook"
    if t in ("TYPE_LIVE_CHAT", "TYPE_WEBCHAT"):
        return "Chat"
    return "Other"


def _plain_text(body: str) -> str:
    text = _BLOCK_RE.sub(" ", body or "")
    text = _TAG_RE.sub("", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _is_call(raw: dict) -> bool:
    mtype = (raw.get("messageType") or "").upper()
    return "CALL" in mtype or "VOICEMAIL" in mtype


def _call_meta(raw: dict):
    """(status, duration_seconds) from either meta shape GHL sends."""
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    call = meta.get("call") if isinstance(meta.get("call"), dict) else {}
    status = call.get("status") or meta.get("callStatus") or raw.get("status")
    duration = call.get("duration", meta.get("callDuration"))
    try:
        duration = int(float(duration)) if duration not in (None, "") else None
    except (TypeError, ValueError):
        duration = None
    return status, duration


def _normalize_call(raw: dict):
    """A GHL call message in the shape of a call_logs row (plus ghl_message_id)."""
    if not raw.get("id"):
        return None
    status, duration = _call_meta(raw)
    dt = _parse_dt(raw.get("dateAdded") or raw.get("createdAt"))
    if not dt:
        return None
    return {
        "id": f"ghl:{raw['id']}",
        "ghl_message_id": raw["id"],
        "source": "ghl",
        "direction": raw.get("direction"),
        "status": status,
        "duration_seconds": duration,
        "started_at": dt.astimezone(timezone.utc).isoformat(),
        "recording_url": None,
    }


def _normalize(raw: dict):
    mtype = (raw.get("messageType") or "").upper()
    if _is_call(raw):
        return None  # returned separately as calls
    if mtype.startswith("TYPE_ACTIVITY_") or mtype in NON_MESSAGE_TYPES or not raw.get("id"):
        return None
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
        "date": dt.astimezone(timezone.utc).isoformat() if dt else None,
        "attachments": [a for a in (raw.get("attachments") or []) if isinstance(a, str)],
        "source": raw.get("source"),
    }


async def _export_all(token: str, location_id: str, contact_id: str, channel):
    """Pages for one channel call, newest first. Returns (status, raw_messages, truncated)."""
    raw, cursor = [], None
    for _ in range(MAX_PAGES):
        code, body = await ghl_integration.export_messages(
            location_id, token, contact_id, channel=channel, cursor=cursor)
        if code != 200:
            return code, [], False
        raw.extend(body.get("messages") or [])
        cursor = body.get("nextCursor")
        if not cursor:
            return 200, raw, False
    return 200, raw, True  # cap hit with more pages left; the oldest are dropped


def _sort_key(message: dict):
    dt = _parse_dt(message["date"])
    return dt or datetime.min.replace(tzinfo=timezone.utc)


async def fetch_lead_messages(db, user_id: str, location_id, contact_id: str) -> dict:
    """{"status": ok|reconnect|unavailable, "messages": [...], "calls": [...]}. Never raises.

    "calls" are the GHL call messages not already in call_logs, oldest first.

    "ok" may carry partial=True (one channel call failed) and truncated=True
    (page cap hit; oldest messages omitted).
    """
    try:
        token = await _token_for(db, user_id, location_id)
        if not token:
            return {"status": "unavailable", "messages": [], "calls": []}

        results = await asyncio.gather(
            _export_all(token, location_id, contact_id, None),
            _export_all(token, location_id, contact_id, "Email"),
        )
        if any(code in (401, 403) for code, _, _ in results):
            return {"status": "reconnect", "messages": [], "calls": []}
        good = [r for r in results if r[0] == 200]
        if not good:
            return {"status": "unavailable", "messages": [], "calls": []}

        seen, messages, calls = set(), [], []
        for _, rows, _ in good:
            for r in rows:
                if not isinstance(r, dict) or r.get("id") in seen:
                    continue
                if _is_call(r):
                    c = _normalize_call(r)
                    if c:
                        seen.add(r["id"])
                        calls.append(c)
                    continue
                m = _normalize(r)
                if m is None:
                    continue
                seen.add(m["id"])
                messages.append(m)
        messages.sort(key=_sort_key)
        calls = await _without_logged(db, user_id, calls)
        calls.sort(key=lambda c: _parse_dt(c["started_at"]))
        return {
            "status": "ok",
            "messages": messages,
            "calls": calls,
            "partial": len(good) < len(results),
            "truncated": any(t for _, _, t in good),
        }
    except Exception as e:
        logger.warning("fetch_lead_messages failed for location %s: %s", location_id, type(e).__name__)
        return {"status": "unavailable", "messages": [], "calls": []}


async def _without_logged(db, user_id: str, calls: list) -> list:
    """Drop GHL calls a workflow webhook already put in call_logs (shown from there)."""
    if not calls:
        return calls
    try:
        ids = [c["ghl_message_id"] for c in calls]
        logged = await db["call_logs"].distinct(
            "source_event_id", {"user_id": user_id, "source": "ghl", "source_event_id": {"$in": ids}})
    except Exception as e:
        logger.warning("call_logs de-duplication failed: %s", type(e).__name__)
        return calls
    logged = set(logged)
    return [c for c in calls if c["ghl_message_id"] not in logged]


def _sniff_audio(data: bytes):
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return None


async def _owned_call(token: str, location_id: str, contact_id: str, message_id: str) -> None:
    """Raise unless message_id is a call on this contact in this location."""
    code, message = await ghl_integration.get_message(token, message_id)
    if code in (401, 403):
        raise MessagingReconnect()
    if code in (400, 404, 422):
        raise RecordingNotFound()
    if code != 200:
        raise MessagingFailed()
    if (message.get("contactId") != contact_id
            or (message.get("locationId") and message.get("locationId") != location_id)
            or not _is_call(message)):
        raise RecordingNotFound()


async def _call_token(db, user_id: str, location_id) -> str:
    try:
        token = await _token_for(db, user_id, location_id)
    except Exception as e:
        logger.warning("call token lookup failed for location %s: %s", location_id, type(e).__name__)
        raise MessagingFailed()
    if not token:
        raise MessagingUnavailable()
    return token


async def fetch_call_recording(db, user_id: str, location_id, contact_id: str, message_id: str):
    """(audio, content_type) for a GHL call on this lead.

    Raises MessagingUnavailable / MessagingReconnect / RecordingNotFound / MessagingFailed.
    """
    token = await _call_token(db, user_id, location_id)
    await _owned_call(token, location_id, contact_id, message_id)
    code, audio, content_type = await ghl_integration.fetch_call_recording(location_id, token, message_id)
    if code in (401, 403):
        raise MessagingReconnect()
    if code in (400, 404, 422) or (code == 200 and not audio):
        raise RecordingNotFound()
    if code != 200:
        raise MessagingFailed()
    if not content_type.startswith("audio/"):
        # GHL may label the file generically; the browser needs a real audio type.
        content_type = _sniff_audio(audio)
        if not content_type:
            raise MessagingFailed()
    return audio, content_type


async def fetch_call_transcript(db, user_id: str, location_id, contact_id: str, message_id: str):
    """The call's transcript as text, or None when GHL has none.

    Raises MessagingUnavailable / MessagingReconnect / RecordingNotFound / MessagingFailed.
    """
    token = await _call_token(db, user_id, location_id)
    await _owned_call(token, location_id, contact_id, message_id)
    code, sentences = await ghl_integration.fetch_call_transcription(location_id, token, message_id)
    if code in (401, 403):
        raise MessagingReconnect()
    if code in (400, 404, 422):
        return None
    if code != 200:
        raise MessagingFailed()
    rows = [s for s in sentences if isinstance(s, dict) and str(s.get("transcript") or "").strip()]
    rows.sort(key=lambda s: (s.get("sentenceIndex") is None, s.get("sentenceIndex") or 0))
    return "\n".join(str(s["transcript"]).strip() for s in rows) or None


async def send_lead_message(db, user_id: str, location_id, contact_id: str, channel: str,
                            message: str, subject: str = None) -> dict:
    try:
        token = await _token_for(db, user_id, location_id)
    except Exception as e:
        logger.warning("send token lookup failed for location %s: %s", location_id, type(e).__name__)
        raise MessagingFailed()
    if not token:
        raise MessagingUnavailable()

    payload = {"type": channel, "contactId": contact_id, "message": message}
    if channel == "Email":
        payload["subject"] = subject
        payload["html"] = html.escape(message).replace("\n", "<br>")

    code, body = await ghl_integration.send_message(token, payload)
    if code in (401, 403):
        raise MessagingReconnect()
    if code == 0 or code >= 500:
        raise MessagingUnconfirmed()
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
