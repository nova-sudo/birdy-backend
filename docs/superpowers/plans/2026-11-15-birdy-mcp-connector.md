# Birdy MCP Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any Birdy account owner can add Birdy to Claude / ChatGPT / Cursor with one link, log in, and use their data, alerts, and media buying agent from that agent. v1 is Phases 1–4; v2 is Phase 5.

**Architecture:** The existing FastMCP server at `/mcp` (`ai/mcp/server.py`) gains a Mongo-backed OAuth 2.1 authorization server (FastMCP `OAuthProvider` subclass), a scope + subscription middleware, and a server-side confirm-code gate for live changes. New tools are thin `@mcp.tool` wrappers over the existing service / `ai/tools/*` functions, never copies. Frontend (birdy-beta) gets a consent page and a "Connect to AI" settings tab.

**Tech Stack:** Python 3.12, FastAPI, fastmcp 2.14.7, mcp 1.24.0, Motor/Mongo, PyJWT, httpx, pytest (asyncio_mode=auto, `mock_db` / `set_current_user` fixtures in `tests/conftest.py`). Frontend: Next.js, React (JSX), vitest, `@/components/ui/*` + pd-* kit.

**Spec:** `docs/superpowers/specs/2026-11-15-birdy-mcp-connector-design.md`

**Repos / worktrees:**
- Backend: `/Users/abdelrahman/Desktop/Birdy/birdy-backend-mcp`, branch `feat/birdy-mcp` (from origin/main b0a3511).
- Frontend: create `/Users/abdelrahman/Desktop/Birdy/birdy-beta-mcp`, branch `feat/birdy-mcp`, from `origin/main` (`git -C ../birdy-beta worktree add -b feat/birdy-mcp ../birdy-beta-mcp origin/main`).

## Global Constraints

- `user_id` comes only from the verified token (`current_user_id()`), never from a tool argument. Every new tool gets a cross-account test (alice's data invisible to bob).
- Scopes: `birdy.read` ("View my data") and `birdy.write` ("View and make changes"). A write-scope token also has read.
- Every MCP tool is tagged exactly one of `read` / `write`; tools that change live ads, campaigns, or post to Slack are additionally tagged `live` and use the confirm-code two-step.
- Confirm codes: single-use, bound to user + tool + exact arguments, valid 10 minutes.
- OAuth access tokens: `type:"mcp"` JWT, 1 hour; refresh tokens rotate on every use, 90-day lifetime. Manual tokens keep `DEFAULT_EXPIRY_DAYS = 365`.
- External (`type:"mcp"`) calls are rate-limited to 120 per minute per user, and require an active/trialing subscription (`billing_middleware.require_active_subscription`). No credits charged for MCP use. Internal orchestrator tokens (`type:"access"`) skip this gate and keep existing credit metering.
- Admin impersonation sessions (`imp` claim true) cannot approve an OAuth request.
- MCP never exposes: admin tools, billing, integration connect/remove, account settings, tracking install, the global refresh-all cycle (`jobs/refresh_all_job` is not per-user).
- Tool docstrings are written for an outside agent: say what the tool returns, when to use it, and, for `live` tools, "Show the preview to the user and only call again with confirm_code after they say yes."
- Existing Ask Birdy behaviour is unchanged: do not add new tools to `ai/mcp_client.MCP_TOOL_NAMES` (the orchestrator keeps its internal paths for them).
- `source="mcp"` on every service call that records who/where an action came from.
- UI: built from the mock the owner picks in Task 0; pd-* kit; verified by build + crawl + screenshots (`next start --port 3005`, `NEXT_PUBLIC_API_URL=http://localhost:8000`).
- New env vars: `API_PUBLIC_URL` (default `https://birdy-backend.vercel.app`), `RESEND_API_KEY`, `EMAIL_FROM` (default `Birdy <alerts@birdy.ai>`; builder confirms the verified sender domain).

## Review Focus

1. **An alert that stays triggered** is evaluated every 15 minutes; the owner must get one email per firing (on the transition into triggered), not one per pass. → test in Task 23.
2. **A refresh token used twice** (replay after rotation) must be rejected, and the new token from the first use keeps working. → test in Task 4.
3. **A confirm code replayed with different arguments** (e.g. other `line_ids`, another card) must be refused with nothing changed. → test in Task 2.
4. **Ambiguous names** ("Acme" matching "Acme Dental" and "Acme Law"; two custom agents with similar names) must return the candidates and change nothing, never pick one silently. → tests in Tasks 9 and 15.
5. **Revoking a connection in Birdy** must stop the very next call with an already-issued access token, not an hour later. → test in Task 6.

---

## Phase 0 — UI direction (gate)

### Task 0: Pick the Connect page and consent screen mocks

Mocks are at `/Users/abdelrahman/Desktop/Birdy/mocks/mcp-connect/` (2–3 directions each for the Connect to AI tab and the consent screen, plus the alert-form email toggle). The owner picks one per screen before Task 7 starts.

- [ ] **Step 1:** Record the picked option for each screen in the spec under "4. Connect page" (one line each, with the mock path), commit `docs(mcp): record chosen connect mocks`.

---

## Phase 1 — One-link install

### Task 1: Scopes, tool tags, and the access middleware

**Files:**
- Create: `ai/mcp/access.py`
- Modify: `ai/mcp/server.py` (register middleware), every existing `ai/mcp/*_mcp.py` (add tags to each `@mcp.tool`)
- Test: `tests/test_mcp_access.py`

**Interfaces:**
- Produces (in `ai/mcp/access.py`):
  - `READ_SCOPE = "birdy.read"`, `WRITE_SCOPE = "birdy.write"`
  - `READ = {"tags": {"read"}, "annotations": {"readOnlyHint": True}}`
  - `WRITE = {"tags": {"write"}, "annotations": {"readOnlyHint": False, "destructiveHint": False}}`
  - `LIVE = {"tags": {"write", "live"}, "annotations": {"readOnlyHint": False, "destructiveHint": True}}`
  - Usage everywhere: `@mcp.tool(**READ)` / `@mcp.tool(**WRITE)` / `@mcp.tool(**LIVE)`
  - `def token_scopes(claims: dict) -> set[str]`: the space-separated `scope` claim; a missing claim means `{READ_SCOPE, WRITE_SCOPE}` (internal `type:"access"` tokens and pre-existing manual tokens).
  - `class BirdyAccessMiddleware(fastmcp.server.middleware.Middleware)` with `on_call_tool`.
- Existing tools: `get_*`, `list_*`, `compare_periods`, `compute_custom_metric` are READ; `create_alert`, `update_alert`, `create_custom_metric`, `update_custom_metric`, `delete_custom_metric` are WRITE.

- [ ] **Step 1: Write the failing tests**

```python
async def test_every_registered_tool_is_tagged_read_or_write():
    tools = await mcp.get_tools()
    for name, tool in tools.items():
        assert len(tool.tags & {"read", "write"}) == 1, name
        if "live" in tool.tags:
            assert "write" in tool.tags, name

def test_token_scopes_defaults_to_full_when_claim_missing():
    assert token_scopes({"type": "access"}) == {"birdy.read", "birdy.write"}
    assert token_scopes({"type": "mcp", "scope": "birdy.read"}) == {"birdy.read"}

async def test_read_only_token_cannot_call_write_tool(mcp_call):      # mcp_call: fixture driving the real ASGI app with a minted token
    res = await mcp_call("create_alert", {...valid args...}, scope="birdy.read")
    assert res.is_error and "view-only" in res.text

async def test_external_token_without_subscription_is_refused(mcp_call, no_subscription):
    res = await mcp_call("get_alerts", {}, token_type="mcp")
    assert res.is_error and "subscription is inactive" in res.text

async def test_internal_access_token_skips_subscription_gate(mcp_call, no_subscription):
    res = await mcp_call("get_alerts", {}, token_type="access")
    assert not res.is_error

async def test_external_calls_rate_limited_per_user(mcp_call):
    # 120 calls per rolling minute per user for type:"mcp"; call 121 → error, another user unaffected
    ...
    assert res.is_error and "Too many Birdy requests" in res.text
```

- [ ] **Step 2:** Run `pytest tests/test_mcp_access.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement `ai/mcp/access.py`. The middleware reads the tool's tags via `context.fastmcp_context.fastmcp.get_tool(name)` and the token via `get_access_token()`. A write tool without `WRITE_SCOPE` raises `ToolError("This Birdy connection is view-only. Reconnect with \"View and make changes\" to do this.")`. For `type:"mcp"`, call `require_active_subscription`; on HTTPException 402 raise `ToolError("Your Birdy subscription is inactive. Reactivate it at <FRONTEND_URL>/settings?tab=billing to keep using Birdy here.")`. Add the `mcp_call` fixture to `tests/conftest.py` (mints a JWT the same way `ai/mcp_client.py` does, with optional `scope` and `type`, and calls through `httpx.ASGITransport` into `mcp_app`). Rate limit (`type:"mcp"` only): `MCP_RATE_LIMIT_PER_MINUTE = 120`, counted in Mongo collection `mcp_rate` keyed `{user_id, minute}` with `$inc` and a TTL index (stateless Vercel, so no in-memory counter); over the limit raise `ToolError("Too many Birdy requests in a minute. Wait a moment and try again.")`. Tag every existing tool.
- [ ] **Step 4:** Run `pytest tests/test_mcp_access.py tests/test_alert_mcp.py -v`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): read/write scopes, tool tags, subscription gate`.

### Task 2: Confirm codes for live changes

**Files:**
- Create: `ai/mcp/confirm.py`
- Modify: `main.py` lifespan (index creation)
- Test: `tests/test_mcp_confirm.py`

**Interfaces:**
- Produces:
  - `CONFIRM_TTL_SECONDS = 600`, collection `mcp_confirm_codes` (TTL index on `expires_at`)
  - `async def issue_code(db, user_id: str, tool: str, args: dict) -> str`: stores `sha256(canonical_json(args))`, where canonical JSON uses sorted keys and leaves out `confirm_code`.
  - `async def consume_code(db, user_id: str, tool: str, args: dict, code: str) -> bool`: atomic `find_one_and_delete` on code + user + tool + args hash + not expired.
  - `async def two_step(db, user_id: str, tool: str, args: dict, confirm_code: str | None, preview: Callable[[], Awaitable[dict]], execute: Callable[[], Awaitable[dict]]) -> dict`:
    - no code: returns `{"needs_confirmation": True, "preview": <preview()>, "confirm_code": <code>, "expires_in_seconds": 600, "next": "Show this preview to the user. If they agree, call this tool again with the same arguments plus confirm_code."}`
    - valid code: returns `execute()`
    - invalid code: returns `{"error": "This confirmation code is invalid, expired, or for different arguments. Nothing was changed. Call again without confirm_code to get a fresh preview."}`

- [ ] **Step 1: Write the failing tests**

```python
async def test_first_call_returns_preview_and_code_without_executing(mock_db): ...  # execute mock not awaited
async def test_valid_code_executes_once(mock_db): ...                              # second use → error, execute called once
async def test_code_with_different_args_is_refused(mock_db):                       # Review Focus 3
    code = await issue_code(mock_db, "alice", "apply_card", {"card_id": "c1", "line_ids": ["a"]})
    assert not await consume_code(mock_db, "alice", "apply_card", {"card_id": "c1", "line_ids": ["a", "b"]}, code)
async def test_code_for_other_user_or_tool_is_refused(mock_db): ...
async def test_expired_code_is_refused(mock_db, freeze_time_plus_601s): ...
```

- [ ] **Step 2:** Run `pytest tests/test_mcp_confirm.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `ai/mcp/confirm.py`. Codes are 6 random URL-safe bytes, via `secrets.token_urlsafe(6)`. Create the index in the `main.py` lifespan, next to `create_mcp_tokens_indexes`.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): server-enforced confirm codes for live changes`.

### Task 3: Access level on manual tokens + connections API

**Files:**
- Modify: `core/models.py` (`CreateMcpTokenRequest.access: Literal["read","write"] = "write"`), `services/mcp_token_service.py`, `routers/mcp_tokens.py`
- Test: `tests/test_mcp_tokens.py` (extend or create)

**Interfaces:**
- `create_mcp_token(db, user_id, name, expiry_days=None, access="write")` adds claim `scope` (`"birdy.read"` or `"birdy.read birdy.write"`) and stores `access` and `kind: "manual"`.
- `is_mcp_token_valid(db, jti)` also returns False when the doc has a `grant_id` whose `oauth_grants` doc is revoked.
- `GET /api/mcp/connections` → `[{id, kind: "manual"|"oauth", name, access, created_at, last_used_at, expires_at|None}]`. Manual tokens come from `mcp_tokens` where `kind != "oauth"`. OAuth grants come from `oauth_grants` (Task 4). Revoked ones are left out.
- `DELETE /api/mcp/connections/{id}` revokes a manual token or an OAuth grant (and all its tokens) owned by the caller. 404 otherwise.
- `last_used_at`: updated by the verifier at most once per 10 minutes per jti/grant.

- [ ] **Step 1: Write the failing tests:** `test_read_token_carries_read_scope_only`, `test_connections_lists_manual_and_oauth_for_caller_only`, `test_delete_connection_of_other_user_404`, `test_revoked_grant_invalidates_its_access_tokens`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): token access levels and connections API`.

### Task 4: Mongo-backed OAuth provider

**Files:**
- Create: `ai/mcp/oauth_provider.py`, `services/oauth_store.py`
- Modify: `core/config.py` (`API_PUBLIC_URL`)
- Test: `tests/test_oauth_provider.py`

**Interfaces:**
- Collections:
  - `oauth_clients`: DCR client info.
  - `oauth_requests`: a pending authorize request: `id`, `client_id`, the params, and `expires_at`, which is 10 minutes after creation.
  - `oauth_codes`: `expires_at` is 5 minutes after creation, and each code can be used once.
  - `oauth_refresh_tokens`: holds a sha256 of the token, plus `grant_id` and `expires_at` (90 days).
  - `oauth_grants`: `id`, `user_id`, `client_id`, `client_name`, `redirect_host`, `access`, `created_at`, `last_used_at`, `revoked`.
  - All of these get TTL indexes where they have `expires_at`.
- `class BirdyOAuthProvider(fastmcp.server.auth.OAuthProvider)` constructor:
  - `base_url=f"{API_PUBLIC_URL}/mcp"` and `issuer_url=API_PUBLIC_URL`
  - `client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["birdy.read","birdy.write"], default_scopes=["birdy.read","birdy.write"])`
  - `revocation_options=RevocationOptions(enabled=True)`
- `authorize(client, params) -> str`: stores an `oauth_requests` doc and returns `f"{FRONTEND_URL}/connect/authorize?request={id}"`.
- `services/oauth_store.py`:
  - `async def approve_request(db, request_id: str, user_id: str, access: Literal["read","write"]) -> str`: creates the grant and the code, then returns the client's `redirect_uri` with `code` and `state` appended.
  - `async def deny_request(db, request_id: str) -> str`: returns `redirect_uri?error=access_denied&state=`.
  - `async def get_request_view(db, request_id) -> dict | None`: returns `{client_name, redirect_host, requested_access}`.
- `exchange_authorization_code`: issues the access JWT (`type:"mcp"`, `sub`, `jti`, `scope`, `grant_id`, `client_id`, 1h) and records it in `mcp_tokens` with `kind:"oauth"` and `grant_id`. It also issues a new refresh token.
- `exchange_refresh_token`: deletes the old refresh token. A refresh token that has already been used fails with an `invalid_grant` error.
- `load_access_token`: carries over today's `_RevocationAwareVerifier` logic (JWT check, plus the jti/grant check for `type:"mcp"`). It returns an `AccessToken` with `claims` set, so `current_user_id()` keeps working for every token type.
- `revoke_token`: revokes the whole grant.
- Redirect URIs accepted at registration: any `https://`, `http://localhost:*` and `http://127.0.0.1:*`, and custom schemes (`cursor://`, `vscode://`). `http://` to other hosts is rejected.

- [ ] **Step 1: Write the failing tests**
  - `test_register_client_rejects_plain_http_non_localhost`
  - `test_authorize_returns_frontend_consent_url_and_stores_request`
  - `test_approve_creates_grant_and_single_use_code`
  - `test_exchange_code_with_wrong_pkce_verifier_fails` (through the `/token` route)
  - `test_refresh_rotates_and_old_refresh_token_is_rejected` (Review Focus 2: the first exchange returns R2; reusing R1 fails; R2 still works)
  - `test_issued_access_token_scope_matches_grant_access`
  - `test_existing_session_and_manual_tokens_still_verify`
- [ ] **Step 2:** Run `pytest tests/test_oauth_provider.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. The `/token` and `/register` handlers are FastMCP's own, so only the provider methods are written here.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): Mongo-backed OAuth 2.1 provider`.

### Task 5: Consent API

**Files:**
- Create: `routers/mcp_oauth.py`
- Modify: `main.py` (include the router)
- Test: `tests/test_mcp_oauth_router.py`

**Interfaces:**
- `GET /api/oauth/requests/{id}` (cookie auth) returns `{client_name, redirect_host, requested_access: "read"|"write"}`, or 404 if the request is missing or expired.
- `POST /api/oauth/requests/{id}/decision` takes `{allow: bool, access: "read"|"write"}` and returns `{redirect_to: str}`.
  - It uses `get_current_claims`, and returns 403 `"Admins can't connect AI tools while viewing as a user."` when `imp` is true.
  - It returns 402 when there's no active subscription, via `require_active_subscription`.

- [ ] **Step 1: Write the failing tests:** `test_get_request_view_requires_login`, `test_allow_returns_redirect_with_code_and_state`, `test_deny_returns_access_denied_redirect`, `test_impersonation_cannot_approve`, `test_no_subscription_cannot_approve`, `test_expired_request_404`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement on top of `services/oauth_store.py`.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): consent endpoints`.

### Task 6: Wire OAuth into the server + end-to-end flow

**Files:**
- Modify: `ai/mcp/server.py`: `mcp = FastMCP(name="Birdy", auth=BirdyOAuthProvider(), middleware=[BirdyAccessMiddleware()], instructions=<below>)`. Delete `_RevocationAwareVerifier`, since its logic moved in Task 4.
- Modify: `main.py`: add `mcp.auth.get_well_known_routes(mcp_path="/mcp")` to the root app, so these are served at the root:
  - `/.well-known/oauth-protected-resource/mcp`
  - `/.well-known/oauth-authorization-server`
  - Keep the existing mount at `/mcp`.
- Test: `tests/test_mcp_oauth_e2e.py`

Server `instructions` (exact copy):
> Birdy is a marketing analytics and media buying assistant for agencies. Use these tools to read the user's clients' Meta ads, GoHighLevel, and lead data, manage alerts, and operate their Birdy Media Buying Agent. Client and agent names can be passed as written; if a tool returns candidates, ask the user which one they meant. Tools marked as making live changes return a preview first: show it and only confirm after the user agrees. Read a client's notes before giving advice about that client.

- [ ] **Step 1: Write the failing test.** `test_full_connect_flow` takes these steps against the ASGI app:
  1. Fetch both well-known docs (200, and the `authorization_endpoint` ends `/mcp/authorize`).
  2. Register a client with DCR.
  3. Call `/mcp/authorize` with PKCE, which redirects to `FRONTEND_URL/connect/authorize?request=`.
  4. POST the decision as a logged-in user, with `access: "read"`.
  5. Exchange the code at `/mcp/token`.
  6. Call `get_alerts` (succeeds), then `create_alert` (view-only error).
  7. Refresh the token.
  8. `DELETE /api/mcp/connections/{grant}`.
  9. The next `get_alerts` with the still-unexpired access token is unauthorized (Review Focus 5).
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Wire it up. Confirm `ai/mcp_client.py` internal calls still pass, because its `type:"access"` tokens verify through `load_access_token`.
- [ ] **Step 4:** Run `pytest -q`. Expected: the whole suite passes.
- [ ] **Step 5:** Commit `feat(mcp): OAuth discovery and end-to-end connect`.

### Task 7: Consent page (frontend)

**Files (birdy-beta-mcp):**
- Create: `src/app/connect/authorize/page.jsx`, `src/components/connect/ConsentCard.jsx`
- Test: `src/components/connect/__tests__/ConsentCard.test.jsx`

**Behaviour:**
- If the user isn't logged in, `apiRequest` 401 sends them to `/login?redirect=/connect/authorize?request=<id>` (LoginForm already honours `redirect`).
- The page shows the client name, the redirect host, and two choices: "View my data" / "View and make changes". The default is whatever was requested, otherwise "View and make changes".
- Allow / Deny POST the decision, then `window.location.assign(redirect_to)`.
- Errors get their own copy:
  - Expired or missing request: "This connection request expired. Go back to <client> and try connecting again."
  - 403 impersonation: show the server message.
  - 402: "An active Birdy subscription is needed to connect." with a link to billing.
- Layout follows the chosen mock from Task 0. The mascot appears only inside `BirdyAvatar`.

- [ ] **Step 1: Write the failing tests:** `renders client name and redirect host`, `allow posts chosen access and navigates to redirect_to`, `deny posts allow=false`, `shows expired copy on 404`.
- [ ] **Step 2:** Run `npx vitest run src/components/connect`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(connect): OAuth consent page`.

### Task 8: "Connect to AI" settings tab (frontend)

**Files (birdy-beta-mcp):**
- Create: `src/components/settings/ConnectAI.jsx`, `src/components/settings/connect-ai-steps.js`
- Modify: `src/app/settings/page.jsx` (add tab `ai` labelled "Connect to AI" to the allowed tabs list and the PageTabs)
- Test: `src/components/settings/__tests__/ConnectAI.test.jsx`

**Content:**
- The MCP URL (`${API_BASE_URL}/mcp`) with a copy button.
- Tabs for claude.ai, Claude Desktop, Claude Code, ChatGPT and Cursor. Each tab's steps live in `connect-ai-steps.js` as data. Claude Code shows `claude mcp add --transport http birdy <url>`.
- A connections list from `GET /api/mcp/connections`, with a revoke button that calls `DELETE` and asks for confirmation through the existing `alert-dialog`.
- "Create token (for developers)": a name, an access level, and a one-time reveal of the token.
- The daily check-in prompt with a copy button (exact copy): `Every weekday at 8am, use Birdy's check_my_accounts tool and tell me only what needs my attention today, most urgent first.`

- [ ] **Step 1: Write the failing tests:** `copies MCP url`, `lists connections and revokes after confirm`, `creates read-only token and shows it once`, `shows check-in prompt`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement following the chosen mock.
- [ ] **Step 4:** Run vitest. Then build with `NEXT_PUBLIC_API_URL=http://localhost:8000 npx next build`, start on 3005, crawl `/settings?tab=ai` and `/connect/authorize?request=x` and take screenshots. Expected: tests PASS, and the screenshots match the mock.
- [ ] **Step 5:** Commit `feat(settings): Connect to AI tab`.

---

## Phase 2 — Media buying agent

### Task 9: Agent lookup, default agent, list_agents

**Files:**
- Create: `ai/agents/access.py`, `ai/mcp/agent_mcp.py`
- Modify:
  - `routers/agent_stock.py`: `_stock_agent` and `_require_card` call `ai/agents/access.py` and raise 404 on None.
  - `routers/agents.py`: `_agent_or_404` does the same.
  - `ai/mcp/__init__.py`: import the new modules.
- Test: `tests/test_agent_mcp.py`

**Interfaces:**
- `ai/agents/access.py`:
  - `async def find_agent(db, user_id, agent_id) -> dict | None`
  - `async def find_stock_agent(db, user_id, agent_id) -> dict | None`
  - `async def find_card(db, user_id, agent_id, card_id) -> dict | None`
- `ai/mcp/agent_mcp.py`:
  - `async def resolve_agent(db, user_id: str, agent: str | None, kind: Literal["stock","custom","any"] = "any") -> dict` returns the agent doc, or raises `ToolError`.
  - Resolution order:
    1. If `agent` is given: an exact id, then a case-insensitive exact name, then a unique substring. Several matches mean `ToolError("Several agents match '<x>': A, B. Which one?")`.
    2. If `agent` isn't given: the `users.mcp_default_agent_id` agent, if it still exists. Otherwise the user's stock agent. Otherwise `ToolError("You don't have a media buying agent yet. Set one up in Birdy: <FRONTEND_URL>/agents")`.
  - If the resolved agent's kind doesn't match `kind`, the `ToolError` names the tool family to use instead.
  - Tools: `list_agents()` (READ) returns `[{id, name, kind, status, is_default}]`. `set_default_agent(agent: str)` (WRITE) stores `users.mcp_default_agent_id` and returns `{default: {id, name, kind}}`.
- The default is stored per user, so it applies to every connection (spec section 2).

- [ ] **Step 1: Write the failing tests:**
  - `test_default_is_stock_agent`
  - `test_set_default_then_resolve_uses_custom`
  - `test_ambiguous_agent_name_returns_candidates` (Review Focus 4)
  - `test_cannot_resolve_other_users_agent_by_id`
  - `test_router_404s_unchanged`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run, including `pytest tests -k "agent"`. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): agent resolution and default agent`.

### Task 10: Stock agent read tools

**Files:**
- Create: `ai/mcp/stock_agent_mcp.py`
- Modify: the `ai/agents/stock/*` modules only where a route's logic is inline. Move it into a function in the matching module and have the route call that function.
- Test: `tests/test_stock_agent_mcp.py`

**Tools (all READ, and each takes `agent: str | None = None`):** each wraps the matching GET route in `routers/agent_stock.py`.

| Tool | Wraps |
|---|---|
| `get_agent_overview` | settings + latest run summary + open-card count |
| `get_agent_runs` | `limit=10` |
| `get_agent_run` | `run_id` |
| `get_agent_month` | `month: "YYYY-MM"` |
| `get_agent_cards` | `status: Literal["open","applied","dismissed","all"]="open"` |
| `get_agent_card` | `card_id`; result shaped with `stock_card_tools.card_view` |
| `get_agent_lessons` | |
| `get_agent_outcomes` | |
| `get_agent_slack_template` | `kind` |

- [ ] **Step 1: Write the failing tests:** one happy-path test per tool against seeded `mock_db` docs, plus `test_stock_tools_reject_custom_agent_with_hint` and `test_cards_of_other_user_invisible`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): stock agent read tools`.

### Task 11: Stock agent write tools

**Files:**
- Modify: `ai/mcp/stock_agent_mcp.py`
- Test: `tests/test_stock_agent_mcp.py`

**Tools:**
- **LIVE** (two-step from Task 2; the preview is `card_view(card)` limited to the affected lines, plus the per-client `spend`/`cpl`):
  - `apply_card(card_id, line_ids: list[str] | None = None, agent=None, confirm_code=None)` → `stock_apply.apply_card(db, mongo_client, user, card_id, by=user, source="mcp", line_ids=line_ids, budget_s=APPLY_BUDGET_SECONDS)`
  - `apply_all_cards(run_id, agent=None, confirm_code=None)` → `stock_apply.apply_run(...)`. The preview lists every open card in the run.
  - `undo_card_line(card_id, line_id, agent=None, confirm_code=None)` → `stock_apply.undo_line(...)`
  - `post_weekly_report(run_id, agent=None, confirm_code=None)` → the logic behind `POST /runs/{run_id}/weekly/post`. The preview is the rendered weekly text.
- **WRITE** (no two-step):
  - `dismiss_card(card_id, reason=None)`
  - `edit_card(card_id, keep_line_ids, additional_changes=None)`, reusing `stock_card_tools.edit_card`
  - `add_agent_lesson(text, scope="client")`, reusing `stock_card_tools.add_lesson`
  - `update_agent_settings(**fields)`: the same pydantic body as `PATCH /settings`
  - `set_agent_slack_template(kind, template)` and `reset_agent_slack_template(kind)`
- The result of every live call carries the service's outcome dict unchanged, plus `"summary"`, which is one sentence on what changed.

- [ ] **Step 1: Write the failing tests:**
  - `test_apply_card_first_call_changes_nothing` (assert `stock_apply.apply_card` is not called)
  - `test_apply_card_with_code_applies_with_source_mcp`
  - `test_apply_card_code_for_other_line_ids_refused`
  - `test_read_scope_cannot_dismiss`
  - `test_edit_card_keeps_only_given_lines`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): stock agent actions with confirmation`.

### Task 12: Custom agent tools

**Files:**
- Create: `ai/mcp/custom_agent_mcp.py`
- Modify: `routers/agents.py`, only to move inline logic into `ai/agents/*` functions as in Task 10
- Test: `tests/test_custom_agent_mcp.py`

**Tools** (each takes `agent=None`; a stock agent gets a hint to use the stock tools):
- **READ:**
  - `get_agent_playbook`, reusing `agent_tools.get_playbook`
  - `get_agent_activity`
  - `get_agent_history`
  - `get_agent_claims`
  - `get_agent_impact`
  - `get_agent_confidence`
  - `get_agent_proposals(status="open")`
  - `get_agent_memory`
  - `get_agent_offers`
- **WRITE:**
  - `accept_proposal(proposal_id)` and `reject_proposal(proposal_id)`: same branching as the routes, including the PROMOTE → lesson path. A 409 becomes an error dict with the route's message.
  - `set_rule_param`, `set_client_target_for_agent`, `set_client_override`, `add_agent_memory`, `add_agent_exception`, `set_agent_schedule` and `set_agent_slack_channel`, reusing the `agent_tools` functions of the same names
  - `delete_agent_memory(memory_id)`
  - `graduate_rule(rule_id)`
  - `start_dry_run()` → `{job_id}`, and `get_dry_run(job_id)` (READ)
- **LIVE:**
  - `activate_agent(confirm_code=None)`. The preview covers the schedule, the clients it covers, and what it may change.
  - `deactivate_agent()` (WRITE)
  - `undo_auto_action(suggestion_id, confirm_code=None)`

- [ ] **Step 1: Write the failing tests:** `test_accept_proposal_updates_rule`, `test_accept_closed_proposal_returns_error_not_raise`, `test_activate_needs_confirmation`, `test_custom_tools_reject_stock_agent_with_hint`, `test_dry_run_job_poll`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): custom agent tools`.

### Task 13: Dashboard suggestion tools

**Files:**
- Create: `ai/mcp/suggestions_mcp.py`
- Test: `tests/test_suggestions_mcp.py`

**Tools:**
- `get_suggestions(status="open")` (READ): wraps the GET list route in `routers/dashboard.py`.
- `apply_suggestion(suggestion_id, confirm_code=None)` (LIVE) → `actions.apply_suggestion(db, mongo_client, user, id, actor=store.ACTOR_USER, source="mcp")`. The preview is the suggestion's action and the ads it touches.
- `deny_suggestion(suggestion_id, reason_code, note=None)` (WRITE): `reason_code` is validated against `DENY_REASONS`, and the docstring lists the allowed values.
- `undo_suggestion(suggestion_id, confirm_code=None)` (LIVE) → `actions.undo_suggestion(...)`.
- The outcomes `not_found` / `failed` / `closed` / `busy` / `not_undoable` become error dicts using the routes' messages.

- [ ] **Step 1: Write the failing tests:** `test_apply_suggestion_two_step`, `test_deny_rejects_unknown_reason`, `test_closed_suggestion_returns_conflict_message`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): dashboard suggestion tools`.

---

## Phase 3 — Remaining tools and app features

### Task 14: Port the remaining Ask Birdy tools

**Files:**
- Create:
  - `ai/mcp/multi_window_mcp.py`: `get_daily_metrics`, `get_metrics_by_day_windows` (READ)
  - `ai/mcp/hp_mcp.py`: `get_call_center_stats` (READ)
  - `ai/mcp/call_analysis_mcp.py`: `get_call_recordings_summary` (READ) and `analyze_call_recordings`. The latter is WRITE and two-step: it starts paid transcription, and its existing `confirmed=True` argument is replaced externally by `confirm_code`.
  - `ai/mcp/creatives_mcp.py`: READ `find_ads`, `list_ad_accounts`, `list_adsets`, `list_creative_tests`, `get_creative_test`; WRITE `draft_creative_test`; LIVE `post_creative_test`, `scale_creative_test`
  - `ai/mcp/attribution_mcp.py`: `get_closes_by_ad` (READ)
- Test: `tests/test_parity_mcp.py`

Each tool's signature is the internal tool's JSON-schema parameters as typed args. The body calls the `ai/tools/*` function with `db`, `current_user_id()` and `mongo_client=get_shared_mongo_client()`.

These are excluded, because they need an open Birdy screen or are covered elsewhere:
- `dashboard_*`, `catalog_search`, `list_view_columns` and `create_saved_view`: they need an open Birdy editor.
- `render_ui_tools`.
- `agent_tools` setup-only tools.
- `stock_card_tools`: covered by Task 11.
- Report designer tools: v2.

- [ ] **Step 1: Write the failing tests:**
  - `test_parity_tools_registered`: every name above is present in `mcp.get_tools()`.
  - One delegation test per module (patch the `ai/tools` function and assert it was called with `user_id` from the token).
  - `test_post_creative_test_needs_confirmation`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): remaining Ask Birdy tools`.

### Task 15: Client context tools

**Files:**
- Create: `ai/mcp/client_mcp.py`
- Test: `tests/test_client_mcp.py`

**Interfaces:**
- `async def resolve_client(db, user_id, client: str) -> dict`: matches on name or id the same way as `agent_tools._match_client`. Several matches mean `ToolError("Several clients match '<x>': A, B. Which one?")`.
- Tools:
  - `get_client(client)` (READ) returns `{id, name, targets, ad_account_currency, integrations: {meta, ghl, hotprospector: bool}, last_refreshed: {meta, ghl}, notes: [latest 10]}`.
  - `get_client_notes(client)` (READ).
  - `add_client_note(client, text)` (WRITE).
  - `delete_client_note(client, note_id)` (WRITE). These wrap the `routers/client_notes.py` logic, moved into `services/client_notes.py` if inline.
  - `set_client_target(client, cpl: float | None = None, leads_per_week: int | None = None)` (WRITE): the logic behind `PUT /api/agents/{id}/client-targets/{gid}`, which writes `group.targets`. Values must be positive.

- [ ] **Step 1: Write the failing tests:** `test_ambiguous_client_returns_candidates` (Review Focus 4), `test_add_then_get_note`, `test_other_users_client_not_found`, `test_set_target_rejects_negative`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): client context tools`.

### Task 16: Lead tools

**Files:**
- Create: `ai/mcp/leads_mcp.py`
- Test: `tests/test_leads_mcp.py`

**Tools (READ):** each resolves `contact_id` → group / location the same way as `routers/leads.py`.
- `get_lead(contact_id)`: `build_lead_detail` + `build_lead_journey` + `get_lead_summary`
- `get_lead_messages(contact_id)` → `fetch_lead_messages`
- `get_call_transcript(contact_id, message_id)` → `fetch_call_transcript`

`MessagingReconnect` / `MessagingUnavailable` become error dicts. The reconnect error says: "GoHighLevel needs reconnecting in Birdy (Settings → Integrations)."

- [ ] **Step 1: Write the failing tests:** `test_get_lead_combines_detail_journey_summary`, `test_lead_of_other_user_not_found`, `test_reconnect_error_message`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): lead tools`.

### Task 17: Attribution tools

**Files:**
- Create: `ai/mcp/attribution_mcp.py` (extends the module from Task 14)
- Test: `tests/test_attribution_mcp.py`

**Tools (READ, `client` resolved via `resolve_client`):** each wraps the matching `routers/attribution.py` GET route.
- `attribution_status(client)`: merges status + diagnostics
- `leads_by_ad(client, start_date=None, end_date=None)`
- `funnel(client, start_date=None, end_date=None)`
- `attribution_overview()`

- [ ] **Step 1: Write the failing tests:** one happy path per tool, plus `test_attribution_scoped_to_user`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): attribution tools`.

### Task 18: Slack report tools

**Files:**
- Create: `ai/mcp/slack_reports_mcp.py`
- Test: `tests/test_slack_reports_mcp.py`

**Tools:**
- `list_slack_reports()` (READ)
- `create_slack_report(...)`, `update_slack_report(report_id, ...)` and `delete_slack_report(report_id)` (WRITE): the same fields and validation as the `routers/slack_reports.py` bodies, reusing `services/slack_reports`.
- `send_test_slack_report(report_id, confirm_code=None)` (LIVE): the preview is the rendered report text and the channel name.
- If Slack isn't connected, the error says: "Connect Slack in Birdy first (Settings → Integrations)."

- [ ] **Step 1: Write the failing tests:** `test_create_validates_like_router`, `test_send_test_needs_confirmation`, `test_no_slack_error`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): slack report tools`.

### Task 19: Data refresh tools

**Files:**
- Create: `ai/mcp/refresh_mcp.py`
- Test: `tests/test_refresh_mcp.py`

**Tools:**
- `refresh_client_data(client, integration: Literal["meta","ghl","all"]="all")` (WRITE) starts `_run_refresh` for that client the way `refresh_integration_data` (`routers/client_groups.py:2900`) does, and returns `{started: [...], check_with: "refresh_status"}`.
- `refresh_status(client)` (READ) wraps `get_refresh_status`.
- No all-clients refresh. The global cycle isn't per-user.

- [ ] **Step 1: Write the failing tests:** `test_refresh_starts_only_requested_integration`, `test_refresh_other_users_client_not_found`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Move the refresh-start logic out of the route into `services/` if the route holds it inline.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): data refresh tools`.

### Task 20: MCP prompts

**Files:**
- Create: `ai/mcp/prompts_mcp.py`
- Test: `tests/test_prompts_mcp.py`

**Prompts (`@mcp.prompt`, exact text):**
- `weekly_client_review()`: "Use Birdy's check_my_accounts for the last 7 days. For each client that needs attention, explain what changed and why using get_client, compare_periods and the agent's cards, then give me a short action list."
- `why_did_cpl_jump(client: str)`: "Using Birdy, find out why CPL changed for {client}. Read get_client notes first, compare this week to last with compare_periods, break it down by campaign and ad, and check get_agent_cards for related findings. End with the likely cause and what to do."
- `prep_client_call(client: str)`: "Prep me for a call with {client} using Birdy: last 30 days results vs target, best and worst ads, lead quality from get_unified_lead_stats, open agent cards, and recent notes. Format as a one-page brief."
- `agent_week()`: "Using Birdy, summarise what my media buying agent did this week: runs, applied and dismissed cards, outcomes, and anything waiting for my decision."
- `daily_check_in()`: "Use Birdy's check_my_accounts and tell me only what needs my attention today, most urgent first. If nothing does, say so in one line."

- [ ] **Step 1: Write the failing test:** `test_prompts_registered_and_render_client_name`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): ready-made prompts`.

---

## Phase 4 — Watching the data

### Task 21: check_my_accounts

**Files:**
- Create: `services/account_check.py`, `ai/mcp/watch_mcp.py`
- Test: `tests/test_account_check.py`

**Interfaces:**
- `async def build_account_check(db, mongo_client, user_id: str, days: int = 7) -> dict` returns `{period: {start, end, prior_start, prior_end}, clients: [ClientCheck], summary: {high, medium, ok}}`.
- `ClientCheck = {client, spend, leads, cpl, target_cpl, prior: {spend, leads, cpl}, change_pct: {spend, leads, cpl}, alerts_fired: int, open_agent_cards: int, attention: "high"|"medium"|"ok", reasons: [str]}`
- Numbers come from the same code as `compare_periods` (`ai/mcp/compare_mcp.py`). Do not recompute metrics.
- `attention` rules (exact):
  - **high** if any of these hold:
    - `target_cpl` is set and `cpl > target_cpl * 1.2`
    - `alerts_fired >= 1`
    - prior spend > 0 and spend == 0
  - **medium** if any of these hold:
    - CPL change ≥ +25%
    - leads change ≤ −25% (with prior leads ≥ 5)
    - `open_agent_cards > 0`
  - **ok** otherwise.
  - Each rule that fires adds a plain-English reason, e.g. `"CPL $52 is 30% over the $40 target"`.
- Sort by attention (high, medium, ok), then spend descending.
- No LLM call. The connected agent writes the narrative.
- Tool: `check_my_accounts(days: int = 7)` (READ), 1 ≤ days ≤ 90.

- [ ] **Step 1: Write the failing tests:** one per rule (boundary at exactly 1.2× → not high; 1.21× → high), `test_leads_drop_ignored_below_5_prior_leads`, `test_sort_order`, `test_client_with_no_target_never_high_on_cpl`, `test_scoped_to_user`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): check_my_accounts`.

### Task 22: Fired-alert tools + channels on create_alert

**Files:**
- Modify: `ai/mcp/watch_mcp.py`, `ai/mcp/alert_mcp.py`
- Test: `tests/test_watch_mcp.py`

**Tools:**
- `get_fired_alerts(days: int = 7)` (READ): `alert_notifications` for the user since then, joined with the alert's name and condition display, newest first.
- `explain_alert(alert_id)` (READ): the alert, `format_condition_display`, `last_eval_result` (including `per_client_results`), the last 10 notifications, and a fresh `evaluate_alert` result labelled `"now"`.
- `create_alert` / `update_alert` gain `notification_channels: list[Literal["in_app","slack","email"]] | None`. The default stays `["in_app"]`.

- [ ] **Step 1: Write the failing tests:** `test_fired_alerts_window_and_order`, `test_explain_alert_other_user_not_found`, `test_create_alert_with_email_channel`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): fired-alert tools`.

### Task 23: Alert emails + "Ask Claude about this" links

**Files:**
- Create: `services/email_service.py`, `services/ask_claude.py`
- Modify:
  - `jobs/alert_jobs.py`: send the email on the transition into triggered only, the same condition `report_events.alert_fired` uses.
  - `services/report_events.py` and the Slack block renderer for the `alert_fired` event: add an "Ask Claude about this" link button.
- Test: `tests/test_alert_email.py`

**Interfaces:**
- `async def send_email(to: str, subject: str, html: str, text: str) -> bool`: an httpx POST to `https://api.resend.com/emails` with `RESEND_API_KEY`. It returns False and logs when the key is missing or on any error, and never raises into the job.
- `def ask_claude_url(prompt: str) -> str` → `"https://claude.ai/new?q=" + urllib.parse.quote(prompt)`
- `def alert_prompt(alert: dict) -> str` → `f'Using Birdy, explain my alert "{alert["name"]}" (alert id {alert["id"]}) and tell me what to do about it.'`
- Email:
  - Subject: `f"Birdy alert: {alert['name']}"`
  - Body: the alert message, the clients involved, a "View in Birdy" link (`FRONTEND_URL/alerts`), and an "Ask Claude about this" link.
  - Sent to the user's account email.

- [ ] **Step 1: Write the failing tests:**
  - `test_email_sent_once_when_alert_enters_triggered` (Review Focus 1): run the job twice with the condition still true, and assert one send.
  - `test_no_email_without_email_channel`
  - `test_missing_api_key_does_not_raise`
  - `test_ask_claude_url_encodes_prompt`
  - `test_slack_alert_block_has_ask_claude_link`
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(alerts): email delivery and Ask Claude links`.

### Task 24: Email toggle in the alert form (frontend)

**Files (birdy-beta-mcp):**
- Modify: `src/app/alerts/page.jsx` and `src/lib/alert-helpers.js`. The default `notification_channels` stays `["in_app"]`; an "Also email me" switch adds `"email"`.
- Test: `src/lib/__tests__/alert-helpers.test.js` (extend or create)

- [ ] **Step 1: Write the failing test:** `email switch adds email channel and keeps in_app`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement with the existing `Switch`, following the alert-form mock from Task 0.
- [ ] **Step 4:** Run vitest. Then build, crawl `/alerts` and take a screenshot.
- [ ] **Step 5:** Commit `feat(alerts): email notification option`.

### Task 25: v1 verification and docs

**Files:**
- Create: `docs/mcp.md`: setup per agent, the tool list by family, scopes, and the two-step flow.
- Modify: `README.md`: one link to `docs/mcp.md`.

- [ ] **Step 1:** In the backend, run `pytest -q`. Expected: all pass.
- [ ] **Step 2:** In the frontend, run `npx vitest run` and `npx next build`. Expected: pass.
- [ ] **Step 3: Live check with Claude Code against local.**
  1. Start `uvicorn main:app --port 8000` with `API_PUBLIC_URL=http://localhost:8000` and `FRONTEND_URL=http://localhost:3005`.
  2. Run `claude mcp add --transport http birdy-local http://localhost:8000/mcp`.
  3. Complete the browser consent as a throwaway test user.
  4. Call `check_my_accounts`, `get_agent_overview`, and `apply_card` (preview only).

  Expected: each works, and the preview changes nothing. Clean up the test user afterwards (see the local verify memory).
- [ ] **Step 4:** Crawl and screenshot `/settings?tab=ai`, `/connect/authorize`, and `/alerts`, then run a code audit against the chosen mocks.
- [ ] **Step 5:** Commit `docs(mcp): connector guide`.

---

## Phase 5 — v2

### Task 26: Reply to a lead

**Files:**
- Modify: `ai/mcp/leads_mcp.py`
- Test: `tests/test_leads_mcp.py`

**Tool:** `send_lead_message(contact_id, channel: Literal["sms","email"], body: str, subject: str | None = None, confirm_code=None)` (LIVE).
- It calls `services.ghl_messages.send_lead_message` with the same arguments and checks as `POST /api/leads/contact/{id}/messages`.
- The preview shows the recipient name, the masked phone or email, the channel, and the exact body.
- `MessagingUnconfirmed` / `MessagingFailed` become error dicts.
- `body` must not be empty, and is limited to 1600 characters for SMS.

- [ ] **Step 1: Write the failing tests:** `test_send_needs_confirmation_and_sends_exact_body`, `test_changed_body_invalidates_code`, `test_empty_body_rejected`, `test_read_scope_cannot_send`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): reply to leads`.

### Task 27: Report designer tools

**Files:**
- Create: `ai/mcp/report_designer_mcp.py`
- Test: `tests/test_report_designer_mcp.py`

**Tools:**
- `list_report_clients()`, `list_report_metrics()` and `get_report_draft()` (READ)
- `propose_report(...)` (WRITE): it saves the draft through the same path as `PUT /api/report-designer/draft`
- `preview_report()` (READ) → the logic behind `POST /api/report-designer/preview`

Each delegates to `ai/tools/report_designer_tools.py`. The draft is the same one the owner sees in Birdy's report designer.

- [ ] **Step 1: Write the failing tests:** one delegation test per tool, plus `test_draft_visible_in_router_state`.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(mcp): report designer tools`.
