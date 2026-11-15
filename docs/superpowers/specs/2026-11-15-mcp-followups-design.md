# MCP connector follow-ups: activity log, credits, tracking, cleanup

Date: 2026-11-15 · Branch: `feat/birdy-mcp` (backend `birdy-backend-mcp`, frontend `birdy-beta-mcp`), on top of the finished connector work.

## Why

Once an assistant can change ads and message leads for an owner, the owner needs to see what it did. They also need to pay for what it costs: the owner wants **every** connector request to use Birdy credits. Before this, the connector was free per call, which reverses the earlier "included, no credits" decision. The rest is housekeeping before merge: usage tracking, the deferred review minors, the Settings tabs on mobile, and docs for v2.

## Goals

1. **Activity log:** a clickable log of everything connected apps did, with full details per entry (layout A, picked from `mocks/mcp-activity/index.html`).
2. **Credits for every request:** priced by the data each request returns, plus an extra fee for confirmed live changes.
3. **Server-side PostHog events** for connector use.
4. **Deferred minors** from the connector reviews, fixed or closed with a reason.
5. **Settings tab bar on mobile:** scrolls sideways instead of overflowing.
6. **Docs:** the developer guide (`docs/mcp.md`) and the user guide cover v2 and credits.

## Non-goals

- Directory listings and an in-app install prompt (item 6 of the earlier list). The owner dropped these.
- Changing how Ask Birdy (the in-app chat) is charged. Internal calls keep their current metering and are not logged here.
- Retries for alert emails, and team-member connections.

## Anti-goals

- **Logs must not leak secrets:** a log entry must never store OAuth tokens, confirm codes or a lead's full phone number or email.
- **Charges must be predictable:** a user must never be charged for a request that was refused.
- **Logging and charging must not break the connector:** a failure in either one must never fail the user's request.

## Decisions (owner)

- **Credits:** every external MCP request costs credits.
- **Pricing:** by the amount of data returned, plus extra credits for confirmed live changes.
- **Activity log layout:** A. A table under "Your connections" in **Settings → Connect to AI**, with filters. Clicking a row opens a side panel with the details; on mobile the panel goes full screen.

## Approaches considered

**Pricing.** There were three options:
- A flat price per call.
- A price by type (look up / change / live).
- A price by work. **Chosen.**

For "by work" there were two ways to measure:
- The size of the response. **Chosen**, because it covers all 119 tools automatically and tracks our cost.
- A formula per tool. Rejected: 119 formulas to keep.

**Log layout.** There were three options:
- A table with a side panel. **Chosen.**
- A day feed that expands in place.
- A table with a separate detail page.

## Design

### Where it hooks in

`ai/mcp/access.py` already has middleware, `on_call_tool`, that every external tool call passes through. It is the single place to measure, charge, log and track. Internal calls (`type: "access"`, Ask Birdy) skip all of it, as they already skip the rate limit and the subscription gate.

### Credits

- **Charge formula:**
  - `base_fee` + `per_10k_chars` × (response characters ÷ 10,000), plus `live_fee` when a LIVE tool actually executes after a valid confirm code.
  - Defaults: `base_fee` 0.2, `per_10k_chars` 1.0, `live_fee` 1.0.
  - Round to 2 decimals, then cap at `max_per_call` (default 10; owner decision).
  - The admin markup does not apply (owner decision).
- **Response size:** the length of the tool result as serialised to the client.
- **Preview calls:** the first step of a LIVE tool is charged as a normal request, with no live fee.
- **Refused calls are free:**
  - view-only scope;
  - a bad, expired or changed confirm code;
  - rate limit;
  - no subscription;
  - out of credits;
  - a tool that raises an error.
- **Rates are editable:** all three live in the existing admin credits settings (`get_credits_settings` / `set_credits_settings`), so prices change without a deploy. The admin credits UI gets three inputs, next to the existing markup.
- **Charging:**
  - Charges go through the existing `_apply_charge` with a ledger row: `source: "mcp"`, `feature: "mcp_request"`, the tool name and the grant id.
  - They appear in the user's existing credit usage history.
- **AI features:** features that already meter AI use (lead summary, call analysis, lesson shaping) keep doing so, on top of the request charge.
- **Out of credits:**
  - When enforcement is on (the existing `enforce` toggle), an external call is refused before it runs.
  - The message says the account is out of Birdy credits and can top up in **Settings → Billing**.
  - When enforcement is off, the call is metered but not blocked, matching the app.
- **Fail-safe:** if charging fails, the request still succeeds and the failure is logged.

### Activity log

**Stored data**

The log lives in a new collection, `mcp_activity`, with one document per external tool call, including refused ones. Fields:

- `id`, `user_id`, `grant_id` (or manual token id), `app_name`, `access` (read or write);
- `tool`, `kind`: one of `lookup`, `change`, `preview`, `live`, `refused`;
- `refusal_reason`, set when the call was refused;
- `client_group_ids`, plus client names at write time;
- `summary`: one plain sentence, such as "Paused 2 ads for Acme Dental";
- `args`, after redaction;
- `result_status`: ok or error;
- `result_excerpt`: what Birdy did, at most 2,000 characters;
- `confirmed_after`: the preview's activity id, for executes;
- `credits`: `{request, data, live, total}`;
- `response_chars`, `duration_ms`, `created_at`.

**Redaction**

- Drop `confirm_code`.
- Mask phone numbers and emails the same way the lead preview does ("•••• 4821", "j•••@acme.com").
- Keep message bodies, because the owner should see exactly what was sent to a lead.

**Summaries**

- Build the sentence from a per-tool label table, a verb phrase per tool, plus client names and counts from the result where available.
- A test asserts every registered tool has a label, so new tools can't ship without one.

**Retention and indexes**

- A TTL index expires entries after 180 days.
- A second index on `(user_id, created_at desc)`.

**API**

- `GET /api/mcp/activity` takes:
  - filters: `app` (grant id), `kind`, `client_group_id`, `from`/`to`;
  - a `cursor` for paging, 50 per page;
  - totals for the period: request count and credits.
- `GET /api/mcp/activity/{id}` returns one entry, owner only. It returns 404 for anyone else.
- `GET /api/mcp/activity.csv` exports the filtered list.

**UI** (`ConnectAI.jsx`, layout A, pd-* kit)

- **The table** sits below "Your connections", with columns: when, app logo, what happened, kind (coloured dot), credits.
- **Filters:** app, kind, client, period (7, 30 or 90 days), and an "Export CSV" button.
- **The header line:** "N requests this period · X credits".
- **The side panel** opens when a row is clicked. It shows:
  - the title and the time;
  - the app and its access level;
  - the tool and the client;
  - the result chip, and "Confirmed after the preview at HH:MM" for executes;
  - what was asked;
  - what Birdy did;
  - the credit breakdown.
- **Links from the panel:**
  - **Undo or open in Birdy:** when the entry concerns an agent card, a suggestion or a creative test, the panel links to that page in Birdy. No undo runs from the log.
  - **Preview pairs:** a preview links to its execute, and an execute links back to its preview.
- **Mobile:** the panel opens full screen with a back button.
- **Empty state:** "Nothing yet. Requests from your connected apps will show up here."

### PostHog (server-side)

Use the PostHog HTTP capture API with the project key from a new env var, `POSTHOG_API_KEY` (plus `POSTHOG_HOST`). There is no new heavy dependency: `httpx` is fine. Events are fire-and-forget and skipped when no key is set. `distinct_id` = Birdy `user_id`, matching the frontend.

| Event | When | Properties |
|---|---|---|
| `mcp_connected` | OAuth grant approved, or manual token created | app_name, access, method (oauth or token) |
| `mcp_disconnected` | grant revoked | app_name |
| `mcp_tool_called` | each external call | tool, kind, result_status, credits_total, response_chars, duration_ms, app_name |
| `mcp_live_confirmed` | a LIVE execute succeeds | tool, app_name |
| `mcp_refused` | a refused call | tool, reason |

No message text, arguments or client names go to PostHog.

### Deferred minors

Work through every `minor (deferred)` line in the old ledger, plus `task-26-review.md`, `task-27-review.md` and `final-review.md`, saved at `docs/superpowers/notes/2026-11-15-mcp-deferred-minors.md`. Fix each one unless it is a deliberate design choice or pre-existing outside this branch. Record each one as fixed or closed, with a one-line reason. Two settled examples:

- **Stays:** "Ask Birdy can't set email at creation" stays.
- **Fixed by the activity log:** "Lead messages not attributed" is fixed through the activity log.

### Settings tabs on mobile

The Settings page tab bar (`src/app/settings/page.jsx`) scrolls sideways on narrow screens, with no visible scrollbar and the selected tab scrolled into view. At 390px every tab must be reachable and the page must not scroll sideways.

### Docs

- **`docs/mcp.md`:**
  - regenerate the tool table, which now has 119 tools;
  - add v2 (reply to leads, report designer);
  - replace "no credits" with the pricing and the admin rates;
  - document the activity log and its API;
  - add the PostHog env vars.
- **User guide** (the Claude Doc "Birdy for Claude, ChatGPT and Cursor"):
  - update the credits FAQ and the overview line about credits;
  - add a section on the activity log.
- **Changelog:** a `src/lib/changelog.js` entry is out of scope, because no release has been cut yet.

## Constraints

- These rules from the connector work still apply:
  - `ai/mcp` never imports routers;
  - `user_id` comes only from the verified token;
  - ownership is matched by id;
  - existing HTTP bodies stay unchanged.
- Logging, charging and PostHog run after the tool returns, and never change its result.
- Their total added latency is under 50 ms per call. The writes may run concurrently.
- UI uses the pd-* kit. It needs screenshots at desktop and 390px before it is called done.

## Builder-check answers (controller calls unless marked)

1. **Outcome classification.**
   - `two_step` and the gates report their outcome through a contextvar, `ai.mcp.access.call_outcome`. The middleware reads it after `call_next`, so it never parses 119 result shapes. Values:
     - `preview`
     - `live_executed`
     - `bad_code`
     - `state_changed`
     - `refused:<reason>`
   - When nothing is set, the kind comes from the tool's tag: READ → `lookup`, WRITE → `change`.
2. **Error results are free.**
   - A result that is a dict whose only failure signal is a top-level `error` key counts as an error.
   - An `{"ok": False, ...}` dict counts as an error too.
   - Error results cost nothing and get `result_status: error`.
   - This rule lives in one helper in access.py, with tests over the known error shapes.
3. **Confirm codes in previews.**
   - Redaction runs on both `args` and `result_excerpt`.
   - `confirm_code` is removed, at any depth.
   - Masking for phones and emails also applies to the excerpt.
   - Billing still counts the full serialised result, which includes the code. That's about 20 characters, so the effect is negligible.
4. **Measuring response characters.**
   - Count the JSON-serialised `structured_content` when it is present. Otherwise count the text of the `content` blocks. Never count both.
   - Images and binary count 0. None of the current tools return any.
   - Cap per call (owner): one request never costs more than `max_per_call`, default 10 credits, admin-editable.
5. **Credit check and concurrency.**
   - The pre-check is "available balance > 0", using the existing `_load_and_sync` and `_available`.
   - A small overdraft from parallel calls is accepted, because the next call is blocked.
   - `credits.py` and `credits_middleware.py` are top-level modules, not routers, so `ai/mcp` may import them.
6. **Whose balance pays.** The token's `sub`, which is always the account owner: only owners can connect. There are no team wallets.
7. **What refused calls log and track.**
   - Refusals for view-only scope, no subscription, out of credits, bad code and changed state are logged. Each gets one row and one `mcp_refused` event.
   - Rate-limit refusals are not logged per call. The middleware upserts one row per user per minute, with `kind: refused`, reason `rate_limit` and a `count`.
8. **Writes on serverless.**
   - The activity insert and the credit charge are awaited, together, under a 300 ms `asyncio.wait_for`. On timeout, a warning is logged and the result is still returned.
   - PostHog is fire-and-forget with a 2 s client timeout. Losing the occasional event is accepted.
   - The latency target becomes "under 50 ms typical, 300 ms worst case".
9. **Summaries and client names.**
   - Client ids come from the args, through one resolver that knows the arg names in use: `client_group_id`, `client`, `group_id`, `card_id` (card to client), `contact_id` (lead to client) and `alert_id`.
   - Summaries come from the per-tool label table, plus the resolved client name.
   - An empty client is fine. The client filter shows only entries that have one.
10. **App identity for filters.**
    - `grant_id` is the OAuth grant id, or the manual token's id. Each entry stores `app_name` and `app_kind`, the logo key such as claude or chatgpt.
    - The app filter lists the distinct apps found in the user's log, including revoked ones, which get a "(disconnected)" suffix.
11. **Links back to Birdy.**
    - The per-tool label table also gives an optional `link` template, filled from the args, such as `/birdyhub/agent?card={card_id}`.
    - The stored field is `link`, a relative URL. When it is absent, the panel shows no link.
12. **Preview pairs.**
    - The confirm-code document stores the preview's `activity_id`. The execute reads it before burning the code and writes `confirmed_after`.
    - The UI finds the reverse link with `GET /api/mcp/activity?confirmed_after=<id>`.
13. **CSV and period totals.**
    - CSV columns: time, app, kind, summary, client, result, credits. No args. Capped at 10,000 rows.
    - Totals are computed on the server for the whole filtered period and returned with page 1.
14. **Indexes and retention.**
    - Follow `create_confirm_code_indexes`: add `create_mcp_activity_indexes` to the same startup hook.
    - `created_at` is a BSON datetime. A 180-day TTL applies to every entry.
15. **Admin rates.**
    - The admin UI is in `birdy-beta-mcp/src/app/admin/credits/page.jsx`.
    - `set_credits_settings` gains `mcp_base_fee`, `mcp_per_10k_chars`, `mcp_live_fee` and `mcp_max_per_call` kwargs. Each is validated as a number ≥ 0 and ≤ 100.
    - Markup (owner): the admin markup is **not** applied to MCP rates. The numbers set are exactly what users pay.
16. **PostHog setup.**
    - The frontend has no PostHog calls today, so there is no identity to match.
    - Events use the user's Birdy `user_id` as `distinct_id`.
    - `POSTHOG_API_KEY` is the project key of the owner's PostHog project. `POSTHOG_HOST` defaults to `https://us.i.posthog.com`.
17. **Deferred minors.**
    - The full list is saved at `docs/superpowers/notes/2026-11-15-mcp-deferred-minors.md` (~60 items).
    - "Pre-existing" means the code also exists at merge-base `b0a3511`.

## Left to the builder

- The exact label wording per tool.
- Whether CSV export streams or builds in memory, since the volume is small.
- The admin UI placement of the three rate inputs, within the existing credits settings panel.
