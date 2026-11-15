# MCP Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Charge Birdy credits for every external MCP request and give owners a clickable activity log. Add server-side PostHog events. Clear the deferred review minors, fix the Settings tabs on mobile, and bring the docs up to v2.

**Architecture:** `BirdyAccessMiddleware.on_call_tool` (ai/mcp/access.py) is the one hook for every external call.
- The gates and `two_step` report what happened through a contextvar.
- After `call_next`, the middleware measures the result and charges credits. It writes one `mcp_activity` row and fires PostHog.
- A REST router serves the log to a new panel in `ConnectAI.jsx`.

**Tech Stack:** FastAPI, FastMCP 2.14.7, Motor/Mongo, httpx (PostHog capture API); Next.js + pd-* kit, vitest, Playwright for screenshots.

**Spec:** `docs/superpowers/specs/2026-11-15-mcp-followups-design.md`. Read its "Builder-check answers" section: it settles each open detail.

**Repos:**
- backend `/Users/abdelrahman/Desktop/Birdy/birdy-backend-mcp`
- frontend `/Users/abdelrahman/Desktop/Birdy/birdy-beta-mcp`

Both are on branch `feat/birdy-mcp`.

## Global Constraints

- Only external calls are charged, logged and tracked. A call is external when the token's `type` != `"access"`. Internal Ask Birdy calls are untouched.
- Charge formula, from the defaults in credits settings:
  - `min(max_per_call, round(base_fee + per_10k_chars * chars / 10000 + (live_fee if live executed), 2))`;
  - defaults: base_fee 0.2, per_10k_chars 1.0, live_fee 1.0, max_per_call 10;
  - the markup is NOT applied.
- These calls are free: refused (view-only, no subscription, out of credits, rate limit, bad code, state changed), raised `ToolError`, and error results.
  - An error result is a dict with a top-level `error` key, or one with `ok` set to False.
- Response chars = `len(json.dumps(structured_content))` when structured content is present. Otherwise, the summed text of the `content` blocks. Never both.
- Out-of-credits gate: block only when credits `enforce` is on and the available balance is ≤ 0. The message is exactly: `You're out of Birdy credits. Top up in Settings → Billing to keep using Birdy here.`
- The activity insert and the charge are awaited together under `asyncio.wait_for(..., 0.3)`. On timeout: log a warning and return the result. PostHog is fire-and-forget with a 2 s client timeout.
- Logs never store tokens or `confirm_code` (at any depth). Phones are masked as `•••• 4821`, emails as `j•••@acme.com`. Lead message bodies are kept.
- `mcp_activity` retention: TTL of 180 days on BSON `created_at`. Index `(user_id, created_at desc)`. Indexes are created in `core/indexes.py`, alongside `create_confirm_code_indexes`.
- Carried over from the connector rules:
  - `ai/mcp` never imports `routers`;
  - user_id comes only from the verified token;
  - ownership is matched by id;
  - existing HTTP bodies stay unchanged.
- Bookkeeping: if a task adds an MCP tool, update `tests/test_mcp_client.py` `external_only` and `tests/test_mcp_registration.py` `EXPECTED_TOOLS_BY_MODULE`. No task here is expected to add one.
- Python: `/Users/abdelrahman/Desktop/Birdy/birdy-backend/.venv/bin/python -m pytest -p no:cacheprovider`.
- Frontend UI uses the pd-* kit. Before a UI task is done, take screenshots at 1280px and 390px with `next start` on port 3005, Playwright with mocked API routes, and chromium at `/Users/abdelrahman/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell`. Build with `NEXT_PUBLIC_API_URL=http://localhost:8000`.

## Review Focus

1. **A burst of parallel calls while the balance is near zero.** Each call passes the "> 0" check and a small overdraft follows. The next call must be blocked, never a crash. Test in Task 2.
2. **A preview followed by a bad or replayed code.** The bad-code call is free and logged as `refused` with reason `bad_code`. A later valid execute still links `confirmed_after` to its preview. Tests in Tasks 1 and 3.
3. **A tool whose result contains a lead's phone or email, or a nested `confirm_code`.** None of these may appear unmasked in `args`, `result_excerpt` or the CSV. Test in Task 3.
4. **A revoked connection.** Its past entries still list, filter and show the app name with "(disconnected)". Test in Task 4.
5. **Mongo slow or down during logging.** The tool result still returns within about 300 ms extra, and the charge failure is logged, not raised. Test in Task 3.

---

### Task 1: Call outcome and result measuring

**Files:**
- Create: `ai/mcp/outcome.py`
- Modify: `ai/mcp/confirm.py` (`two_step`, `issue_code`); `ai/mcp/access.py` (set the outcome for gate refusals)
- Test: `tests/test_mcp_outcome.py`

**Interfaces:**
- Produces, in `ai/mcp/outcome.py`:
  - `call_outcome: ContextVar[dict | None]`;
  - `set_outcome(kind: str, **extra) -> None`, where kind is one of `preview`, `live_executed`, `bad_code`, `state_changed`, `refused`, and extra may carry `reason`, `activity_id` or `confirm_doc`;
  - `get_outcome() -> dict | None`;
  - `reset_outcome() -> Token`;
  - `is_error_result(value) -> bool`;
  - `response_chars(tool_result) -> int`.
- `two_step` sets `preview`, `bad_code`, `state_changed` or `live_executed`. On a successful execute, the outcome carries the burned doc's `activity_id`, if any.
- `issue_code(..., activity_id: str | None = None)` stores `activity_id` on the code doc.
- The access middleware calls `set_outcome("refused", reason=...)` before raising. The reasons are `view_only`, `no_subscription` and `rate_limit` (Task 2 adds `out_of_credits`).

- [ ] **Step 1: Write failing tests.**
  - `test_two_step_sets_preview` and `test_two_step_sets_live_executed`.
  - `test_bad_code_sets_bad_code` and `test_state_change_sets_state_changed`.
  - `test_is_error_result`:
    - `{"error": "x"}` → True; `{"ok": False}` → True;
    - `{"ok": True}` → False; `[]` → False; `{"errors": []}` → False.
  - `test_response_chars_prefers_structured`: a result with both structured content and content counts the structured JSON only.
  - `test_view_only_refusal_sets_outcome`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The middleware resets the contextvar at the start of every call.
- [ ] **Step 4: Run the new tests and `tests/test_mcp_confirm*.py` and `tests/test_mcp_access.py`.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): report call outcomes`.

### Task 2: Credits for every external request

**Files:**
- Modify: `credits.py`:
  - `_default_settings` and `get_credits_settings` gain `mcp_base_fee`, `mcp_per_10k_chars`, `mcp_live_fee`, `mcp_max_per_call`;
  - `set_credits_settings` gets the same four kwargs, each validated 0 ≤ x ≤ 100.
- Modify: the admin credits settings route that calls `set_credits_settings` (find it with `grep -rn set_credits_settings routers`), so it accepts and returns the four fields.
- Create: `services/mcp_billing.py`
- Modify: `ai/mcp/access.py` (pre-check gate)
- Test: `tests/test_mcp_billing.py`

**Interfaces:**
- Produces:
  - `mcp_charge_amount(settings: dict, chars: int, live: bool) -> float`;
  - `async charge_mcp_call(db, user_id: str, *, tool: str, grant_id: str | None, chars: int, live: bool) -> dict`, which returns `{request, data, live, total}`;
  - `async has_credits(db, user_id: str) -> bool`, true when enforcement is off or the available balance is > 0.
- The charge uses `credits._apply_charge` with ledger `{"source": "mcp", "feature": "mcp_request", "tool": tool, "grant_id": grant_id}`.

- [ ] **Step 1: Write failing tests.**
  - `test_amount_formula`:
    - chars=0, live=False → 0.2;
    - chars=15000 → 1.7;
    - chars=10000, live=True → 2.2;
    - chars=500000 → 10.0 (cap);
    - with markup 4.0 set, the amounts are unchanged.
  - `test_settings_roundtrip_and_validation`: -1 → ValueError; 101 → ValueError.
  - `test_out_of_credits_blocks_when_enforced`: the exact message from Global Constraints; the tool does not run; nothing is charged.
  - `test_not_blocked_when_enforce_off`.
  - `test_internal_call_not_charged`.
  - `test_parallel_calls_near_zero_then_blocked` (Review Focus 1).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The pre-check runs after the subscription gate. Out of credits → `set_outcome("refused", reason="out_of_credits")` and raise a `ToolError` with the message.
- [ ] **Step 4: Run the new tests and `tests/test_credits*.py`.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(credits): MCP request pricing`.

### Task 3: Activity recording

**Files:**
- Create:
  - `ai/mcp/activity_labels.py`: `LABELS: dict[str, dict]`, one entry per registered tool, `{"label": "Paused ads", "link": "/birdyhub/agent?card={card_id}" | None}`;
  - `services/mcp_activity.py`.
- Modify: `ai/mcp/access.py` (after `call_next`: classify, charge, record); `core/indexes.py`
- Test: `tests/test_mcp_activity.py`

**Interfaces:**
- Consumes:
  - Task 1's `get_outcome`, `is_error_result` and `response_chars`;
  - Task 2's `charge_mcp_call`.
- Produces in `services/mcp_activity.py`:
  - `redact(value) -> value` (masks phones and emails, drops `confirm_code` at any depth);
  - `async resolve_clients(db, user_id, args) -> list[{"id","name"}]`, which resolves `client_group_id`, `client`, `group_id`, `card_id`, `contact_id` and `alert_id`;
  - `summarize(tool, args, clients, outcome) -> str`;
  - `async record_call(db, *, user_id, grant_id, app_name, app_kind, access, tool, kind, refusal_reason, args, result, result_status, credits, chars, duration_ms, confirmed_after) -> str`, which returns the activity id;
  - `async record_rate_limited(db, user_id, grant_id, app_name)`, which upserts one row per user per minute with `count`;
  - `async create_mcp_activity_indexes(mongo_client)`.
- Document fields exactly as in the spec's "Activity log" section, plus `app_kind`, `link` and `count`.
- `kind` mapping:
  - outcome `preview` → `preview`;
  - `live_executed` → `live`;
  - `refused`, `bad_code` or `state_changed` → `refused`;
  - otherwise READ-tagged → `lookup`, WRITE-tagged → `change`.
- A preview's activity id goes onto its confirm code. Insert the row first, then `issue_code(..., activity_id=...)`; or update the code doc after insert. The implementer picks one and documents it.
- `grant_id` comes from the claim `grant_id` (OAuth) or `jti` (manual token).
- `app_name` comes from the claim `client_name`, or the manual token's name.
- `app_kind` is derived from `app_name`, using the same mapping as the frontend's `src/components/connect/AppLogo.jsx`: `claude`, `chatgpt`, `cursor`, `claude-code` or `other`.

- [ ] **Step 1: Write failing tests.**
  - `test_every_registered_tool_has_a_label`.
  - `test_lookup_logged_and_charged`.
  - `test_refused_view_only_logged_free`.
  - `test_bad_code_logged_free_then_valid_execute_links_preview` (Review Focus 2).
  - `test_redaction_masks_phone_email_and_confirm_code`, over args, a nested result and a preview result (Review Focus 3).
  - `test_rate_limited_collapses_to_one_row_per_minute`.
  - `test_slow_db_does_not_block_result`: patch the insert to sleep 2 s; the call returns in < 1 s and no exception surfaces (Review Focus 5).
  - `test_error_result_free_and_status_error`.
  - `test_internal_call_not_logged`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Write a label for every tool. Wording: past-tense verb phrases a business owner would use.
- [ ] **Step 4: Run the new tests and the full suite.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): activity log recording`.

### Task 4: Activity API

**Files:**
- Create: `routers/mcp_activity.py`, registered where `routers/mcp_tokens.py` is.
- Test: `tests/test_mcp_activity_api.py`

**Interfaces:**
- Consumes: the Task 3 collection.
- `GET /api/mcp/activity`:
  - query `app`, `kind`, `client_group_id`, `from`, `to` (ISO dates), `confirmed_after`, `cursor`, `limit` (≤50, default 50);
  - returns `{"items":[...], "next_cursor": str|null, "totals": {"requests": int, "credits": float} | null}`;
  - totals are present on the first page only and cover the whole filter;
  - list items carry `id, created_at, app_name, app_kind, kind, summary, clients, credits.total, result_status, revoked: bool`.
- `GET /api/mcp/activity/apps` → `[{"grant_id","app_name","app_kind","revoked"}]`, the distinct apps in the user's log.
- `GET /api/mcp/activity/{id}` → the full redacted entry. Another user's entry → 404.
- `GET /api/mcp/activity.csv`: the same filters. Columns `time, app, kind, summary, client, result, credits`. At most 10,000 rows. No args.

- [ ] **Step 1: Write failing tests.**
  - `test_list_filters_and_cursor`.
  - `test_totals_first_page_only`.
  - `test_detail_owner_only_404`.
  - `test_csv_columns_and_no_args`.
  - `test_revoked_app_still_listed` (Review Focus 4).
  - `test_confirmed_after_reverse_lookup`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Use `get_current_user` for auth, like `routers/mcp_tokens.py`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(mcp): activity API`.

### Task 5: PostHog server events

**Files:**
- Create: `services/posthog_events.py`
- Modify:
  - `core/config.py` (`POSTHOG_API_KEY`, `POSTHOG_HOST`, default `https://us.i.posthog.com`);
  - `ai/mcp/access.py`;
  - the OAuth approve path and manual-token create (`mcp_connected`);
  - `revoke_connection` (`mcp_disconnected`).
- Test: `tests/test_posthog_events.py`

**Interfaces:**
- Produces: `capture(user_id: str, event: str, properties: dict) -> None`. It is non-blocking: it schedules an httpx POST to `{POSTHOG_HOST}/i/v0/e/` with a 2 s timeout, and does nothing when no key is set.
- Events and properties exactly as in the spec's PostHog table.

- [ ] **Step 1: Write failing tests.**
  - `test_noop_without_key`.
  - `test_tool_called_properties_have_no_args_or_client_names`.
  - `test_connected_and_disconnected_fired`.
  - `test_live_confirmed_fired_once`.
  - `test_refused_reason`.
  - `test_posthog_failure_never_raises`.
  - Mock httpx; the network guard forbids real calls.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(analytics): MCP PostHog events`.

### Task 6: Activity panel in Settings (frontend)

**Files:**
- Create: `src/components/settings/AIActivity.jsx`, `src/components/settings/AIActivityPanel.jsx`
- Modify: `src/components/settings/ConnectAI.jsx` (render `<AIActivity />` below "Your connections")
- Test: `src/components/settings/__tests__/AIActivity.test.jsx`

**Interfaces:**
- Consumes: the Task 4 endpoints, through the app's existing `apiRequest` helper.
- Layout A from `/Users/abdelrahman/Desktop/Birdy/mocks/mcp-activity/index.html`:
  - a header line `N requests · X credits`;
  - filters: All apps (from `/apps`, with revoked apps suffixed "(disconnected)"), All kinds, All clients, and a period of 7/30/90 days (default 7);
  - an "Export CSV" link;
  - a table: When · app logo (`AppLogo`) · What happened · Kind (coloured dot: lookup grey, change/preview primary, live warning, refused danger) · Credits;
  - "Load more" when `next_cursor` is set.
- Row click opens a side panel showing:
  - the title and time · app · access;
  - Tool, Client, Result chip, and "Confirmed after the preview at HH:MM" with a link to that entry;
  - a preview that has an execute shows "Confirmed at HH:MM";
  - "What was asked" (args, pretty), "What Birdy did" (`result_excerpt`), and the `link` as "Open in Birdy →";
  - a Credits breakdown: Request / Data returned (N characters) / Live change / Total.
- Below 768px the panel is full screen with a back button.
- Empty state, exactly: "Nothing yet. Requests from your connected apps will show up here."
- Kind labels: Looked up · Change · Preview · Live change · Refused · <reason in plain words>.

- [ ] **Step 1: Write failing tests.**
  - `renders rows and totals`.
  - `filter change refetches with params`.
  - `row click opens panel with breakdown`.
  - `empty state copy`.
  - `load more appends`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, then build and take screenshots** at 1280 and 390, of the table, the open panel and the empty state. Save them to the plan workspace.
- [ ] **Step 5: Commit** `feat(settings): AI activity log`.

### Task 7: Admin rate inputs and Settings tabs on mobile (frontend)

**Files:**
- Modify: `src/app/admin/credits/page.jsx` (four number inputs labelled "MCP request fee", "MCP credits per 10,000 characters", "MCP live change fee", "MCP max per request", saved through the existing settings save call)
- Modify: `src/app/settings/page.jsx` (the tab bar scrolls sideways with no visible scrollbar, and the selected tab scrolls into view)
- Test: the existing admin credits test, if one exists, plus `src/app/settings/__tests__/tabs-scroll.test.jsx`

- [ ] **Step 1: Write failing tests.**
  - `admin saves mcp rates`.
  - `selected settings tab scrolled into view`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them, then take a 390px screenshot of Settings** showing every tab reachable and no page-level horizontal scroll (`document.documentElement.scrollWidth <= 390`).
- [ ] **Step 5: Commit** `feat(admin): MCP rates; fix(settings): tabs scroll on mobile`.

### Task 8: Deferred review minors

**Files:**
- Read: `docs/superpowers/notes/2026-11-15-mcp-deferred-minors.md`
- Modify: whatever each item touches, in both repos.
- Create: `docs/superpowers/notes/2026-11-15-mcp-minors-resolution.md`, a table: item · fixed/closed · reason · commit.

- [ ] **Step 1:** List every item in the notes file. For each, decide: fix, or close. Close only when the item is a deliberate spec or owner decision, is pre-existing at merge-base `b0a3511`, or was already fixed by a later commit.
- [ ] **Step 2:** Fix the "fix" items. Add a test for each behaviour change. Use small commits grouped by area, with messages `fix(<area>): …`.
- [ ] **Step 3:** Run the full backend suite twice and the frontend tests for the touched areas. Expected: fully green both times.
- [ ] **Step 4:** Write the resolution table and commit it with `docs: MCP minors resolution`.

### Task 9: Developer docs

**Files:**
- Modify: `docs/mcp.md`
- Run: `python scripts/mcp_tool_table.py`

- [ ] **Step 1:** Regenerate the tool table. Expected: 119 tools, including `send_lead_message`, `propose_report` and `preview_report`.
- [ ] **Step 2:** Update the owner and appendix sections:
  - replace "uses none of your credits" and "No credits are charged" with the pricing, using the formula, defaults, cap, free cases and admin settings;
  - add "Reply to leads" and "Report designer" to "What it can do";
  - add an "Activity log" section, covering where to find it, what it shows and the 180-day retention;
  - add the activity API to the appendix;
  - add `POSTHOG_API_KEY` and `POSTHOG_HOST` to Server settings.
- [ ] **Step 3:** Commit `docs(mcp): v2, credits, activity log`.

### After Task 9 (controller)

The controller updates the user guide (the Claude Doc "Birdy for Claude, ChatGPT and Cursor"):
- replace the "no credits" lines with the pricing in plain words;
- add an "Activity log" section.
