"""
ai/mcp/group_mcp.py
--------------------
The client group tool (get_client_groups), registered onto the shared FastMCP
server (ai/mcp/server.py) served over the Model Context Protocol, mounted into
the main FastAPI app at /mcp (see main.py). This is a real MCP server —
reachable by external MCP clients (Claude Desktop, Claude Code, etc.), not
just Birdy's own orchestrator.

The logic lives in ai/tools/group_tools.py, shared with the orchestrator's
fallback path.
"""

from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from ai.mcp.access import READ
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import group_tools


@mcp.tool(**READ)
async def get_client_groups() -> dict:
    """List all client groups (business clients) for the user. Returns group IDs, names, and which integrations are linked. Call this first to discover available groups before filtering other queries."""
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())

    return await group_tools.get_client_groups(db, user_id)
