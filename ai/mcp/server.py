"""
ai/mcp/server.py
------------------
The single shared FastMCP server instance for all of Birdy's migrated tools.
Every ai/mcp/*_mcp.py module imports `mcp` from here and registers its own
tools onto it with @mcp.tool — one MCP server, mounted once at /mcp (see
main.py), exposing every migrated tool to external MCP clients and to
Birdy's own orchestrator (via ai/mcp_client.py) alike.

Auth: BirdyOAuthProvider (ai/mcp/oauth_provider.py) is both the OAuth 2.1
authorization server Claude/ChatGPT connect through (/mcp/authorize, /token,
/register, /revoke; discovery documents at the root, see main.py) and the
verifier for every bearer token: session/orchestrator `type: "access"` JWTs,
manual `type: "mcp"` tokens and OAuth-issued ones, the latter two checked
against mcp_tokens/oauth_grants on every request so revocation is immediate.
user_id is always read from the verified token's claims via current_user_id(),
never accepted as a tool argument.
"""

from fastmcp import FastMCP
from mcp.types import Icon
from fastmcp.server.dependencies import get_access_token

from ai.mcp import voice
from ai.mcp.access import BirdyAccessMiddleware
from ai.mcp.oauth_provider import BirdyOAuthProvider

mcp = FastMCP(
    name="Birdy",
    auth=BirdyOAuthProvider(),
    middleware=[BirdyAccessMiddleware()],
    instructions=voice.INSTRUCTIONS,
    website_url=voice.WEBSITE_URL,
    icons=[Icon(src=voice.ICON_URL, mimeType="image/png", sizes=["128x128"])],
)

# stateless_http=True: required for Vercel — serverless invocations have no
# sticky sessions, so the MCP transport must not rely on server-held session state.
# Built once here so main.py (mounting) and ai/mcp_client.py (internal calls)
# share the exact same ASGI app instance instead of constructing it twice.
mcp_app = mcp.http_app(path="/", stateless_http=True)


def current_user_id() -> str:
    token = get_access_token()
    if token is None or not token.claims.get("sub"):
        raise ValueError("Unauthenticated MCP request: missing or invalid bearer token")
    return token.claims["sub"]
