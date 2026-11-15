"""
services/attribution_reports.py
-------------------------------
The attribution read views the HTTP routes (routers/attribution.py) and the MCP tools
(ai/mcp/attribution_mcp.py) both serve. One implementation, so the two can't drift.

Callers pass a client group they already know belongs to the user. A bad date raises
AttributionInputError, which the router turns into a 400 and the MCP tool into a ToolError.
"""

from datetime import datetime

from core.database import DB_NAME
from services import landing_funnel as landing_funnel_service
from services import lead_collection as lead_collection_service
from services.attribution_service import install_status, leads_by_ad
from services.tracked_leads import TRACKED_LEADS


class AttributionInputError(ValueError):
    """A caller-supplied date or window the views can't use."""


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise AttributionInputError(f"Invalid date: {value}")


def parse_day(value: str | None, field: str) -> str | None:
    """An ISO date as the day key the funnel rollup stores."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        raise AttributionInputError(f"Invalid {field} date: {value}")


async def leads_by_ad_report(
    user_id: str, group_id: str, start: str | None, end: str | None, mongo_client,
    include_predated: bool = False,
) -> dict:
    """Attributed leads per Meta ad for one client group, counted by contact creation date."""
    start_at, end_at = parse_timestamp(start), parse_timestamp(end)
    rows = await leads_by_ad(
        user_id, group_id, start_at, end_at, mongo_client, include_predated=include_predated,
    )
    return {
        "group_id": group_id,
        "start": start,
        "end": end,
        "ads": rows,
        "attributed_leads": sum(r["leads"] for r in rows),
    }


async def funnel_report(group: dict, start: str, end: str, mongo_client) -> dict:
    """Landing views -> form starts -> opt-ins for one client group, or applicable=False with the reason."""
    group_id = group["id"]
    start_day = parse_day(start, "start")
    end_day = parse_day(end, "end")
    if start_day > end_day:
        raise AttributionInputError("start must not be after end")

    config = lead_collection_service.read(group)
    method = config["method"]

    applicable = method in lead_collection_service.NEEDS_SCRIPT
    if method == lead_collection_service.METHOD_UNKNOWN:
        applicable = await landing_funnel_service.has_any_data(group_id, mongo_client)

    if not applicable:
        return {
            "group_id": group_id,
            "group_name": group.get("name"),
            "method": method,
            "applicable": False,
            "reason": (
                "This client's leads come from Meta Instant Forms, which never "
                "send anyone to a page of their own."
                if method == lead_collection_service.METHOD_INSTANT_FORM else
                "This client's form tool sends leads to GoHighLevel, so its "
                "pages carry no Birdy script to measure. Leads are attributed "
                "from GoHighLevel."
                if method == lead_collection_service.METHOD_FORM_TO_GHL else
                "No landing page activity has reached Birdy for this client yet."
            ),
        }

    funnel = await landing_funnel_service.funnel(group_id, start_day, end_day, mongo_client)
    status = await install_status(group_id, mongo_client)
    return {
        "group_id": group_id,
        "group_name": group.get("name"),
        "method": method,
        "applicable": True,
        "installed": status["installed"],
        "last_seen_at": status["last_seen_at"],
        **funnel,
    }


async def overview_report(user_id: str, mongo_client) -> dict:
    """One tracking row per client group of this user; unconfigured clients first."""
    db = mongo_client[DB_NAME]
    groups = await db["client_groups"].find(
        {"user_id": user_id},
        projection={
            "id": 1, "name": 1, "client_status": 1, "_id": 0,
            "attribution_site_id": 1, "lead_collection": 1,
        },
    ).to_list(None)

    rows = []
    for group in groups:
        group_id = group["id"]
        config = lead_collection_service.read(group)
        status = await install_status(group_id, mongo_client)
        leads = await db[TRACKED_LEADS].count_documents({"client_group_id": group_id})
        rows.append({
            "group_id": group_id,
            "group_name": group.get("name"),
            "client_status": group.get("client_status"),
            "method": config["method"],
            "form_provider": config["form_provider"],
            "site_id": group.get("attribution_site_id"),
            "installed": status["installed"],
            "last_seen_at": status["last_seen_at"],
            "ad_click_seen": status["first_ad_click_seen"],
            "tracked_leads": leads,
            "attributed_leads": status["attributed_leads"],
        })

    rows.sort(key=lambda r: (
        r["method"] != lead_collection_service.METHOD_UNKNOWN,
        (r["group_name"] or "").lower(),
    ))
    return {"clients": rows}
