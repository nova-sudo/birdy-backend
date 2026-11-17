# MCP minors resolution (2026-11-16)

Sources: `2026-11-15-mcp-deferred-minors.md` (ledger, per-task reviews, final review M1-M8 and its triage) and the follow-up minors from the credits/activity/analytics/admin/mobile plan (Tasks 1-7).
Merge-base for "pre-existing": `b0a3511`. Commits are in `birdy-backend-mcp` unless marked FE (`birdy-beta-mcp`).

## Fixed

| Item | Commit |
|---|---|
| Credits period rollover `$set` dropped `credits.rev` and could clobber a concurrent top-up. Now conditional on the observed rev and period, bumps rev, retries. Tests: rev bump, concurrent top-up. When retries run out it logs a warning and returns the last stored rev, never an unstored one (test). | 1627c4e, this round |
| MCP rates 0-100 bound was UI-only. `CreditsConfigUpdate` rejects negative and > 100 (422). Tests on the model and on `PUT /api/admin/credits/config` over HTTP. | 3ef7960 |
| Activity `_parse_day` accepted trailing junk (Task 4). Strict `YYYY-MM-DD`. Test. | 6379363 |
| Activity `from` local date parsed as UTC (Task 6). API accepts an instant with offset (`Z` parsed via `.replace("Z", "+00:00")`, with a test); FE sends local midnight. Tests both sides. | ba38a66, FE cd8622b |
| M1 read tools needed no scope. A token with neither `birdy.read` nor `birdy.write` is refused (reason `no_scope`); a write token still reads. Tests updated. | 9774834, 3d297c5, FE e345841 (label) |
| M2 impersonating admin could mint a manual MCP token. `POST /api/mcp/tokens` returns 403 when `imp` is set. Test. Revoke stays allowed (safe direction). | 07218e8 |
| M6 double alert email from overlapping cron runs. Email claim is conditional on the emailed list read; the losing run skips. Test (fails without the fix). | 1888297 |
| Task 5: concurrent double-revoke fires `mcp_disconnected` twice. Revoke updates are conditional on `revoked != true`; the event fires only when `modified_count == 1`; the return value is unchanged. Test with two gathered revokes per connection (fails without the fix). | this round |
| M4 inconsistent error shapes. Rule written down in `docs/mcp.md` (ToolError for not-found/bad input, `{ok:false,error}` for refused outcomes). | fed34e5 |
| Mobile refused row orphan "·" (Task 6). Loose separator removed, kind is nowrap. Test + 390px shot. | FE cd8622b, e345841 |
| Mobile panel dialog role/focus (Task 6). `role="dialog"`, focus on open and restored on close, `aria-modal` and body scroll lock below 768px. Tests. | FE cd8622b |
| Load-more error text (Task 6). Own state and message, rows and button kept. Test. | FE cd8622b |
| Admin `mcpSaved` effect wiped in-progress edits (Task 7). Merged per field: an untouched field follows a changed saved value, an edited field keeps the edit. Tests (fee kept at 0.7, max_per_call follows 12, Save sends both). | FE a16af6f |
| Admin save toast omitted MCP rates (Task 7). Test. | FE a16af6f |
| Deferred (final report): fresh settings read per call. `has_credits` uses the 20 s settings cache; an admin toggle reaches every instance within that window. Test. | this round |
| Deferred: burst undercharge after 25 lost rev races. Retries back off with growing jitter (5 ms × attempt, ≤ 0.125 s) instead of re-colliding at once. Test forces 5 lost races. | this round |
| Deferred: manual token mint 401 after the access token lapsed. The mint reads claims without the expiry check after `get_current_user` (which renews from the refresh cookie); an expired impersonation token is still 403. Test. | this round |
| Deferred: `link_for` placeholders not URL-encoded. Values are `quote(..., safe="")`; booleans skipped. Test. | this round |
| Deferred: escaped-JSON `confirm_code` in excerpts. The pattern takes escaped quotes, and dict results are masked after `json.dumps` too (a JSON string nested as a value). Test. | this round |
| Deferred: mobile activity panel had no focus trap. Tab/Shift+Tab wrap inside the full-screen panel. Test. | FE this round |
| Deferred (pre-existing): admin markup/stopper edits wiped on revalidate. Same per-field rule as the MCP rates. Test. | FE this round |

## Closed

| Item | Reason |
|---|---|
| Bare 10-digit unix timestamps over-masked as phones (Task 3) | Documented trade-off in `services/mcp_activity.py` (controller ruling). |
| Task 3: extension suffix stays visible after mask | Low-risk cosmetic: the digits are masked, only a file-type-like suffix remains. |
| Task 1: error-dict results set `live_executed` | Already fixed in Task 2 (`is_error_result`). |
| Task 5: missing asserts, loop-bound client | Already fixed in c1280e3. |
| Task 5: concurrent double-revoke fired `mcp_disconnected` twice | FIXED in this round (see Fixed). It was not fixed by c1280e3. |
| Task 5: a Mongo lookup failure drops the activity row | Closed, narrowed. c1280e3 only made the PostHog events survive a failed lookup (`ai/mcp/access.py` `record()`, test `test_events_fire_and_result_returns_when_mongo_lookups_raise`). The activity row is still skipped in that case (the error is re-raised before `record_call`), and logged as a warning. The lookups failing means Mongo is failing, so the insert would fail too. |
| Task 1: `reset_outcome` unused Token; contextvar holds a replaced dict; no RED run; import order | Harmless / process notes. Not changed. |
| Task 1: middleware no_subscription / rate_limit untested | Already covered by later tests: `test_external_token_without_subscription_is_refused` and `test_external_calls_rate_limited_per_user` (tests/test_mcp_access.py), `test_no_subscription_refusal_logged` and `test_rate_limited_collapses_to_one_row_per_minute` (tests/test_mcp_activity.py). |
| Task 2: `wait_for` cancel can deduct without a ledger row | Accepted 0.3 s trade-off in the spec (the retry path now has a backoff test, see Fixed). |
| Task 4 minor: DCR secret plaintext, no IPv6 loopback, no aud binding, code replay not revoking grant, refresh race untested | Final-review triage: OK to leave. |
| M3 spoofable client names ("unverified app" label) | UI/owner decision. |
| M5 orphan grants listed before first token | Pre-existing design of the connector flow; list/lifecycle change is a spec decision. |
| M7 `source="mcp"` for `post_weekly_report`, `custom_ops.activate/deactivate` | Checked: those services take no `source` argument, so there is nothing to pass. |
| M8 unthrottled `/mcp/register` and `/mcp/token` | Needs a rate-limit/retention design; owner/spec decision. |
| Ledger Tasks 1-25 and Task 26/27 review minors not listed above (unused imports, utcnow, duplicate helpers, cosmetic strings, test-only gaps, hardcoded colours, localStorage email, scoreboard cap, etc.) | Final-review triage "OK to leave": cosmetic, test-only or pre-existing before `b0a3511`; none changes behaviour. |
| Pricing, what is free, retention, masking rules, layout choice A | Owner/spec decisions, not touched. |
| Task 7: tabs test asserts class strings | Test style only; behaviour covered. |
| Task 5: `get_current_user` app-wide does not check token type | Was C1 in the final review and is fixed in the final fix wave (`tests/test_rest_rejects_mcp_tokens.py`). |
| Final re-review: `update_agent_settings` fingerprint includes background-written `settings.clients.*.prepaid`, so a rare false "changed since the preview" | Closed: safe direction (the agent just previews again); excluding `prepaid` from the fingerprint is a spec change to the confirm binding. |
| Final re-review: `undo_suggestion` / `undo_card_line` carry no state fingerprint | Closed: previews come from applied, immutable records, so low risk; adding a fingerprint changes the confirm-code design (spec decision). |
| Final re-review: untested alert dedupe combination (email + overlapping runs) | Fixed: `test_overlapping_runs_email_once` (1888297). Non-email firings can still double-insert a notification under overlap; that predates this branch. |
