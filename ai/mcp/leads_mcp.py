"""
ai/mcp/leads_mcp.py
-------------------
One lead (a GoHighLevel contact): the full detail page, the live message thread and call transcripts.
The send tool is live (two steps). A lead is identified by its contact_id and must belong to one of the caller's client groups. user_id always
comes from the token. Messaging problems come back as {"error": ...} dicts, not exceptions.
"""

import re
from typing import Literal

from fastmcp.exceptions import ToolError

from ai.mcp.access import LIVE, READ
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services.ghl_messages import (
    MAX_MESSAGE_LENGTH, MAX_SUBJECT_LENGTH, MessageInvalid, MessagingFailed, MessagingReconnect,
    MessagingUnavailable, MessagingUnconfirmed, RecordingNotFound, check_sendable,
    fetch_call_transcript, fetch_lead_messages,
)
from services.ghl_messages import send_lead_message as send_lead_message_service
from services.ghl_live import safe_token_for
from services.lead_detail import build_lead_detail, find_lead_contact
from services.lead_journey import build_lead_journey
from services.lead_summary import get_lead_summary

_RECONNECT = {"error": "GoHighLevel needs reconnecting in Birdy (Settings → Integrations)."}
_UNAVAILABLE = {"error": "GoHighLevel is not available for this lead's client (not connected, or it did not respond)."}
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _db():
    return get_db(get_shared_mongo_client())


async def _owned_lead(db, user_id: str, contact_id: str) -> dict:
    """The lead's contact row, only when it belongs to one of this user's client groups."""
    doc = await find_lead_contact(db, user_id, contact_id)
    group_id = doc.get("client_group_id") if doc else None
    if doc and group_id and not await db["client_groups"].find_one({"id": group_id, "user_id": user_id}):
        doc = None
    if not doc:
        raise ToolError("Lead not found. Use get_ghl_contacts or get_unified_leads to find a lead's contact_id.")
    return doc


@mcp.tool(**READ)
async def get_lead(contact_id: str) -> dict:
    """Everything Birdy knows about one lead: contact details, opportunities and their stages, ad attribution (campaign, ad, form answers), calls, a newest-first journey timeline (ad click, form fill, site visits, calls, appointments, stage changes) and a short AI summary of the lead.

    Use it for "tell me about this lead", "what did Ada do before she booked?" or "where did this lead come from?".
    Returns {lead, journey, summary}; summary.status is ok, generating, unavailable, blocked or failed.
    lead.calls and lead.engagement count the calls Birdy has logged (call center and webhook calls). Calls that
    exist only in the GoHighLevel conversation are not in them: get_lead_messages returns those, so read both
    before saying a lead was never called.

    Args:
        contact_id: The GoHighLevel contact id, as returned by get_ghl_contacts or get_unified_leads.
    """
    db, user_id = _db(), _current_user_id()
    await _owned_lead(db, user_id, contact_id)
    lead = await build_lead_detail(db, user_id, contact_id)
    if lead is None:
        raise ToolError("Lead not found.")
    return {
        "lead": lead,
        "journey": await build_lead_journey(db, user_id, contact_id),
        "summary": await get_lead_summary(db, user_id, contact_id),
    }


@mcp.tool(**READ)
async def get_lead_messages(contact_id: str) -> dict:
    """The live GoHighLevel conversation with one lead: SMS and email messages oldest first, plus call records that are not already logged.

    Use it for "what was said to this lead?" or before judging how a lead was followed up.
    Returns {status: "ok", messages, calls, partial?, truncated?}. If GoHighLevel needs reconnecting or is unavailable it returns {"error": "..."} instead.

    Args:
        contact_id: The GoHighLevel contact id, as returned by get_ghl_contacts or get_unified_leads.
    """
    db, user_id = _db(), _current_user_id()
    doc = await _owned_lead(db, user_id, contact_id)
    out = await fetch_lead_messages(db, user_id, doc.get("location_id"), contact_id)
    if out.get("status") == "reconnect":
        return dict(_RECONNECT)
    if out.get("status") != "ok":
        return dict(_UNAVAILABLE)
    return out


@mcp.tool(**READ)
async def get_call_transcript(contact_id: str, message_id: str) -> dict:
    """The transcript of one recorded call with a lead, as plain text.

    Use it to read what was said on a call. Get the message_id from the "calls" list of get_lead_messages or the lead's calls in get_lead.
    Returns {transcript: text}, or {transcript: null} when GoHighLevel holds none. A message_id that is not a call on this lead is refused. GoHighLevel problems return {"error": "..."}.

    Args:
        contact_id: The GoHighLevel contact id, as returned by get_ghl_contacts or get_unified_leads.
        message_id: The call's GoHighLevel message id.
    """
    db, user_id = _db(), _current_user_id()
    doc = await _owned_lead(db, user_id, contact_id)
    if not _MESSAGE_ID.match(message_id or ""):
        raise ToolError("message_id is not a valid call message id.")
    try:
        transcript = await fetch_call_transcript(db, user_id, doc.get("location_id"), contact_id, message_id)
    except RecordingNotFound:
        raise ToolError("No call with that message_id on this lead.")
    except MessagingReconnect:
        return dict(_RECONNECT)
    except (MessagingUnavailable, MessagingFailed):
        return dict(_UNAVAILABLE)
    return {"transcript": transcript}


# ---------------------------------------------------------------------------
# Live (two steps): a real SMS or email to a lead
# ---------------------------------------------------------------------------

_GHL_CHANNEL = {"sms": "SMS", "email": "Email"}


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"•••• {digits[-4:]}" if digits else "••••"


def _mask_email(email: str) -> str:
    local, _, domain = (email or "").strip().partition("@")
    return f"{local[:1]}•••@{domain}"


def _lead_name(contact: dict) -> str:
    name = " ".join(p for p in (contact.get("firstName"), contact.get("lastName")) if p).strip()
    return name or contact.get("name") or "this lead"


@mcp.tool(**LIVE)
async def send_lead_message(contact_id: str, channel: Literal["sms", "email"], body: str,
                            subject: str | None = None, confirm_code: str | None = None) -> dict:
    """Send a real SMS or email to a lead through GoHighLevel, from the client's own GoHighLevel number or address. It cannot be undone.

    Two steps. Called without confirm_code it sends nothing and returns a preview (who it goes to with the phone or email partly hidden, the channel, the subject and the exact message) and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat exactly the same contact_id, channel, body and subject. Any change (even one character) is refused and nothing is sent. A lead with no phone (sms) or no email (email), an email without a subject, an empty message, a message over 1600 characters, or GoHighLevel needing a reconnect returns {"error": ...} and no code.
    Returns {ok: true, message} when sent. {ok: false, error} when GoHighLevel rejected it or could not confirm it: if it could not confirm, check get_lead_messages before sending again.

    Args:
        contact_id: The GoHighLevel contact id, as returned by get_ghl_contacts or get_unified_leads.
        channel: sms or email.
        body: The message text (up to 1600 characters). Leading and trailing whitespace is trimmed.
        subject: Required for email (up to 300 characters), ignored for sms.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id = _db(), _current_user_id()
    doc = await _owned_lead(db, user_id, contact_id)
    ghl_channel = _GHL_CHANNEL.get(channel)
    if not ghl_channel:
        return {"error": "channel must be sms or email."}
    # What the user is asked about is bound to the code exactly as typed; both steps then clean it the same way.
    args = {"contact_id": contact_id, "channel": channel, "body": body, "subject": subject}
    message = (body or "").strip()
    if not message:
        return {"error": "The message is empty."}
    if len(message) > MAX_MESSAGE_LENGTH:
        return {"error": f"The message is too long ({len(message)} characters; at most {MAX_MESSAGE_LENGTH})."}
    if subject is not None and len(subject) > MAX_SUBJECT_LENGTH:
        return {"error": f"The subject is too long (at most {MAX_SUBJECT_LENGTH} characters)."}
    contact = doc.get("contact_data") or {}
    try:
        clean_subject = check_sendable(contact, ghl_channel, subject)
    except MessageInvalid as e:
        return {"error": e.detail}
    if not confirm_code and not await safe_token_for(db, user_id, doc.get("location_id")):
        return dict(_RECONNECT)

    async def preview():
        out = {"to": _lead_name(contact), "channel": channel, "body": message}
        if ghl_channel == "Email":
            out["email"] = _mask_email(contact.get("email"))
            out["subject"] = clean_subject
        else:
            out["phone"] = _mask_phone(contact.get("phone"))
        return out

    async def execute():
        try:
            sent = await send_lead_message_service(db, user_id, doc.get("location_id"), contact_id,
                                                   ghl_channel, message, clean_subject)
        except (MessagingReconnect, MessagingUnavailable):
            return dict(_RECONNECT)
        except MessagingUnconfirmed:
            return {"ok": False, "error": "GoHighLevel did not confirm the message, so it may still have been sent. "
                                          "Check get_lead_messages before sending again."}
        except MessagingFailed:
            return {"ok": False, "error": "GoHighLevel rejected the message. Nothing was sent."}
        return {"ok": True, "message": sent}

    return await two_step(db, user_id, "send_lead_message", args, confirm_code, preview, execute)
