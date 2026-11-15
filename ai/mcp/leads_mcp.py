"""
ai/mcp/leads_mcp.py
-------------------
One lead (a GoHighLevel contact): the full detail page, the live message thread and call transcripts.
A lead is identified by its contact_id and must belong to one of the caller's client groups. user_id always
comes from the token. Messaging problems come back as {"error": ...} dicts, not exceptions.
"""

import re

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services.ghl_messages import (
    MessagingFailed, MessagingReconnect, MessagingUnavailable, RecordingNotFound,
    fetch_call_transcript, fetch_lead_messages,
)
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
