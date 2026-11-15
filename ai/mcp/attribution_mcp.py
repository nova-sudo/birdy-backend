"""
ai/mcp/attribution_mcp.py
--------------------------
Which ads produce the closes. Wraps ai/tools/attribution_tools.py; user_id always comes from the token.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ
from ai.mcp.client_mcp import resolve_client
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import attribution_tools
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import attribution_reports, lead_collection as lead_collection_service
from services.attribution_reports import AttributionInputError
from services.attribution_service import install_status


@mcp.tool(**READ)
async def get_closes_by_ad(level: Literal["ad", "adset", "campaign"] = "ad", start_date: str | None = None,
                           end_date: str | None = None, group_ids: list[str] | None = None, limit: int = 15) -> dict:
    """Which campaigns, ad sets or ads produce the closes (won GoHighLevel opportunities), with revenue, leads, close rate, spend and cost per close, best first.

    Use it for "which ad is generating the closes or sales", "best ad by revenue" and "cost per close by ad". It
    counts leads that arrived in the window, by the ad that brought them in, and their opportunity's current status.

    Args:
        level: ad (default), adset or campaign.
        start_date: Start of the lead-arrival window, YYYY-MM-DD. Defaults to 30 days before end_date.
        end_date: End of the window, YYYY-MM-DD. Defaults to today.
        group_ids: Optional client group ids (from get_client_groups). Omit for all clients.
        limit: Rows to return (1-15, default 15).
    """
    return await attribution_tools.get_closes_by_ad(
        get_db(get_shared_mongo_client()), _current_user_id(), mongo_client=get_shared_mongo_client(),
        level=level, start_date=start_date, end_date=end_date, group_ids=group_ids, limit=limit)


_DEFAULT_FUNNEL_DAYS = 30


async def _group(db, user_id: str, client: str) -> dict:
    return await resolve_client(db, user_id, client)


def _bad_input(e: AttributionInputError) -> ToolError:
    return ToolError(f"{e}. Use YYYY-MM-DD." if str(e).startswith("Invalid") else str(e))


@mcp.tool(**READ)
async def attribution_status(client: str) -> dict:
    """Whether one client's lead tracking is set up and working: is the Birdy tracking script live, has an ad click been seen, how many leads are attributed to an ad so far, plus the setup checklist (which stages are done and the next step to take).

    Use it for "is tracking working for Acme?", "why aren't Acme's leads attributed to ads?" or before trusting
    leads_by_ad and funnel numbers. Returns {group_id, group_name, status: {installed, last_seen_at,
    first_ad_click_seen, last_ad_id, attributed_leads}, diagnostics: {method, applicable, complete, stages, next_step}}.
    A name that matches several clients returns the candidates and nothing else.

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
    """
    db, user_id = get_db(get_shared_mongo_client()), _current_user_id()
    group = await _group(db, user_id, client)
    mongo_client = get_shared_mongo_client()
    return {
        "group_id": group["id"],
        "group_name": group.get("name"),
        "status": await install_status(group["id"], mongo_client),
        "diagnostics": await lead_collection_service.diagnose(group, mongo_client),
    }


@mcp.tool(**READ)
async def leads_by_ad(client: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    """Leads attributed to each Meta ad for one client, by the ad that brought the person in (via Birdy's tracking script), most leads first. Returns {group_id, start, end, ads: [{ad_id, ad_name, adset_id, campaign_id, leads, deterministic_leads, last_lead_at}], attributed_leads}.

    Use it for "which ads are bringing in Acme's leads" (lead volume only). For which ads produce closes, revenue
    and cost per close, use get_closes_by_ad instead. Leads are counted by the date the contact was created.

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
        start_date: First day of the window, YYYY-MM-DD, inclusive. Omit for no lower bound.
        end_date: Last day of the window, YYYY-MM-DD, inclusive (leads created that day count). Omit for no upper bound.
    """
    mongo_client, user_id = get_shared_mongo_client(), _current_user_id()
    group = await _group(get_db(mongo_client), user_id, client)
    try:
        return await attribution_reports.leads_by_ad_report(user_id, group["id"], start_date, end_date, mongo_client,
                                                            inclusive_end_day=True)
    except AttributionInputError as e:
        raise _bad_input(e)


@mcp.tool(**READ)
async def funnel(client: str, start_date: str | None = None, end_date: str | None = None) -> dict:
    """One client's landing-page funnel: views, form starts and opt-ins with the drop-off between stages, the previous period for comparison, and per-day rows. Returns {applicable, ...}; applicable is false, with a reason, for clients whose leads come from Meta Instant Forms or a form tool that sends straight to GoHighLevel (they have no landing page of ours to measure).

    Use it for "how is Acme's landing page converting" or "where do visitors drop off". It is not about
    which ads produce closes (use get_closes_by_ad) or which ads bring leads (use leads_by_ad).

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
        start_date: Start of the window, YYYY-MM-DD. Defaults to 30 days before end_date.
        end_date: End of the window, YYYY-MM-DD. Defaults to today.
    """
    mongo_client, user_id = get_shared_mongo_client(), _current_user_id()
    group = await _group(get_db(mongo_client), user_id, client)
    try:
        end = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = start_date
        if not start:
            end_day = attribution_reports.parse_day(end, "end")
            start = (datetime.strptime(end_day, "%Y-%m-%d") - timedelta(days=_DEFAULT_FUNNEL_DAYS)).strftime("%Y-%m-%d")
        return await attribution_reports.funnel_report(group, start, end, mongo_client)
    except AttributionInputError as e:
        raise _bad_input(e)


@mcp.tool(**READ)
async def attribution_overview() -> dict:
    """One tracking row per client across all of the user's clients: tracking method, whether the script is installed and last seen, whether an ad click was seen, tracked leads and ad-attributed leads. Clients that haven't set up tracking come first. Returns {clients: [...]}.

    Use it for "which clients have tracking working and which need chasing". For one client's detail and next
    setup step use attribution_status.
    """
    return await attribution_reports.overview_report(_current_user_id(), get_shared_mongo_client())
