"""
ai/mcp/report_designer_mcp.py
-----------------------------
Birdy's Slack report designer as MCP tools: the same draft the owner sees in the app (Hub -> Reports -> designer).
Reads list clients and metrics, show the draft and render a preview; propose_report saves a draft. Wraps
ai/tools/report_designer_tools.py; user_id always comes from the token. Nothing here changes the live report.
"""

from typing import Literal

from ai.mcp.access import READ, WRITE
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import report_designer_tools as tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import report_drafts


def _db():
    return get_db(get_shared_mongo_client())


@mcp.tool(**READ)
async def list_report_clients() -> dict:
    """The owner's active clients as the report designer sees them: each one's id, name, which data sources are connected (Meta, GoHighLevel, HotProspector) and whether targets are set.

    Use it to find a client id before binding a client-specific figure in a report, or to see which clients have data for a metric. Returns {"clients": [...], "total": n}.
    """
    return await tools.list_report_clients(_db(), _current_user_id())


@mcp.tool(**READ)
async def list_report_metrics() -> dict:
    """What a Slack report can contain: the metrics (id, label, data source, whether available and whether a target comparison exists), the periods, the placeholder syntax (e.g. {{cpl}}), a guide to the Slack blocks allowed (header, section, divider, image, context, buttons, table, data_table, native charts and more) and the daily metrics for charts.

    Read this before propose_report so the blocks use valid metric ids and block shapes. Metrics with available=false have no connected data source; avoid them unless the owner asks.
    """
    return await tools.list_report_metrics(_db(), _current_user_id())


@mcp.tool(**READ)
async def get_report_draft() -> dict:
    """The owner's current report draft and their saved (live) report, either of which may be null.

    Use it before changing a report: edit the draft's blocks (or the saved report's if there is no draft) and send the whole thing back with propose_report. Returns {"draft": template or null, "saved": template or null}.
    """
    return await tools.get_report_draft(_db(), _current_user_id())


@mcp.tool(**READ)
async def preview_report(source: Literal["draft", "saved"] = "draft") -> dict:
    """Render a report as the real Slack message with the owner's own numbers (sample figures when they have none yet). Nothing is sent to Slack.

    Use it to check how a draft or the live report will look. Returns {"blocks": [...], "text": str, "sample": bool, "template": the template rendered}.

    Args:
        source: "draft" renders the working draft (falling back to the saved report, then the standard brief); "saved" renders the live report only and returns empty blocks if there is none.
    """
    return await report_drafts.preview_stored(_db(), _current_user_id(), source)


@mcp.tool(**WRITE)
async def propose_report(template: dict) -> dict:
    """Save a Slack report as the owner's DRAFT: the schedule plus the WHOLE Block Kit message. The owner's live report does not change until they save it in Birdy's report designer.

    Use it to design or revise a report. Allowed blocks include header, section with fields, divider, image, context, link buttons, rich_text, markdown, card, carousel, table, data_table and native charts (data_visualization: line/bar/area/pie). Live numbers go in {{placeholders}} or a birdy_data binding, never typed in. Always send the complete blocks array. Returns {"ok": true, "saved_blocks": [...]} on success, or {"ok": false, "errors": [...]} when Slack's rules are broken, in which case nothing is saved; fix the errors and call again. Call list_report_metrics first for valid ids and block shapes.

    Args:
        template: {"frequency": "daily" | "weekly", "day": "Monday" (weekly only), "time": a whole hour like "9:00 AM", "blocks": [Slack blocks]}.
    """
    return await tools.propose_report(_db(), _current_user_id(), template=template)
