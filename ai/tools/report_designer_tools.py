"""
Tools for the Slack report copilot (chat page "report_designer").

The model never writes the report's numbers. It designs a template, and
`propose_report` validates it and stores it as the user's draft; the UI then
renders that draft with real figures next to the conversation.
"""

import logging

from ai.tools.registry import registry
from services import report_template as rt

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
        "note": ("Only offer metrics with available=true unless the user asks; others have no "
                 "connected data source yet. has_target=true means compare='target' can work."),
    }


async def get_report_draft(db, user_id, **_):
    from routers.report_designer import load_draft, saved_brief
    draft = await load_draft(db, user_id)
    saved = await saved_brief(db, user_id)
    return {"draft": draft, "saved": saved,
            "note": "Edit the draft (or the saved report if there is no draft) and send the full template to propose_report."}


async def propose_report(db, user_id, template=None, **_):
    from routers.report_designer import save_draft
    if not isinstance(template, dict):
        return {"ok": False, "errors": ["template is required"]}
    clients = await _clients(db, user_id)
    try:
        clean = rt.validate(template, {c["id"] for c in clients if c.get("id")})
    except rt.TemplateError as e:
        return {"ok": False, "errors": e.errors}
    await save_draft(db, user_id, clean)
    return {"ok": True, "draft": clean,
            "note": "Saved. The live preview beside the chat now shows this draft with the user's real numbers."}


_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(rt.SECTION_TYPES)},
        "metric": {"type": "string", "description": "metric/ranking: a metric id from list_report_metrics"},
        "scope": {
            "description": "metric only: 'portfolio' (all clients) or {\"client_id\": \"...\"}",
            "anyOf": [
                {"type": "string", "enum": ["portfolio"]},
                {"type": "object", "properties": {"client_id": {"type": "string"}}, "required": ["client_id"]},
            ],
        },
        "compare": {"type": "string", "enum": list(rt.COMPARES),
                    "description": "metric only: 'previous' = change vs the period before; 'target' = vs the client's target"},
        "direction": {"type": "string", "enum": list(rt.DIRECTIONS), "description": "ranking only"},
        "limit": {"type": "integer", "minimum": 1, "maximum": rt.MAX_RANKING, "description": "ranking only"},
        "focus": {"type": "string", "description": "birdy_take only: what the take should focus on"},
    },
    "required": ["type"],
}


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
            "Save a complete Slack report template as the user's draft. The UI shows it immediately as a live "
            "preview with real numbers. Always send the FULL template (schedule + every section), not a diff. "
            "Returns errors to fix if the template is invalid."
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
                        "title": {"type": "string", "description": "optional report name, max 60 chars"},
                        "sections": {"type": "array", "maxItems": rt.MAX_SECTIONS, "items": _SECTION_SCHEMA},
                    },
                    "required": ["frequency", "time", "sections"],
                },
            },
            "required": ["template"],
        },
        executor=propose_report,
    )
