import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response
from pymongo.errors import DuplicateKeyError

from core.database import DB_NAME
from core.models import WaitlistRequest
from dependencies import get_mongo_client

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/waitlist")
async def join_waitlist(request: WaitlistRequest, response: Response):
    """Public, unauthenticated endpoint — the landing page's waitlist form.
    Relies on the unique index on waitlist.email (see
    utils/cache_helpers.py::create_performance_indexes) to reject duplicates."""
    email = request.email.strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email is required")

    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            await db["waitlist"].insert_one({
                "email": email,
                "submitted_at": datetime.now(),
            })
            logger.info(f"Waitlist signup: {email}")
            return {"message": "Joined waitlist", "email": email}
        except DuplicateKeyError:
            response.status_code = 409
            return {"message": "Already on waitlist", "email": email}
        except Exception as e:
            logger.error(f"Error saving waitlist signup for {email}: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to join waitlist: {str(e)}")
