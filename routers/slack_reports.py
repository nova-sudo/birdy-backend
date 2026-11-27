"""
routers/slack_reports.py
------------------------
The account's Slack reports (services/slack_reports.py), for Hub → Reports.

    GET    /api/slack-reports             every report, the default (the brief) first; ?owner= one agent's
    POST   /api/slack-reports             create one (`owner` picks the agent; Birdy's by default)
    PATCH  /api/slack-reports/{id}        change name, trigger, channel, on/off or content (never the owner)
    DELETE /api/slack-reports/{id}        delete one (not the default — turn it off instead)
    POST   /api/slack-reports/{id}/test   post it now, with sample event details

Content is a template the report copilot designs (routers/report_designer.py):
the UI seeds the copilot's draft with a report's template and saves the result
back here. Editing the default report writes the brief too, so onboarding and
Settings keep showing what will be sent.

Every report belongs to one agent (`owner`: birdy, media_buyer or sales_coach), so each
agent's Reports tab lists its own. The default brief is always Birdy's.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import slack_report_ops as ops
from services import slack_reports as sr
from services.slack_report_ops import ReportBody, Trigger  # noqa: F401  (the request models)

logger = logging.getLogger(__name__)

router = APIRouter()


def _refuse(e: ops.ReportError) -> HTTPException:
    return HTTPException(e.status_code, e.detail)


@router.get("/api/slack-reports")
async def list_reports(owner: Optional[ops.Owner] = Query(None), current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        return await ops.list_reports(mongo_client[DB_NAME], current_user, owner=owner)


@router.post("/api/slack-reports")
async def create_report(body: ReportBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            return await ops.create_report(mongo_client[DB_NAME], current_user, body)
        except ops.ReportError as e:
            raise _refuse(e)


@router.patch("/api/slack-reports/{report_id}")
async def update_report(report_id: str, body: ReportBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            return await ops.update_report(mongo_client[DB_NAME], current_user, report_id, body)
        except ops.ReportError as e:
            raise _refuse(e)


@router.delete("/api/slack-reports/{report_id}")
async def delete_report(report_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        try:
            return await ops.delete_report(mongo_client[DB_NAME], current_user, report_id)
        except ops.ReportError as e:
            raise _refuse(e)


@router.post("/api/slack-reports/{report_id}/test")
async def test_report(report_id: str, current_user: str = Depends(get_current_user)):
    """Post the report now. Event reports get sample event details. Not recorded as a run."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            report = await ops.get_report(db, current_user, report_id)
        except ops.ReportError as e:
            raise _refuse(e)
        return await ops.send_test(db, report)
