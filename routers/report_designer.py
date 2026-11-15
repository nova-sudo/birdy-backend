"""
routers/report_designer.py
--------------------------
Endpoints behind the Slack report copilot (onboarding step + Settings modal).

The conversation itself runs through /api/chat with page="report_designer";
its `propose_report` tool writes the user's working draft to `report_drafts`.
These endpoints let the UI read that draft, seed it, and render any template
as the real Slack message, with the user's own numbers.

    GET  /api/report-designer/state    current brief, draft, metric catalog, clients
    PUT  /api/report-designer/draft    replace the draft (seed from current / reset)
    POST /api/report-designer/preview  render a template (default: the draft, or the saved report)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import report_blocks as rt
from services.report_drafts import (  # noqa: F401  (re-exported: tests and callers patch/import these here)
    DRAFTS, _clients, client_names, load_draft, preview_payload, save_draft, saved_brief, preview_stored,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class DraftBody(BaseModel):
    template: Optional[dict] = None   # None → seed from the saved brief (or the default)


class PreviewBody(BaseModel):
    template: Optional[dict] = None   # None → the stored template named by `source`
    source: str = "draft"             # "draft" (copilot) | "saved" (the live report, Settings)


def _validate(template: dict, names: dict[str, str]) -> dict:
    try:
        return rt.validate(template, names)
    except rt.TemplateError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors})


@router.get("/api/report-designer/state")
async def state(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        clients = await _clients(db, current_user)
        return {
            "brief": await saved_brief(db, current_user),
            "draft": await load_draft(db, current_user),
            "catalog": rt.catalog_for(rt.sources_connected(clients)),
            "periods": rt.periods_for_ui(),
            "daily_metrics": [{"id": k, "label": v["label"]} for k, v in rt.DAILY_METRICS.items()],
            "clients": [{"id": c["id"], "name": c.get("name")} for c in clients],
        }


@router.put("/api/report-designer/draft")
async def put_draft(body: DraftBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        if body.template is None:
            template = await saved_brief(db, current_user) or rt.default_template()
        else:
            template = _validate(body.template, await client_names(db, current_user))
        await save_draft(db, current_user, template)
        return {"draft": template}


@router.post("/api/report-designer/preview")
async def preview(body: PreviewBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        if body.template is None:
            return await preview_stored(db, current_user, body.source)
        template = _validate(body.template, await client_names(db, current_user))
        return await preview_payload(db, current_user, template)
