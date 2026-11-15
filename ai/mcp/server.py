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

Two kinds of valid bearer token reach this server:
- `type: "access"` — the normal login cookie's token, or the orchestrator's
  own short-lived internal token (ai/mcp_client.py). Signature + expiry only.
- `type: "mcp"` — a long-lived token a user explicitly generated for an
  external MCP client (routers/mcp_tokens.py, services/mcp_token_service.py).
  These additionally carry a `jti` that's checked against the `mcp_tokens`
  collection on every request, so one can be revoked instantly without
  rotating JWT_SECRET (which would invalidate every user's session).
"""

from fastmcp import FastMCP
from fastmcp.server.auth import TokenVerifier, AccessToken
from fastmcp.server.dependencies import get_access_token

from ai.mcp.access import BirdyAccessMiddleware
from ai.mcp.oauth_provider import verify_birdy_token


class _RevocationAwareVerifier(TokenVerifier):
    """Wraps JWTVerifier: same signature/expiry check for every token, plus a
    revocation lookup for long-lived `type: "mcp"` tokens specifically. Normal
    session/internal `type: "access"` tokens skip the DB round-trip entirely."""

    async def verify_token(self, token: str) -> AccessToken | None:
        # Same logic the OAuth provider's load_access_token uses (ai/mcp/oauth_provider.py).
        return await verify_birdy_token(token)


mcp = FastMCP(name="Birdy AI Tools", auth=_RevocationAwareVerifier())
mcp.add_middleware(BirdyAccessMiddleware())

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
