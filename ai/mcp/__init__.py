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
from ai.mcp import agent_mcp  # noqa: F401
from ai.mcp import stock_agent_mcp  # noqa: F401
from ai.mcp import custom_agent_mcp  # noqa: F401
from ai.mcp import suggestions_mcp  # noqa: F401
from ai.mcp import multi_window_mcp  # noqa: F401
from ai.mcp import hp_mcp  # noqa: F401
from ai.mcp import call_analysis_mcp  # noqa: F401
from ai.mcp import creatives_mcp  # noqa: F401
from ai.mcp import attribution_mcp  # noqa: F401
from ai.mcp import client_mcp  # noqa: F401
from ai.mcp import leads_mcp  # noqa: F401
from ai.mcp import slack_reports_mcp  # noqa: F401
from ai.mcp import report_designer_mcp  # noqa: F401
from ai.mcp import refresh_mcp  # noqa: F401
from ai.mcp import watch_mcp  # noqa: F401
from ai.mcp import prompts_mcp  # noqa: F401
from ai.mcp import hello_mcp  # noqa: F401
from ai.mcp import pacing_mcp  # noqa: F401
from ai.mcp import cards  # noqa: F401  (card resources; tools attach them via cards.card())

from ai.mcp.voice import apply_titles  # noqa: E402

apply_titles(mcp)  # after every tool module has registered
cards.attach_live_cards(mcp)
