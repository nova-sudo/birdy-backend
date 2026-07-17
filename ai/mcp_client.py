"""
ai/mcp_client.py
-----------------
Lets ai/orchestrator.py call the MCP-hosted tools (ai/mcp/*.py) the same way
an external MCP client would — including going through the same JWTVerifier
auth check — rather than reaching into the tool functions directly.

Internal calls are authenticated by minting a short-lived JWT for the
already-authenticated user_id (same HS256 secret/shape as the `auth_token`
cookie) and presenting it as a bearer token over an ASGI-backed HTTP
transport: httpx.ASGITransport drives the request straight into the mounted
FastMCP Starlette app in-process (no real socket), but still runs through its
actual auth middleware. fastmcp's pure in-memory transport (FastMCPTransport)
was deliberately NOT used here — it bypasses HTTP/auth entirely, which would
mean internal calls skip the same verifier external callers go through.
"""

import contextlib
import logging
import time

import httpx
import jwt as pyjwt
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from core.config import JWT_SECRET, JWT_ALGORITHM
from ai.mcp import mcp_app
from ai.tools.result_shaping import serialize_with_truncation, error_payload

logger = logging.getLogger(__name__)

# Every tool name registered on the shared FastMCP server (ai/mcp/server.py).
# Add a module's tool names here as each one gets migrated.
MCP_TOOL_NAMES = {
    # ai/mcp/alert_mcp.py
    "get_alerts", "create_alert", "update_alert",
    # ai/mcp/meta_mcp.py
    "get_campaign_insights", "get_adset_insights", "get_ad_insights", "get_facebook_leads",
    # ai/mcp/ghl_mcp.py
    "get_ghl_contacts", "get_ghl_opportunity_stats", "get_ghl_opp_stats_windowed",
    "get_ghl_opp_stats_monthly", "get_ghl_tag_breakdown", "get_tag_rollup_by_campaign",
    # ai/mcp/group_mcp.py
    "get_client_groups",
    # ai/mcp/summary_mcp.py
    "get_account_summary",
    # ai/mcp/compare_mcp.py
    "compare_periods",
    # ai/mcp/meta_live_mcp.py
    "get_meta_insights_live", "get_meta_leads_live", "get_meta_insights_monthly",
    # ai/mcp/custom_metrics_mcp.py
    "list_custom_metrics", "compute_custom_metric", "list_available_metric_fields",
    "create_custom_metric", "update_custom_metric", "delete_custom_metric",
    # ai/mcp/unified_leads_mcp.py
    "get_unified_leads", "get_unified_lead_stats",
}

_INTERNAL_TOKEN_TTL_SECONDS = 60
# No "/mcp" prefix: the ASGITransport below targets mcp_app directly (the raw
# FastMCP sub-app, built with http_app(path="/")), not the outer FastAPI
# app's "/mcp" mount — so its own root "/" is the correct path.
_INTERNAL_BASE_URL = "http://internal.local"


def _mint_internal_token(user_id: str) -> str:
    # time.time() is timezone-safe; datetime.utcnow().timestamp() is NOT — it
    # treats the naive UTC value as local time, silently shifting exp by the
    # host's UTC offset (harmless on a UTC-run server, wrong everywhere else).
    exp = int(time.time()) + _INTERNAL_TOKEN_TTL_SECONDS
    return pyjwt.encode({"sub": user_id, "exp": exp, "type": "access"}, JWT_SECRET, algorithm=JWT_ALGORITHM)


def needs_mcp(allowed: list | None) -> bool:
    """Whether this page's tool allowlist could include an MCP-hosted tool."""
    return allowed is None or any(name in allowed for name in MCP_TOOL_NAMES)


@contextlib.asynccontextmanager
async def mcp_client_for(user_id: str):
    """Async context manager yielding a fastmcp.Client authenticated as user_id."""
    token = _mint_internal_token(user_id)

    def httpx_client_factory(headers=None, timeout=None, auth=None, **kwargs):
        merged_headers = {**(headers or {}), "Authorization": f"Bearer {token}"}
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mcp_app),
            base_url=_INTERNAL_BASE_URL,
            headers=merged_headers,
            timeout=timeout,
            **kwargs,
        )

    transport = StreamableHttpTransport(url=_INTERNAL_BASE_URL, httpx_client_factory=httpx_client_factory)
    async with Client(transport) as client:
        yield client


async def get_mcp_schemas(client: "Client", allowed: list | None) -> list[dict] | None:
    """List MCP tools and convert to the OpenAI-style schema all providers expect.

    Returns None on failure so callers can fall back to the registry's copies
    of the same tools instead of losing them entirely.
    """
    try:
        tools = await client.list_tools()
    except Exception as e:
        logger.warning(f"MCP list_tools failed, falling back to registry: {e}")
        return None

    schemas = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema,
            },
        }
        for t in tools
    ]
    if allowed is not None:
        schemas = [s for s in schemas if s["function"]["name"] in allowed]
    return schemas


async def call_mcp_tool(client: "Client", name: str, args: dict) -> str:
    """Call an MCP tool and normalize the result to ToolRegistry.execute()'s JSON-string contract."""
    try:
        result = await client.call_tool(name, args, raise_on_error=False)
    except Exception as e:
        logger.error(f"MCP transport error calling '{name}': {e}", exc_info=True)
        return error_payload(name, str(e))

    if result.is_error:
        message = "Tool call failed"
        for block in result.content:
            if getattr(block, "type", None) == "text":
                message = block.text
                break
        return error_payload(name, message)

    payload = result.structured_content if result.structured_content is not None else result.data
    return serialize_with_truncation(payload)
