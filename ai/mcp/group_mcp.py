"""
ai/mcp/group_mcp.py
--------------------
The client group tool (get_client_groups), registered onto the shared FastMCP
server (ai/mcp/server.py) served over the Model Context Protocol, mounted into
the main FastAPI app at /mcp (see main.py). This is a real MCP server —
reachable by external MCP clients (Claude Desktop, Claude Code, etc.), not
just Birdy's own orchestrator.

Business logic here is a straight port of ai/tools/group_tools.py — see that
module for the (unchanged, still-registered) fallback path used by the
orchestrator if the MCP path is unavailable.
"""

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.server import mcp, current_user_id as _current_user_id


@mcp.tool
async def get_client_groups() -> dict:
    """List all client groups (business clients) for the user. Returns group IDs, names, and which integrations are linked. Call this first to discover available groups before filtering other queries."""
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    cursor = db["client_groups"].find(
        {"user_id": user_id},
        {
            "id": 1,
            "name": 1,
            "meta_ad_account_id": 1,
            "ghl_location_id": 1,
            "hotprospector_group_id": 1,
            "ad_account_currency": 1,
            "notes": 1,
            "status": 1,
        },
    )
    docs = await cursor.to_list(length=None)
    groups = []
    for doc in docs:
        groups.append({
            "id": doc.get("id"),
            "name": doc.get("name"),
            "meta_ad_account_id": doc.get("meta_ad_account_id"),
            "ghl_location_id": doc.get("ghl_location_id"),
            "hotprospector_group_id": doc.get("hotprospector_group_id"),
            "currency": doc.get("ad_account_currency"),
            "notes": doc.get("notes", ""),
            "status": doc.get("status"),
        })
    return {"client_groups": groups, "total": len(groups)}
