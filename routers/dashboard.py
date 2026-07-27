"""
routers/dashboard.py
--------------------
Homepage "Do it for me" suggestion API. Implements exactly the contract the
frontend is wired to (src/app/dashboard/useDashboardData.js). All endpoints run
as the authenticated user via get_current_user; the heavy lifting (Meta writes +
reversal bookkeeping) lives in services/dashboard_service.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import dashboard_service

logger = logging.getLogger(__name__)

router = APIRouter()


class AlertActionBody(BaseModel):
    action: str


@router.get("/api/dashboard/summary")
async def dashboard_summary(user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        return await dashboard_service.get_summary(client[DB_NAME], user)


@router.post("/api/dashboard/suggestions/{suggestion_id}/apply")
async def apply_suggestion(suggestion_id: str, user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        try:
            return await dashboard_service.apply_suggestion(client[DB_NAME], user, suggestion_id)
        except dashboard_service.SuggestionNotFound:
            raise HTTPException(status_code=404, detail="Suggestion not found")
        except dashboard_service.MetaNotConnected:
            raise HTTPException(status_code=400, detail="Meta account not connected")


@router.post("/api/dashboard/suggestions/{suggestion_id}/undo")
async def undo_suggestion(suggestion_id: str, user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        try:
            return await dashboard_service.undo_suggestion(client[DB_NAME], user, suggestion_id)
        except dashboard_service.MetaNotConnected:
            raise HTTPException(status_code=400, detail="Meta account not connected")


@router.delete("/api/dashboard/suggestions/{suggestion_id}")
async def dismiss_suggestion(suggestion_id: str, user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        return await dashboard_service.dismiss_suggestion(client[DB_NAME], user, suggestion_id)


@router.post("/api/dashboard/alerts/{alert_id}/action")
async def alert_action(alert_id: str, body: AlertActionBody, user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        return await dashboard_service.run_alert_action(client[DB_NAME], user, alert_id, body.action)


@router.post("/api/dashboard/wins/{win_id}/complete")
async def complete_win(win_id: str, user: str = Depends(get_current_user)):
    async with get_mongo_client() as client:
        return await dashboard_service.complete_win(client[DB_NAME], user, win_id)
