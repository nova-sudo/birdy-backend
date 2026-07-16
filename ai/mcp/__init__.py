"""
ai/mcp/__init__.py
-------------------
Importing this package registers every tool module onto the shared FastMCP
server (ai/mcp/server.py) and re-exports the built ASGI app. main.py and
ai/mcp_client.py both do `from ai.mcp import mcp_app` so there is exactly one
FastMCP server/app instance for the whole process.
"""

from ai.mcp.server import mcp, mcp_app  # noqa: F401

from ai.mcp import alert_mcp  # noqa: F401
from ai.mcp import meta_mcp  # noqa: F401
from ai.mcp import ghl_mcp  # noqa: F401
from ai.mcp import group_mcp  # noqa: F401
from ai.mcp import summary_mcp  # noqa: F401
from ai.mcp import compare_mcp  # noqa: F401
from ai.mcp import meta_live_mcp  # noqa: F401
from ai.mcp import custom_metrics_mcp  # noqa: F401
from ai.mcp import unified_leads_mcp  # noqa: F401
