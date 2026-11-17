"""
services/report_drafts.py
-------------------------
The report designer's draft storage and preview, shared by the dashboard routes
(routers/report_designer.py), the chat copilot tools and the MCP tools, so all three see
the same draft and render the same message.
"""

from datetime import datetime, timezone
from typing import Optional

from services import report_blocks as rt

DRAFTS = "report_drafts"


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


async def stored_template(db, user_id: str, source: str) -> Optional[dict]:
    """The template a preview shows when none is sent: source "draft" is the draft, else the saved report,
    else the default; source "saved" is the saved report only (None when there is none)."""
    draft = await load_draft(db, user_id) if source == "draft" else None
    template = draft or await saved_brief(db, user_id)
    if not template:
        if source == "saved":
            return None
        template = rt.default_template()
    return template


async def preview_stored(db, user_id: str, source: str) -> dict:
    """The preview for source "draft" or "saved" when no template is sent (the empty payload if there is nothing)."""
    template = await stored_template(db, user_id, source)
    if template is None:
        return {"blocks": [], "text": "", "sample": False, "template": None}
    return await preview_payload(db, user_id, template)
