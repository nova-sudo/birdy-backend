# MCP minors resolution (2026-11-16)

Sources: `2026-11-15-mcp-deferred-minors.md` (ledger, per-task reviews, final review M1-M8 and its triage) and the follow-up minors from the credits/activity/analytics/admin/mobile plan (Tasks 1-7).
Merge-base for "pre-existing": `b0a3511`. Commits are in `birdy-backend-mcp` unless marked FE (`birdy-beta-mcp`).

## Fixed

| Item | Commit |
|---|---|
| Credits period rollover `$set` dropped `credits.rev` and could clobber a concurrent top-up. Now conditional on the observed rev and period, bumps rev, retries. Tests: rev bump, concurrent top-up. | 1627c4e |
| MCP rates 0-100 bound was UI-only. `CreditsConfigUpdate` rejects negative and > 100 (422). Test. | 3ef7960 |
| Activity `_parse_day` accepted trailing junk (Task 4). Strict `YYYY-MM-DD`. Test. | 6379363 |
| Activity `from` local date parsed as UTC (Task 6). API accepts an instant with offset; FE sends local midnight. Tests both sides. | ba38a66, FE cd8622b |
| M1 read tools needed no scope. A token with neither `birdy.read` nor `birdy.write` is refused (reason `no_scope`); a write token still reads. Tests updated. | 9774834, 3d297c5, FE e345841 (label) |
| M2 impersonating admin could mint a manual MCP token. `POST /api/mcp/tokens` returns 403 when `imp` is set. Test. Revoke stays allowed (safe direction). | 07218e8 |
| M6 double alert email from overlapping cron runs. Email claim is conditional on the emailed list read; the losing run skips. Test (fails without the fix). | 1888297 |
| M4 inconsistent error shapes. Rule written down in `docs/mcp.md` (ToolError for not-found/bad input, `{ok:false,error}` for refused outcomes). | fed34e5 |
| Mobile refused row orphan "·" (Task 6). Loose separator removed, kind is nowrap. Test + 390px shot. | FE cd8622b, e345841 |
| Mobile panel dialog role/focus (Task 6). `role="dialog"`, focus on open and restored on close, `aria-modal` and body scroll lock below 768px. Tests. | FE cd8622b |
| Load-more error text (Task 6). Own state and message, rows and button kept. Test. | FE cd8622b |
| Admin `mcpSaved` effect wiped in-progress edits (Task 7). Only untouched fields follow a changed saved value. Tests. | FE a16af6f |
| Admin save toast omitted MCP rates (Task 7). Test. | FE a16af6f |
| Task 7: tabs test asserts class strings | see Closed (test style) |

## Closed

| Item | Reason |
|---|---|
| Bare 10-digit unix timestamps over-masked as phones (Task 3) | Documented trade-off in `services/mcp_activity.py` (controller ruling). |
| Task 3: extension suffix stays visible after mask | Same masking rules; owner/spec decision. |
| Task 1: error-dict results set `live_executed` | Already fixed in Task 2 (`is_error_result`). |
| Task 5: missing asserts, loop-bound client; concurrent double-revoke fires `mcp_disconnected` twice; Mongo lookup failure drops the activity row | Already fixed in c1280e3. |
| Task 1: `reset_outcome` unused Token; contextvar holds a replaced dict; no RED run; middleware no_subscription/rate_limit untested; import order | Harmless / process notes; the middleware tests now cover both reasons. Not changed. |
| Task 2: no 25-retry exhaustion test; `wait_for` cancel can deduct without a ledger row | Test-only gap and an accepted 0.3 s trade-off in the spec. |
| Task 4 minor: DCR secret plaintext, no IPv6 loopback, no aud binding, code replay not revoking grant, refresh race untested | Final-review triage: OK to leave. |
| M3 spoofable client names ("unverified app" label) | UI/owner decision. |
| M5 orphan grants listed before first token | Pre-existing design of the connector flow; list/lifecycle change is a spec decision. |
| M7 `source="mcp"` for `post_weekly_report`, `custom_ops.activate/deactivate` | Checked: those services take no `source` argument, so there is nothing to pass. |
| M8 unthrottled `/mcp/register` and `/mcp/token` | Needs a rate-limit/retention design; owner/spec decision. |
| Ledger Tasks 1-25 and Task 26/27 review minors not listed above (unused imports, utcnow, duplicate helpers, cosmetic strings, test-only gaps, hardcoded colours, localStorage email, scoreboard cap, etc.) | Final-review triage "OK to leave": cosmetic, test-only or pre-existing before `b0a3511`; none changes behaviour. |
| Pricing, what is free, retention, masking rules, layout choice A | Owner/spec decisions, not touched. |
| Task 7: tabs test asserts class strings | Test style only; behaviour covered. |
| Task 1/2 "pre-existing" items (`get_current_user` type check app-wide, `_parse_day` was new) | `get_current_user` type check was C1 and is fixed in the final fix wave. |
