"""
ai/mcp/meta_mcp.py
--------------------
The Meta (Facebook Ads) insights tools (get_campaign_insights, get_adset_insights,
get_ad_insights, get_facebook_leads), registered onto the shared FastMCP server
(ai/mcp/server.py) served over the Model Context Protocol, mounted into the main
FastAPI app at /mcp (see main.py). This is a real MCP server — reachable by
external MCP clients (Claude Desktop, Claude Code, etc.), not just Birdy's own
orchestrator.

Business logic here is a straight port of ai/tools/meta_tools.py — see that
module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.
"""

from ai.tools.derived_metrics import enrich
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.access import READ
from ai.mcp.cards import card
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import meta_tools
from ai.tools.meta_tools import _legacy_insights, shape_cached_insights
from services.facebook_cache_shape import read_preset


# ---------------------------------------------------------------------------
# Helpers: read from facebook_cache on client_groups (where the data lives)
# ---------------------------------------------------------------------------

def _pick_preset(start_date, end_date):
    """Map a date range to the best matching facebook_cache preset key."""
    from datetime import date as _date, timedelta

    if not start_date and not end_date:
        return "maximum"

    today = _date.today()
    try:
        s = _date.fromisoformat(start_date) if start_date else None
        e = _date.fromisoformat(end_date) if end_date else today
    except ValueError:
        return "maximum"

    if not s:
        return "maximum"

    delta = (e - s).days

    # Exact matches
    if s == e == today:
        return "today"
    if s == e == today - timedelta(days=1):
        return "yesterday"
    if delta <= 7:
        return "last_7d"
    if delta <= 14:
        return "last_14d"
    if delta <= 30:
        return "last_30d"
    if delta <= 90:
        return "this_quarter"
    if delta <= 365:
        return "this_year"
    return "maximum"


async def _get_cached_data(db, user_id, level, group_ids=None, start_date=None, end_date=None):
    """
    Read campaign/adset/ad data from facebook_cache on client_groups.
    This is where the actual data lives (populated by the preset refresh).
    Falls back to the per-day insights collections if cache has no data.
    """
    preset = _pick_preset(start_date, end_date)

    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            # Split shape: identity once, plus this preset's metrics.
            "facebook_cache.entities": 1,
            f"facebook_cache.presets.{preset}": 1,
            # Legacy bucket, for groups not yet refreshed since the split.
            f"facebook_cache.{preset}": 1,
        },
    ).to_list(None)

    results = []
    for g in groups:
        cache = g.get("facebook_cache", {})
        # read_preset prefers the split shape and falls back to the legacy
        # bucket, so this works either side of the migration. The flat
        # top-level campaigns/adsets/ads are deliberately no longer consulted:
        # they are the ALL-TIME copies, so falling back to them served
        # lifetime rows under whatever window was asked for.
        items = read_preset(cache, preset).get(level, [])

        for item in items:
            item["client_group_id"] = g.get("id")
            item["client_group_name"] = g.get("name")
            enrich(item)
            results.append(item)

    return results


# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

@mcp.tool(**READ)
async def get_campaign_insights(
    start_date: str | None = None,
    end_date: str | None = None,
    group_ids: list[str] | None = None,
) -> dict:
    """Get Facebook campaign-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, results/leads). Returns data from the cached preset that best matches the date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        group_ids: List of client group IDs to filter by. Omit to query all groups.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    # Try cache first (where the data actually is)
    cached = await _get_cached_data(db, user_id, "campaigns", group_ids, start_date, end_date)
    if cached:
        return shape_cached_insights(cached)

    # Fallback to the per-day insights collection (never for scoped groups).
    return await _legacy_insights(db, "facebook_campaign_insights", user_id, start_date, end_date, group_ids)


@mcp.tool(**READ)
async def get_adset_insights(
    start_date: str | None = None,
    end_date: str | None = None,
    group_ids: list[str] | None = None,
) -> dict:
    """Get Facebook ad set-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM). Returns data from the cached preset that best matches the date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        group_ids: List of client group IDs to filter by. Omit to query all groups.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    cached = await _get_cached_data(db, user_id, "adsets", group_ids, start_date, end_date)
    if cached:
        return shape_cached_insights(cached)

    # Fallback to the per-day insights collection (never for scoped groups).
    return await _legacy_insights(db, "facebook_adset_insights", user_id, start_date, end_date, group_ids)


@mcp.tool(**READ, meta=card("ads"))
async def get_ad_insights(
    start_date: str | None = None,
    end_date: str | None = None,
    group_ids: list[str] | None = None,
) -> dict:
    """Get Facebook individual ad-level performance metrics (spend, impressions, clicks, CTR, CPC, CPM, quality rankings). Returns data from the cached preset that best matches the date range.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        group_ids: List of client group IDs to filter by. Omit to query all groups.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    cached = await _get_cached_data(db, user_id, "ads", group_ids, start_date, end_date)
    if cached:
        shaped = shape_cached_insights(cached)
        if shaped.get("client_group"):  # one client: its money is in one currency (for the ad leaderboard card)
            g = await db["client_groups"].find_one({"id": shaped["client_group"]["id"], "user_id": user_id},
                                                   {"ad_account_currency": 1, "facebook_cache.currency": 1, "_id": 0}) or {}
            shaped["currency"] = (g.get("facebook_cache") or {}).get("currency") or g.get("ad_account_currency")
        return shaped

    # Fallback to the per-day insights collection (never for scoped groups).
    return await _legacy_insights(db, "facebook_ad_insights", user_id, start_date, end_date, group_ids)


@mcp.tool(**READ)
async def get_facebook_leads(
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 100,
    group_ids: list[str] | None = None,
) -> dict:
    """Get Facebook leads with contact info (name, email, phone) and which ad/campaign generated them. Sorted by newest first.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        limit: Max leads to return (default 100, max 200)
        group_ids: List of client group IDs to filter by. Omit to query all groups.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    return await meta_tools.get_facebook_leads(
        db, user_id, start_date=start_date, end_date=end_date, limit=limit, group_ids=group_ids)
