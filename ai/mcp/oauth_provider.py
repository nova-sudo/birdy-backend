"""
ai/mcp/oauth_provider.py
--------------------------
Birdy's OAuth 2.1 authorization server for the MCP endpoint, so Claude /
ChatGPT "Add custom connector" can register itself (DCR), send the owner to
Birdy's consent page, and get tokens. Storage is services/oauth_store.py.

The /register, /authorize, /token and /revoke HTTP handlers are the MCP SDK's
own: they verify PKCE, client auth, redirect_uri matching and code/refresh
expiry. Only the provider hooks are written here.

load_access_token is also the verifier for every bearer token reaching /mcp
(verify_birdy_token): the chat's in-process `type: "internal"` JWTs, manual
`type: "mcp"` tokens, and the OAuth access tokens issued below (`type: "mcp"`
+ grant_id), all signed with JWT_SECRET. Login cookies (`type: "access"`) are refused.
"""

import calendar
import re
from datetime import datetime
from functools import partial
from urllib.parse import urlparse

from fastmcp.server.auth import AccessToken, OAuthProvider
from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.revoke import RevocationErrorResponse, RevocationHandler, RevocationRequest
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.middleware.client_auth import AuthenticationError, ClientAuthenticator
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, ValidationError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from ai.mcp.access import MCP_TOKEN_TYPES, READ_SCOPE, WRITE_SCOPE
from core.config import API_PUBLIC_URL, FRONTEND_URL, JWT_ALGORITHM, JWT_SECRET
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import oauth_store
from services.mcp_token_service import create_oauth_access_token, is_mcp_token_valid

ACCESS_TOKEN_SECONDS = 3600
# The MCP endpoint is main.py's mount at /mcp; the OAuth endpoints live under it.
MCP_RESOURCE_URL = f"{API_PUBLIC_URL}/mcp"
# Public clients (Claude, ChatGPT register with "none") authenticate by PKCE alone.
CLIENT_AUTH_METHODS = ["client_secret_post", "none"]  # no client_secret_basic: the SDK token handler reads client_id from the form
ALL_SCOPES = [READ_SCOPE, WRITE_SCOPE]

_jwt_verifier = JWTVerifier(public_key=JWT_SECRET, algorithm=JWT_ALGORITHM)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}
_CUSTOM_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*$")
# Schemes that could execute or read locally if a client registered them as a "redirect".
_BLOCKED_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "filesystem"}


async def verify_birdy_token(token: str) -> AccessToken | None:
    """Signature + expiry for every Birdy JWT, and only the two types /mcp serves: Birdy's
    own in-process chat token and `type: "mcp"` tokens (manual or OAuth), which also need a
    live, unrevoked mcp_tokens row (and grant, for OAuth).

    The login cookie (`type: "access"`) is refused: every user holds their own, and the
    internal token's exemption from subscription, credits and rate limits must not reach it.
    External apps connect through OAuth or a manual token instead."""
    result = await _jwt_verifier.verify_token(token)
    if result is None:
        return None
    if result.claims.get("type") not in MCP_TOKEN_TYPES:
        return None
    if result.claims.get("type") == "mcp":
        jti = result.claims.get("jti")
        if not jti:
            return None
        db = get_db(get_shared_mongo_client())
        if not await is_mcp_token_valid(db, jti):
            return None
    return result


def _redirect_uri_allowed(uri: str) -> bool:
    lowered = uri.lower()
    scheme, sep, _ = lowered.partition(":")
    if not sep or "#" in uri:
        return False
    if scheme == "https":
        return True
    if scheme == "http":
        return urlparse(lowered).hostname in _LOOPBACK_HOSTS
    return bool(_CUSTOM_SCHEME.match(scheme)) and scheme not in _BLOCKED_SCHEMES


def _epoch(dt: datetime) -> int:
    """Naive-UTC datetime (as stored in Mongo) -> unix seconds."""
    return calendar.timegm(dt.utctimetuple())


class BirdyAuthorizationCode(AuthorizationCode):
    grant_id: str
    user_id: str


class BirdyRefreshToken(RefreshToken):
    grant_id: str
    user_id: str


class _RevocationHandler(RevocationHandler):
    """The SDK's /revoke (RFC 7009), except a public client may leave out
    client_secret: mcp's RevocationRequest makes the field required, so public
    clients got a 400. Client authentication is unchanged: a client registered
    with a secret still has to send it (ClientAuthenticator, 401 otherwise)."""

    async def handle(self, request: Request) -> Response:
        try:
            client = await self.client_authenticator.authenticate_request(request)
        except AuthenticationError as e:
            return PydanticJSONResponse(
                status_code=401,
                content=RevocationErrorResponse(error="unauthorized_client", error_description=e.message),
            )
        try:
            form = await request.form()
            req = RevocationRequest.model_validate({"client_secret": None, **dict(form)})
        except ValidationError as e:
            return PydanticJSONResponse(
                status_code=400,
                content=RevocationErrorResponse(error="invalid_request", error_description=str(e)),
            )

        loaders = [self.provider.load_access_token, partial(self.provider.load_refresh_token, client)]
        if req.token_type_hint == "refresh_token":
            loaders.reverse()
        token = None
        for loader in loaders:
            token = await loader(req.token)
            if token is not None:
                break
        # Unknown tokens still get 200 (RFC 7009 2.2).
        if token and token.client_id == client.client_id:
            await self.provider.revoke_token(token)
        return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


class BirdyOAuthProvider(OAuthProvider):
    """Endpoints at {API_PUBLIC_URL}/mcp/{authorize,token,register,revoke}; the
    issuer is API_PUBLIC_URL itself, so the discovery documents are served at the
    root app (main.py): /.well-known/oauth-authorization-server and
    /.well-known/oauth-protected-resource/mcp."""

    def __init__(self):
        super().__init__(
            base_url=MCP_RESOURCE_URL,
            issuer_url=API_PUBLIC_URL,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=ALL_SCOPES, default_scopes=ALL_SCOPES,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

    @staticmethod
    def _db():
        return get_db(get_shared_mongo_client())

    # ── routes + discovery metadata ─────────────────────────────────────────

    def _get_resource_url(self, path: str | None = None) -> AnyHttpUrl:
        # base_url already ends in /mcp. FastMCP builds the sub-app with path="/" and
        # main.py asks for well-known routes with mcp_path="/mcp"; both mean this URL
        # (no trailing slash), which is also what the 401's resource_metadata points at.
        return AnyHttpUrl(MCP_RESOURCE_URL)

    def _authorization_server_metadata(self):
        # The SDK sets issuer = base_url (…/mcp), which would not match the
        # authorization_servers entry (the root issuer) the resource metadata advertises.
        metadata = build_metadata(
            self.base_url, self.service_documentation_url,
            self.client_registration_options, self.revocation_options,
        )
        return metadata.model_copy(update={
            "issuer": self.issuer_url,
            "token_endpoint_auth_methods_supported": CLIENT_AUTH_METHODS,
            "revocation_endpoint_auth_methods_supported": CLIENT_AUTH_METHODS,
        })

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = []
        for route in super().get_routes(mcp_path):
            if route.path == "/.well-known/oauth-authorization-server":
                route = Route(
                    route.path,
                    endpoint=cors_middleware(MetadataHandler(self._authorization_server_metadata()).handle,
                                             ["GET", "OPTIONS"]),
                    methods=["GET", "OPTIONS"],
                )
            elif route.path == "/revoke":
                handler = _RevocationHandler(self, ClientAuthenticator(self))
                route = Route("/revoke", endpoint=cors_middleware(handler.handle, ["POST", "OPTIONS"]),
                              methods=["POST", "OPTIONS"])
            routes.append(route)
        return routes

    # ── clients (DCR) ───────────────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        doc = await oauth_store.load_client(self._db(), client_id)
        return OAuthClientInformationFull.model_validate(doc) if doc else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not _redirect_uri_allowed(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        f"Redirect URI not allowed: {uri}. Use https://, http://localhost, "
                        "http://127.0.0.1, or an app scheme."
                    ),
                )
        await oauth_store.save_client(self._db(), client_info.model_dump(mode="json", exclude_none=True))

    # ── authorize: hand off to Birdy's consent page ────────────────────────

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        scopes = params.scopes or (client.scope.split() if client.scope else list(ALL_SCOPES))
        request_id = await oauth_store.create_request(
            self._db(),
            client_id=client.client_id,
            client_name=client.client_name or "An app",
            scopes=scopes,
            state=params.state,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return f"{FRONTEND_URL}/connect/authorize?request={request_id}"

    # ── authorization code ─────────────────────────────────────────────────

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> BirdyAuthorizationCode | None:
        doc = await oauth_store.load_code(self._db(), client.client_id, authorization_code)
        if not doc:
            return None
        return BirdyAuthorizationCode(
            code=authorization_code,
            scopes=doc["scopes"],
            expires_at=float(_epoch(doc["expires_at"])),
            client_id=doc["client_id"],
            code_challenge=doc["code_challenge"],
            redirect_uri=doc["redirect_uri"],
            redirect_uri_provided_explicitly=doc["redirect_uri_provided_explicitly"],
            resource=doc.get("resource"),
            grant_id=doc["grant_id"],
            user_id=doc["user_id"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: BirdyAuthorizationCode
    ) -> OAuthToken:
        db = self._db()
        # The SDK already checked PKCE/redirect/expiry; this makes the code single-use
        # even under a concurrent double-submit.
        doc = await oauth_store.consume_code(db, client.client_id, authorization_code.code)
        if not doc:
            raise TokenError(error="invalid_grant", error_description="authorization code already used")
        grant = await oauth_store.get_active_grant(db, doc["grant_id"])
        if not grant:
            raise TokenError(error="invalid_grant", error_description="this connection was revoked")
        return await self._issue_tokens(db, grant, doc["scopes"], refresh_scopes=doc["scopes"])

    # ── refresh token (rotated on every use) ───────────────────────────────

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> BirdyRefreshToken | None:
        db = self._db()
        doc = await oauth_store.load_refresh_token(db, client.client_id, refresh_token)
        if not doc or not await oauth_store.get_active_grant(db, doc["grant_id"]):
            return None
        return BirdyRefreshToken(
            token=refresh_token,
            client_id=doc["client_id"],
            scopes=doc["scopes"],
            expires_at=_epoch(doc["expires_at"]),
            grant_id=doc["grant_id"],
            user_id=doc["user_id"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: BirdyRefreshToken, scopes: list[str]
    ) -> OAuthToken:
        db = self._db()
        # Atomic delete: of two concurrent uses of one refresh token, only one wins.
        doc = await oauth_store.consume_refresh_token(db, client.client_id, refresh_token.token)
        if not doc:
            raise TokenError(error="invalid_grant", error_description="refresh token already used")
        grant = await oauth_store.get_active_grant(db, doc["grant_id"])
        if not grant:
            raise TokenError(error="invalid_grant", error_description="this connection was revoked")
        return await self._issue_tokens(db, grant, scopes, refresh_scopes=doc["scopes"])

    async def _issue_tokens(self, db, grant: dict, scopes: list[str], refresh_scopes: list[str]) -> OAuthToken:
        access = "read" if grant["access"] == "read" else oauth_store.access_for_scopes(scopes)
        minted = await create_oauth_access_token(
            db,
            user_id=grant["user_id"],
            grant_id=grant["id"],
            client_id=grant["client_id"],
            client_name=grant["client_name"],
            access=access,
            lifetime_seconds=ACCESS_TOKEN_SECONDS,
        )
        refresh, _ = await oauth_store.create_refresh_token(db, grant=grant, scopes=refresh_scopes)
        return OAuthToken(
            access_token=minted["token"],
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=minted["scope"],
            refresh_token=refresh,
        )

    # ── access tokens + revocation ─────────────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        return await verify_birdy_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoking any token of an OAuth connection revokes the whole grant."""
        if isinstance(token, BirdyRefreshToken):
            grant_id = token.grant_id
        else:
            grant_id = (getattr(token, "claims", None) or {}).get("grant_id")
        if not grant_id:
            return  # not an OAuth-issued token (session or manual): nothing to revoke here
        db = self._db()
        grant = await oauth_store.get_active_grant(db, grant_id)
        if grant and grant["client_id"] == token.client_id:
            await oauth_store.revoke_grant(db, grant_id)
