# Birdy as an MCP connector — design

Date: 2026-11-15 · Status: draft for approval (builder-check answers folded in)

## Intent

Any Birdy account owner can add Birdy to Claude (claude.ai, Claude Desktop, Claude Code) or another MCP-capable agent (ChatGPT, Cursor, …) and, from that agent, do what they can do in Birdy:

1. **Use the tools** — ask about their clients' Meta / GHL / lead data, compare periods, custom metrics, alerts, creatives, calls, reports.
2. **Watch their data** — both *ask* ("how did my clients do this week?") and *get alerted* when something is off.
3. **Work the media buying agent** — see what it did and found, accept/reject its proposals, tune its playbook, targets, and memory.

Why: owners increasingly live in their AI assistant; Birdy should be reachable there instead of only in its own app.

## Current state (origin/main, b0a3511)

- `ai/mcp/server.py`: one FastMCP server mounted at `/mcp` (stateless HTTP, Vercel). Ask Birdy's orchestrator already calls tools through it (`ai/mcp_client.py`).
- ~30 tools are on MCP: Meta cached + live, GHL, unified leads, alerts, custom metrics, compare, summary, groups.
- Auth: HS256 JWT bearer. `type:"mcp"` long-lived tokens (create/list/revoke via `routers/mcp_tokens.py`, revocable by `jti`). `user_id` always comes from the verified token, never from arguments.
- No frontend for MCP at all. No OAuth. Login is email + password (`/api/login`). No email-sending provider in the backend.
- Many Ask Birdy tools are **not** on MCP: daily metrics, multi-window, call center / recordings / analysis, report designer, creatives, attribution, dashboards, Hot Prospector, page views, and all media buying agent capabilities (agent operations are REST routes in `routers/agents.py` and setup tools in `ai/tools/agent_tools.py`).
- Alerts: evaluated by `jobs/alert_jobs.py`; delivered in-app; a fired alert can trigger a Slack report (`slack_reports` event triggers).

## Goals

- **One-link install**: owner pastes `https://<api>/mcp` into "Add custom connector", is sent to a Birdy login/consent page, and is connected. No token copying.
- **Parity**: every read and write an owner can do in Birdy's chat and agent screens is available as an MCP tool, scoped to their account.
- **Alerts reach the owner outside Claude, with a path into Claude**; plus an optional daily check-in inside Claude.
- **A Connect page in Birdy** explaining how to add it to each agent, managing connections, and showing the check-in prompt.

## Non-goals

- Birdy pushing messages into a Claude chat (MCP/agents don't support it).
- Admin / cross-account tools over MCP (admin console stays app-only).
- Billing, onboarding, OAuth-integration setup (connecting Meta/GHL accounts) over MCP — done in the app.
- Rich UI rendering (`render_ui_tools`, dashkit visuals) — MCP returns data; the agent renders it.
- Publishing to Anthropic's/OpenAI's connector directories (later, separate).

## Anti-goals (fail even if it works)

- Any path where one account can read or change another account's data.
- An agent able to act on live ad accounts without the same confirmations the app requires.
- Owners having to edit JSON config files to get started.
- Two divergent tool implementations (MCP vs Ask Birdy) — the orchestrator keeps using the same MCP tools.

## Approaches considered (alerting)

| | What | Trade-off |
|---|---|---|
| A | Birdy watches and alerts via Slack/app/email; each alert links into Claude; MCP has tools to read/explain fired alerts and create alerts by chat | Reliable, every agent; nudge arrives outside Claude |
| B | Claude scheduled task runs a daily "check my accounts" prompt against the MCP | Lives in Claude; only where scheduled tasks exist; owner must set up; misses urgent things between runs |
| **C (chosen)** | **A as the backbone, B as an add-on** (one `check_my_accounts` tool + copy-paste prompt on the Connect page) | Covers both; B is small |

Picked by the owner in conversation: C, A first.

## Design

### 1. OAuth for one-link install

- Add OAuth 2.1 authorization-server support so the MCP endpoint advertises `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`, supports dynamic client registration (DCR) and PKCE — what claude.ai / ChatGPT custom connectors require.
- Use FastMCP's built-in OAuth server support (fastmcp 2.14.7 is already pinned) rather than hand-rolling, backed by Mongo storage for clients, codes, and refresh tokens (stateless Vercel → no in-memory state).
- Authorize step: redirect to a Birdy frontend page `/connect/authorize` — if not logged in, normal login first; then a consent screen ("Claude wants to access your Birdy account: read data, manage alerts, manage your media buying agent") → Allow / Deny.
- Issued access tokens are the existing `type:"mcp"` JWT shape (short-lived, e.g. 1h) with refresh tokens stored and revocable; so `current_user_id()` and every existing tool work unchanged.
- **Two access levels (decided):** the consent screen offers "View my data" (read-only scope) or "View and make changes" (full scope). The scope is stored on the grant and carried in the token; every tool is tagged read or write, and write tools reject read-only tokens with a clear message. Manual tokens pick a level when created.
- Each OAuth grant shows up as a "connection" alongside manually minted tokens, revocable from the Connect page.
- Manual token flow stays for Claude Code / Desktop / developers.

### 2. Tool parity

- Port every Ask Birdy tool not yet on MCP into `ai/mcp/*_mcp.py` modules (same pattern as existing ones; add names to `MCP_TOOL_NAMES`), so the orchestrator and external agents share them.
- New `agent_mcp.py` exposing the media buying agents, wrapping the logic behind `routers/agent_stock.py`, `routers/agents.py`, and `routers/dashboard.py` (not duplicating it).
- **Which agent (decided):** tools act on the owner's **Birdy Media Buying Agent** (`stock_media_buying`) by default. Every agent tool takes an optional `agent` (name or id) so the owner can point at a custom agent instead, and a `set_default_agent` tool lets them switch the default for their connection. `list_agents` shows both kinds.
  - Stock agent: settings, runs (latest / by id / by month), cards, apply / apply-all / dismiss / edit / undo cards, file a card line, lessons, outcomes, Slack templates (get/put/delete/preview), weekly post, preview.
  - Custom agents: playbook/rules, client targets/overrides, activity, work/history, claims, impact, confidence, proposals accept/reject, memory add/delete, exceptions, schedule, Slack channel, activate/deactivate, dry-run (start + poll job), undo auto-action, graduate rule.
  - Dashboard suggestions: apply / deny / undo.
  - Setup-wizard–only tools (`draft_playbook`, `show_block`, practice cases) stay in-app for v1; owners set up an agent in Birdy, then operate it from Claude.
- Tool descriptions written for an outside agent (no reliance on Birdy's system prompt), and MCP tool annotations set (`readOnlyHint`, `destructiveHint`).
- **Live changes need a server-enforced two-step (decided).** Any tool that changes live ads or campaigns (card apply / apply-all / undo, dashboard suggestion apply / undo, activate agent, undo auto-action, weekly post to Slack) works like this:
  1. Without a `confirm_code`, it changes nothing and returns a preview: what will change, per client, with expected effect (e.g. "pause 3 ads in Acme, ~$120/day"), plus a single-use `confirm_code` bound to user + tool + exact arguments, valid ~10 minutes.
  2. Called again with that code and identical arguments, it executes and returns before/after.
  The tool description tells the agent to show the preview and get the owner's yes before step 2. Same idea as the `confirmed` gate in `ai/tools/call_analysis_tools.py`, but enforced by a server-issued code rather than a boolean. Annotations are still set, as a second layer.

### 3. Watching the data

- **Ask**: a `check_my_accounts` tool — one call returns, per client, spend / leads / CPL vs target and vs prior period, fired alerts, and agent findings, ranked by what needs attention. Also the backbone of the daily check-in.
- **Alerts (A)**:
  - MCP tools: `get_fired_alerts` (recent firings with context), `explain_alert` (the numbers behind a firing). `create_alert` / `update_alert` already exist.
  - Delivery: keep in-app + Slack; add **email** delivery as a `notification_channels` option (needs an email provider — builder picks, e.g. Resend).
  - Each Slack/email alert includes an "Ask Claude about this" link: `https://claude.ai/new?q=<prefilled prompt naming the alert>` (works when the connector is installed; harmless otherwise).
- **Daily check-in (B)**: Connect page shows a copy-paste prompt for Claude scheduled tasks: "Every weekday at 8am, use Birdy's check_my_accounts and tell me only what needs my attention."

### 4. Connect page (frontend, birdy-beta)

- Settings → "Connect to AI" page: the MCP URL with copy button; per-agent tabs (claude.ai, Claude Desktop, Claude Code, ChatGPT, Cursor) with steps; active connections list with revoke; "Create token" for manual setups; the check-in prompt.
- Plus `/connect/authorize` consent page.
- Per project rules: 2–3 HTML mock options of both screens presented before building; built with the pd-* kit; verified with crawl + screenshots.

### 5. Security

- `user_id` only from token; every tool scoped by it (existing invariant, test each new tool for cross-account access).
- OAuth: PKCE required, redirect URIs exact-match registered ones, consent required per client, refresh-token rotation, revocation checked per request (existing `jti` lookup).
- Rate limiting on `/mcp` per user.
- Admin impersonation sessions cannot authorize an OAuth client.

### 6. Billing (decided)

- MCP access is part of the Birdy subscription: an active subscription (`billing_middleware.require_active_subscription`) is required to authorize a connection and is checked on each MCP request; lapsed accounts get a clear "your Birdy subscription is inactive" error.
- No per-call or credit charges for MCP use on top of the subscription. *(My reading of "we charge them for using Birdy itself" — correct me if AI-heavy tools like call analysis should still consume credits.)*
- Ask Birdy's own internal calls through `/mcp` are unaffected (they keep their existing credit metering).

## Phases

1. OAuth + consent page + Connect page (install works end to end with existing tools).
2. Media buying agent tools (stock agent first, then custom agents, then dashboard suggestions), with the two-step confirmation.
3. Remaining tool parity.
4. Watching: `check_my_accounts`, fired-alert tools, email delivery, "Ask Claude" links, check-in prompt.

## Left to the builder

- Exact OAuth wiring within FastMCP and Mongo storage shape.
- Email provider choice.
- Tool naming/grouping within the agreed scope; which agent read endpoints merge into one tool.
- Token lifetimes within the stated bounds.
