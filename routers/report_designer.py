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
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from services import report_blocks as rt

logger = logging.getLogger(__name__)

router = APIRouter()

DRAFTS = "report_drafts"


class DraftBody(BaseModel):
    template: Optional[dict] = None   # None → seed from the saved brief (or the default)


class PreviewBody(BaseModel):
    template: Optional[dict] = None   # None → the stored template named by `source`
    source: str = "draft"             # "draft" (copilot) | "saved" (the live report, Settings)


async def _clients(db, user_id: str) -> list[dict]:
    return await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        {"_id": 0, "id": 1, "name": 1, "meta_ad_account_id": 1, "ghl_location_id": 1,
         "hotprospector_group_id": 1},
    ).to_list(length=1000)


async def client_names(db, user_id: str) -> dict[str, str]:
    return {c["id"]: c.get("name") or "" for c in await _clients(db, user_id) if c.get("id")}


async def load_draft(db, user_id: str) -> Optional[dict]:
    doc = await db[DRAFTS].find_one({"user_id": user_id}, {"_id": 0, "template": 1})
    template = rt.normalize((doc or {}).get("template"))
    return template if rt.has_content(template) else None


async def save_draft(db, user_id: str, template: dict) -> None:
    await db[DRAFTS].update_one(
        {"user_id": user_id},
        {"$set": {"template": template, "updated_at": datetime.utcnow()}},
        upsert=True,
    )


async def saved_brief(db, user_id: str) -> Optional[dict]:
    user = await db["users"].find_one({"user_id": user_id}, {"integrations.slack_bot.brief": 1}) or {}
    brief = ((user.get("integrations") or {}).get("slack_bot") or {}).get("brief")
    template = rt.normalize(brief)
    if template:
        template.pop("updated_at", None)
    return template if rt.has_content(template) else None


async def preview_payload(db, user_id: str, template: dict) -> dict:
    """The rendered message, with real figures when there are any to show."""
    ctx = await rt.build_context(db, user_id, template, datetime.now(timezone.utc),
                                  allow_sample=True, preview=True)
    take = rt.has_take(template)
    placeholder = None
    if take:
        focus = take["opts"].get("focus")
        placeholder = ("Birdy writes two or three sentences here each time the report goes out"
                       + (f", focused on {focus}." if focus else "."))
    blocks, text = rt.render(template, ctx, take=placeholder)
    return {"blocks": blocks, "text": text, "sample": ctx.sample, "template": template}


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
            draft = await load_draft(db, current_user) if body.source == "draft" else None
            template = draft or await saved_brief(db, current_user)
            if not template:
                if body.source == "saved":
                    return {"blocks": [], "text": "", "sample": False, "template": None}
                template = rt.default_template()
        else:
            template = _validate(body.template, await client_names(db, current_user))
        return await preview_payload(db, current_user, template)
