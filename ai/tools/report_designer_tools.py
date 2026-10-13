"""
Tools for the Slack report copilot (chat page "report_designer").

The model composes the whole Slack Block Kit message freely, but never its
numbers: every live figure is a {{placeholder}} or a `birdy_data` binding the
renderer fills from the caches. `propose_report` checks the blocks against
Slack's rules, stores them as the user's draft, and the UI renders that draft
with real figures next to the conversation.
"""

import logging

from ai.tools.registry import registry
from services import report_blocks as rt

logger = logging.getLogger(__name__)


async def _clients(db, user_id):
    return await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        {"_id": 0, "id": 1, "name": 1, "meta_ad_account_id": 1, "ghl_location_id": 1,
         "hotprospector_group_id": 1, "targets": 1},
    ).to_list(length=1000)


async def list_report_clients(db, user_id, **_):
    clients = await _clients(db, user_id)
    rows = []
    for c in clients:
        sources = rt.sources_connected([c])
        rows.append({
            "id": c.get("id"),
            "name": c.get("name"),
            "connected": sorted(rt.SOURCE_LABELS[s] for s in sources),
            "has_targets": bool(c.get("targets")),
        })
    return {"clients": rows, "total": len(rows)}


async def list_report_metrics(db, user_id, **_):
    clients = await _clients(db, user_id)
    return {
        "metrics": rt.catalog_for(rt.sources_connected(clients)),
        "periods": rt.periods_for_ui(),
        "placeholders": rt.PLACEHOLDER_GUIDE,
        "blocks": rt.BLOCKS_GUIDE,
        "daily_metrics": [{"id": k, "label": v["label"]} for k, v in rt.DAILY_METRICS.items()],
        "note": ("Use a metric's id as its placeholder name, e.g. {{cpl}}. Metrics with available=false "
                 "have no connected data source yet; avoid them unless the user asks. "
                 "show=target / show=vs_target only work where has_target=true."),
    }


async def get_report_draft(db, user_id, **_):
    from routers.report_designer import load_draft, saved_brief
    draft = await load_draft(db, user_id)
    saved = await saved_brief(db, user_id)
    return {"draft": draft, "saved": saved,
            "note": "Edit the draft's blocks (or the saved report's if there is no draft) and send the whole thing to propose_report."}


async def propose_report(db, user_id, template=None, **_):
    from routers.report_designer import save_draft
    if not isinstance(template, dict):
        return {"ok": False, "errors": ["template is required"]}
    clients = await _clients(db, user_id)
    try:
        clean = rt.validate(template, {c["id"]: c.get("name") or "" for c in clients if c.get("id")})
    except rt.TemplateError as e:
        return {"ok": False, "errors": e.errors}
    await save_draft(db, user_id, clean)
    return {"ok": True, "draft": clean,
            "note": "Saved. The live preview beside the chat now shows this draft with the user's real numbers."}


def register_report_designer_tools():
    registry.register(
        name="list_report_clients",
        description="List the user's active clients with their ids, which data sources each has connected, and whether targets are set.",
        parameters={"type": "object", "properties": {}},
        executor=list_report_clients,
    )
    registry.register(
        name="list_report_metrics",
        description="List the metrics a Slack report can include, with labels, data source, availability and whether a target comparison exists.",
        parameters={"type": "object", "properties": {}},
        executor=list_report_metrics,
    )
    registry.register(
        name="get_report_draft",
        description="Get the user's current report draft and their saved report, if any.",
        parameters={"type": "object", "properties": {}},
        executor=get_report_draft,
    )
    registry.register(
        name="propose_report",
        description=(
            "Save the user's Slack report as a draft: the schedule plus the WHOLE Block Kit message (blocks). "
            "Any block a channel message allows — header, section with fields, divider, image, context, link "
            "buttons, rich_text, markdown, card, carousel, table, data_table and native charts "
            "(data_visualization: line/bar/area/pie). Live numbers go in {{placeholders}} or a birdy_data "
            "binding on charts/tables/carousels. The UI shows it at once as a live preview with real numbers. "
            "Always send the complete blocks array. Returns errors to fix if Slack's rules are broken."
        ),
        parameters={
            "type": "object",
            "properties": {
                "template": {
                    "type": "object",
                    "properties": {
                        "frequency": {"type": "string", "enum": list(rt.FREQUENCIES)},
                        "day": {"type": "string", "description": "weekly only, e.g. Monday"},
                        "time": {"type": "string", "description": "a whole hour like '9:00 AM'"},
                        "blocks": {"type": "array", "items": {"type": "object"},
                                   "description": "Slack Block Kit blocks (see list_report_metrics → blocks)."},
                    },
                    "required": ["frequency", "time", "blocks"],
                },
            },
            "required": ["template"],
        },
        executor=propose_report,
    )
