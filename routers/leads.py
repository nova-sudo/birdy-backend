"""
routers/leads.py
----------------
Read endpoints for the Lead Detail page.

GET /api/leads/contact/{contact_id}
    200 with the assembled lead detail (services/lead_detail.py), or
    404 {"detail": "Lead not found"} when the contact does not exist for
    the signed-in user.
"""

from fastapi import APIRouter, Depends, HTTPException

from core.database import DB_NAME
from dependencies import get_mongo_client, get_current_user
from services.lead_detail import build_lead_detail

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("/contact/{contact_id}")
async def get_lead_detail(contact_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        detail = await build_lead_detail(db, current_user, contact_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    return detail
