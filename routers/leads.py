"""
routers/leads.py
----------------
Read endpoints for the Lead Detail page.

GET /api/leads/contact/{contact_id}
    200 with the assembled lead detail (services/lead_detail.py), or
    404 {"detail": "Lead not found"} when the contact does not exist for
    the signed-in user.

GET  /api/leads/contact/{contact_id}/messages
    Live GHL messages for the lead (services/ghl_messages.py).
GET  /api/leads/contact/{contact_id}/journey
    Newest-first timeline of the lead (services/lead_journey.py); 404 unknown lead.
GET  /api/leads/contact/{contact_id}/summary
    Cached AI summary (generated on first view); POST regenerates it.
    200 {status: ok|blocked|unavailable|failed|generating, summary, generated_at, stale}; 404 unknown lead.
POST /api/leads/contact/{contact_id}/messages
    Send an SMS or email to the lead. 201 {"message": ...}; 404 unknown lead;
    422 missing subject/phone/email; 409 reconnect GHL; 502 GHL failure.
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user
from services.ghl_messages import (
    MessagingFailed, MessagingReconnect, MessagingUnavailable, MessagingUnconfirmed, RecordingNotFound,
    fetch_call_recording, fetch_call_transcript, fetch_lead_messages, send_lead_message,
)
from services.lead_detail import build_lead_detail
from services.lead_journey import build_lead_journey
from services.lead_summary import get_lead_summary

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("/contact/{contact_id}")
async def get_lead_detail(contact_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        detail = await build_lead_detail(db, current_user, contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return detail


@router.get("/contact/{contact_id}/journey")
async def get_lead_journey(contact_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        journey = await build_lead_journey(db, current_user, contact_id)
    if journey is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return journey


async def _summary(contact_id: str, user_id: str, refresh: bool):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        result = await get_lead_summary(db, user_id, contact_id, refresh=refresh)
    if result is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return result


@router.get("/contact/{contact_id}/summary")
async def get_lead_summary_endpoint(contact_id: str, current_user: str = Depends(get_current_user)):
    return await _summary(contact_id, current_user, refresh=False)


@router.post("/contact/{contact_id}/summary")
async def refresh_lead_summary_endpoint(contact_id: str, current_user: str = Depends(get_current_user)):
    return await _summary(contact_id, current_user, refresh=True)


class SendMessageBody(BaseModel):
    channel: Literal["SMS", "Email"]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]
    subject: Optional[str] = Field(default=None, max_length=300)


async def _find_contact(db, user_id: str, contact_id: str) -> dict:
    doc = await db["ghl_contacts"].find_one({"user_id": user_id, "contact_data.id": contact_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Lead not found")
    return doc


@router.get("/contact/{contact_id}/messages")
async def get_lead_messages(contact_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        doc = await _find_contact(db, current_user, contact_id)
        return await fetch_lead_messages(db, current_user, doc.get("location_id"), contact_id)


@router.post("/contact/{contact_id}/messages", status_code=201)
async def send_lead_message_endpoint(contact_id: str, body: SendMessageBody,
                                     current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        doc = await _find_contact(db, current_user, contact_id)

        contact = doc.get("contact_data") or {}
        subject = (body.subject or "").strip() or None
        if body.channel == "Email":
            if not subject:
                raise HTTPException(status_code=422, detail="Subject is required for email")
            if not (contact.get("email") or "").strip():
                raise HTTPException(status_code=422, detail="This lead has no email address")
        elif not (contact.get("phone") or "").strip():
            raise HTTPException(status_code=422, detail="This lead has no phone number")

        try:
            message = await send_lead_message(
                db, current_user, doc.get("location_id"), contact_id,
                body.channel, body.message, subject)
        except (MessagingReconnect, MessagingUnavailable):
            raise HTTPException(status_code=409, detail="Reconnect GoHighLevel to send messages")
        except MessagingUnconfirmed:
            raise HTTPException(
                status_code=504,
                detail="We couldn't confirm the message was sent. Check the thread before sending again.")
        except MessagingFailed:
            raise HTTPException(status_code=502, detail="GoHighLevel rejected the message.")
    return {"message": message}


# GHL message ids are short alphanumeric strings; anything else never reaches GHL.
MessageId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


async def _call_media(contact_id: str, message_id: str, user_id: str, fetch):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        doc = await _find_contact(db, user_id, contact_id)
        try:
            return await fetch(db, user_id, doc.get("location_id"), contact_id, message_id)
        except RecordingNotFound:
            raise HTTPException(status_code=404, detail="Recording not found")
        except (MessagingReconnect, MessagingUnavailable):
            raise HTTPException(status_code=409, detail="Reconnect GoHighLevel to play call recordings")
        except MessagingFailed:
            raise HTTPException(status_code=502, detail="GoHighLevel couldn't return the recording.")


@router.get("/contact/{contact_id}/calls/{message_id}/recording")
async def get_call_recording(contact_id: str, message_id: MessageId,
                             current_user: str = Depends(get_current_user)):
    audio, content_type = await _call_media(contact_id, message_id, current_user, fetch_call_recording)
    return Response(content=audio, media_type=content_type,
                    headers={"Cache-Control": "private, max-age=3600"})


@router.get("/contact/{contact_id}/calls/{message_id}/transcript")
async def get_call_transcript(contact_id: str, message_id: MessageId,
                              current_user: str = Depends(get_current_user)):
    return {"transcript": await _call_media(contact_id, message_id, current_user, fetch_call_transcript)}
