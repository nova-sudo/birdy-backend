"""
ai/mcp/server.py
------------------
The single shared FastMCP server instance for all of Birdy's migrated tools.
Every ai/mcp/*_mcp.py module imports `mcp` from here and registers its own
tools onto it with @mcp.tool — one MCP server, mounted once at /mcp (see
main.py), exposing every migrated tool to external MCP clients and to
Birdy's own orchestrator (via ai/mcp_client.py) alike.

Auth: connections are authenticated with Birdy's existing HS256 JWT (same
secret/algorithm as the `auth_token` cookie — see core/config.py). user_id is
always read from the verified token's claims via _current_user_id(), never
accepted as a tool argument, so the LLM/caller can't impersonate another user.
"""

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token

from core.config import JWT_SECRET, JWT_ALGORITHM

_verifier = JWTVerifier(public_key=JWT_SECRET, algorithm=JWT_ALGORITHM)

mcp = FastMCP(name="Birdy AI Tools", auth=_verifier)

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
