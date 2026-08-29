"""
routers/call_analysis.py
------------------------
HTTP surface for member-scoped AI call analysis (Sales-Hub Members tab).

Two-step, mirroring the chat tools' confirm-first design:

  GET  /api/call-analysis/member/estimate — cost preview (no audio touched):
       how many of the agent's last-N calls have recordings, total minutes,
       how many are already transcribed (free), and ~credits to run.
  POST /api/call-analysis/member/analyze  — one batch (≤ CALL_ANALYSIS_MAX_CALLS)
       of the agent's most recent recorded calls; the frontend chains batches
       with `before` (the previous response's `next_before`) up to the user's
       chosen total (≤ MEMBER_ANALYSIS_MAX_TOTAL).

Heavy lifting lives in services/call_analysis_service.py.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from core.database import get_db
from dependencies import get_current_user, get_mongo_client
from credits_middleware import check_credits
from services import call_analysis_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/call-analysis", tags=["call_analysis"])


@router.get("/member/estimate")
async def estimate_member_analysis(
    agent_name: str = Query(..., min_length=1, description="Member display name (matches call recordings' caller name)"),
    limit: int = Query(10, ge=1, le=call_analysis_service.MEMBER_ANALYSIS_MAX_TOTAL),
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            return await call_analysis_service.estimate_member_analysis(db, current_user, agent_name, limit)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Member analysis estimate failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to estimate analysis cost")


class MemberAnalyzeRequest(BaseModel):
    agent_name: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=call_analysis_service.CALL_ANALYSIS_MAX_CALLS)
    before: str | None = None  # ISO datetime cursor from the previous batch's next_before
    focus: str | None = None


@router.post("/member/analyze")
async def analyze_member_calls(
    request: MemberAnalyzeRequest,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        try:
            db = get_db(mongo_client)
            # Same stopper as chat: block when out of Birdy Credits (402).
            await check_credits(current_user, mongo_client)
            return await call_analysis_service.analyze_member_calls(
                db,
                current_user,
                request.agent_name,
                limit=request.limit,
                before=request.before,
                focus=request.focus,
            )
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.error(f"Member analysis failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to analyze member calls")
