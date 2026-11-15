# MCP connector: deferred review minors (2026-11-15)

## From the task ledger
- Task 1: minor (deferred): create_index on every external call (access.py:82) — create once
- Task 1: minor (deferred): claims["sub"] KeyError possible (access.py:119)
- Task 1: minor (deferred): unused WRITE imports in several *_mcp.py
- Task 1: minor (deferred): no test for TTL index on mcp_rate
- Task 1: minor (deferred): _index_ready flag never resets; rate-limit tests may flake at minute rollover; missing sub → shared rate bucket (subscription check should reject first — verify in final review)
- Task 2: minor (deferred): DuplicateKeyError on code collision unhandled (confirm.py:68) — drop unique index or retry
- Task 2: minor (deferred): utcnow deprecated; default=str in hash; tests for key-order/collision/execute-error
- Task 3: minor (deferred): no-op update round trips when fresh; list hides nothing expired
- Task 3: minor (deferred): legacy revoke_mcp_token (DELETE /api/mcp/tokens/{id}) has no kind filter — could revoke a single oauth token doc by id
- Task 4: minor (deferred): code replay doesn't revoke grant (SHOULD); DCR client_secret plaintext (SDK compares raw); no IPv6 loopback; no aud binding; refresh race untested
- Task 5: minor (deferred): tests override get_current_claims fully (real JWT path untested); get_current_user app-wide doesn't check type (pre-existing, whole-branch review)
- Task 6: minor (deferred): _RevocationHandler copies SDK handler (pin/comment); _get_resource_url ignores path; client_secret_basic advertised but needs form client_id; e2e assertions loose on issuer; no smoke test for main.lifespan entering mcp_app.lifespan; fixture patches private _base_app
- Task 7: minor (deferred): "Signed in as" from localStorage not session (backend GET could return email); redirect_to not https-checked client-side; hardcoded #FBFAFF
- Task 8: minor (deferred): hardcoded accent color; no tests for revoke/token failure toasts
- Task 9: minor (deferred): find_stock_agent unused (Task 10 may use it); no stale-default test; set_default_agent no-op w/o users doc; stock agent (name None) unmatchable by name — consider "birdy"/"media buying" alias
- Task 10: minor (deferred): routers/agents.py keeps _memory_to_api/_proposal_to_api aliases; routers/agent_stock.py has its own _utcnow (two clocks); reports.scoreboard silently caps HTTP at 100 (pre-existing); list_cards converts 51 rows
- Task 11: minor (deferred): edit_card summary counts keep_line_ids not kept lines; _what_happened says "Could not" for unknown ok outcomes; apply_all summary always "Started applying"; undo test tail noisy; cross-account comment overclaims (no replay-as-bob test); no cross-account on settings/template tools; weekly preview is plain text not template-rendered Slack
- Task 12: minor (deferred): rule-mode mapping duplicated (custom_agent_mcp preview vs custom_ops activate); get_agent_confidence READ persists confidence.*; module name custom_ops holds stock paths; weak "Candidates" regex
- Task 12: minor (deferred): no test for readiness/conflict changing between preview and confirm; custom_ops.deactivate docstring says "clients released" but only changes status
- Task 13: minor (deferred): deny _find pre-check duplicates service not_found; redundant outcome tuple at suggestions_mcp:190
- Task 13: minor (deferred): reopen-ack undo says "Undid the suggestion in Meta." after a "nothing changes" preview; no test for reopen path
- Task 14: minor (deferred): error shapes inconsistent ({ok,error} vs {error}); private _resolve_test/_resolve_accounts used from ai/mcp; scale re-resolves adsets at execute
- Task 14: minor (deferred): get_closes_by_ad cross-account check weak (no seeded close data); draft_creative_test **_ could let chat dispatcher override created_via — check schema
- Task 15: minor (deferred): _iso duplicated in client_mcp; redundant rows[:limit] in services/client_notes
- Task 16: minor (deferred): vacuous journey assertion (test_leads_mcp:59); get_lead doesn't guard None journey/summary (router 404s) — ⚠️ low risk, ownership+detail already passed; duplicate contact query
- Task 17: minor (deferred): _bad_input keys off "Invalid" message prefix; _group pass-through; leads_by_ad end_date exclusive (midnight) undocumented vs funnel inclusive — agent will assume end-of-day; double date parse
- Task 18: minor (deferred): "no channel" string match on skipped reason; stray "#" channel label; unused test param; brief-sync side effect untested via tool
- Task 19: minor (deferred unless fixed this round): check-then-claim not atomic; no fresh-claim boundary test
- Task 19: minor (deferred): route's unconditional claim_refresh can overlap an MCP-started GHL refresh (pre-existing)
- Task 20: review Approved; minor (deferred): TOOL_NAME regex needs an underscore (single-word tools unchecked)
- Task 21: minor (deferred unless fixed): extra `currency` field per client (keep, note in spec)
- Task 21: minor (deferred): account_check imports private _symbol from useless_ad_purger (move to shared util)
- Task 22: minor (deferred): client_name should fall back to row group_name when group deleted; import ordering; explain_alert returns raw doc incl user_id
- Task 23: minor (deferred): inline sends serialize (worst case ~30 slow sends hits 300s; recovers next hour, no dupes, one email lost); outcome logged twice; recipient email in logs; no test for users.email priority / "@" guard
- Task 24: minor (deferred): account email from localStorage (one-render fallback flash); import line style
- Task 25: minor (deferred unless fixed): ConnectAI tests coupled to dual render
- Task 25: minor (deferred): live check exit ignores leftover oauth_clients + swallowed count errors; no-scheme URL message says "non-local"; guard doesn't cover DB target (writes to whatever .env Mongo is)

## Task 26 review
# Task 26 review: send_lead_message (b74560e..b575fe9)

### Spec Compliance

- ✅ Spec compliant.
  - Tool signature matches the brief (`ai/mcp/leads_mcp.py:95-96`), registered `@mcp.tool(**LIVE)` (`:94`), uses `two_step` (`:154`), docstring has the required phrase (`:99`, asserted at `tests/test_leads_mcp.py:374`).
  - The confirm code binds `{contact_id, channel, body, subject}` exactly as typed (`leads_mcp.py:116`). A one-character whitespace change is refused (`tests/test_leads_mcp.py:418`).
  - The preview shows the name, masked phone "•••• 4821" or email "j•••@acme.com", the channel, the subject (email) and the trimmed body that will actually be sent (`leads_mcp.py:132-139`). Tests check that the raw number and the local part never leak (`tests/test_leads_mcp.py:385,396`).
  - Every validation runs before a code is issued: not found/other account (ToolError via `_owned_lead`, `:111`), empty (`:118`), over 1600 on both channels (`:120`, same as the route), subject over 300 (`:122`), no phone/email or no subject (`check_sendable`, `:126`), and a missing token returns the reconnect error (`:129`). The reconnect test asserts that no code document was written (`tests/test_leads_mcp.py:477`).
  - The inline route checks moved into `services/ghl_messages.check_sendable` (`services/ghl_messages.py:290-304`). The router delegates with the same 422 detail strings (`routers/leads.py:228-231`), and the pydantic limits now use the shared constants. I ran `tests/test_leads_router.py`: 22 passed, so the 422/409/504 status codes are unchanged.
  - Errors become dicts: Unconfirmed returns "may still have been sent, check get_lead_messages", Failed returns "rejected", Reconnect/Unavailable return the reconnect error (`leads_mcp.py:145-151`). This matches the route's 409 mapping for Unavailable.
  - Required tests are present: cross-account preview and confirm (`tests/test_leads_mcp.py:498`), replay with another contact, channel or subject (`:422`), single use (`:432`), SMS over 1600 (`:448`), error dicts (`:480`), read scope cannot send (`:510`). GHL is mocked at the service boundary.
  - Bookkeeping is done: `tests/test_mcp_client.py:547` external_only and `tests/test_mcp_registration.py:572`. The tool is not in MCP_TOOL_NAMES.
  - No routers are imported from ai/mcp. user_id comes only from `_current_user_id()`.
  - `source="mcp"`: `services.ghl_messages.send_lead_message` has no attribution parameter (checked `services/ghl_messages.py` send body: it hardcodes `"source": "api"` in its return value only). Not applicable.
- ⚠️ Cannot verify from diff: `_owned_lead` (unchanged, `leads_mcp.py:39-47`) accepts a contact row that has no `client_group_id` based on `find_lead_contact` alone. This is behaviour from an earlier task. The controller should confirm that `find_lead_contact` scopes by user_id/location id, which it is believed to do.

### Strengths

- The args hash binds the raw input, while both steps derive the sent text the same way. The preview body is byte-for-byte what `execute` sends, and the test asserts this (`tests/test_leads_mcp.py:390`).
- Code consumption is atomic `find_one_and_delete` (`ai/mcp/confirm.py consume_code`), so two parallel confirm calls cannot double-send. All validations re-run on the confirm call before the code is burned.
- The reconnect pre-check is cheap (no GHL call). It uses the same resolver as the send (`safe_token_for` wraps `token_for`, the same function as `ghl_messages._token_for`), so the pre-check and the send cannot disagree on which token they find.
- The Unconfirmed message steers the model away from a duplicate send.
- The router refactor is minimal and behaviour-preserving, and the detail strings were moved verbatim.

### Checks run

- Named risk: double send under concurrent confirms. Checked `consume_code`: it is atomic `find_one_and_delete` with an expiry filter. OK.
- Named risk: the pre-check token differs from the send token. Checked `services/ghl_live.safe_token_for` and the `ghl_messages` import: both use `token_for`. OK.
- Named risk: an SMS with a subject passes a subject to GHL. The service only adds a subject to the payload for Email (`services/ghl_messages.py` send). This is the same as the route. OK.
- Named risk: the route refactor changed HTTP behaviour. Ran `tests/test_leads_router.py`: 22 passed. Ran `tests/test_leads_mcp.py`: 26 passed.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- `ai/mcp/leads_mcp.py:80-81`: `_mask_phone` reveals every digit when a stored number has 4 digits or fewer (for example "•••• 12"). Unlikely for real leads. Masking `digits[-4:]` only when `len(digits) > 4` would close it.
- `ai/mcp/leads_mcp.py:84-86`: `_mask_email` on a malformed address with no "@" returns "x•••@". This is cosmetic only, because `check_sendable` only requires a non-blank email.
- `ai/mcp/leads_mcp.py:112-114`: the unknown-channel branch cannot be reached through MCP, because the `Literal` type is validated by fastmcp. It can only be reached through `.fn`. It is harmless as a defensive check.
- `ai/mcp/leads_mcp.py:118-123` versus `routers/leads.py:196-197`: the length and empty-body limits are enforced twice, once by pydantic in the route and once by hand in the tool. They share the constants, so they cannot drift numerically, but `check_sendable` could own them too. This is a judgement call, not required.
- `tests/test_leads_mcp.py:520-530`: `test_route_and_tool_share_the_same_checks` tests `check_sendable` directly. It does not prove that the router delegates to it, although the router tests cover the 422s by status code. The router tests do not assert the detail strings.
- Test output noise: the `datetime.utcnow()` DeprecationWarning from `ai/mcp/confirm.py:32` comes from an earlier task, not this one. It is worth fixing once branch-wide.

### Assessment

**Task quality:** Approved

**Reasoning:** The send path validates fully before issuing a code and binds exactly what the user saw. It sends exactly the previewed text, burns the code atomically, and maps every GHL failure to a clear dict. The route refactor keeps HTTP behaviour identical, and the tests cover every case the controller required.

## Task 27 review
# Task 27 review

### Spec Compliance
- ✅ Spec compliant.
- Ruling 1: services/report_drafts.py:14-83 holds DRAFTS, client_names, load_draft, save_draft, saved_brief and preview_payload (plus _clients), moved verbatim. routers/report_designer.py:194 re-imports them and ai/tools/report_designer_tools.py:129 imports from the service. The lazy router imports are gone.
- Ruling 1, HTTP: state and put_draft are untouched. The preview route delegates to preview_stored with the same branch logic, so the bodies and status codes match (routers/report_designer.py:292).
- Ruling 2: stored_template (services/report_drafts.py:366-375) mirrors the old route branch exactly. Draft falls back to the saved brief, then the default template. Saved returns None, and preview_stored turns that into the empty payload.
- Ruling 3: propose_report is tagged WRITE and its docstring says the live report doesn't change until the owner saves it (ai/mcp/report_designer_mcp.py:104-113). It returns error dicts, not raises.
- Ruling 4: the tests cover delegation for all five tools, test_draft_visible_in_router_state, a cross-user test, an invalid template saving nothing, and preview source=saved returning the empty payload.
- Constraints: user_id comes only from _current_user_id(). Registration is correct, with external_only and EXPECTED_TOOLS_BY_MODULE updated and the tools absent from MCP_TOOL_NAMES.
- Named risk, router imports: I ran `import ai.mcp` and checked sys.modules, and no `routers*` module is loaded.
- Named risk, other callers of the moved symbols: the only other user is tests/test_report_blocks.py:264, which imports preview_payload from the router. The re-export keeps that working.
- Named risk, datetime in the router: the router no longer imports datetime and has no remaining use of it (grep).

### Strengths
- Real shared state: the router and both tool layers go through one service module, and the draft-visible-in-router test exercises the actual router.state with the in-memory DB.
- Branch logic is shared in one place rather than copied into the tool.
- The cross-user test checks the document count as well as the reads.

### Issues
#### Critical
None.
#### Important
None.
#### Minor
- services/report_drafts.py:317 versus ai/tools/report_designer_tools.py:_clients. Two near-identical `_clients` helpers remain; the tools version adds `targets` to the projection. This is intentional, and it was already duplicated before this change.
- routers/report_designer.py:194-196 re-exports unused names (`_clients`, `DRAFTS`, `saved_brief`, `preview_stored`) under noqa. Trim to the ones that are used or patched (preview_payload, load_draft, save_draft, client_names).
- services/report_drafts.py:338 keeps `datetime.utcnow()`, which is deprecated. It was moved as-is; the report notes it.
- tests/test_report_designer_mcp.py test_preview_delegates monkeypatches report_drafts.preview_stored. That works because the tool calls it through the module, so it is fine, but it is a mock-heavy test; the real-behavior preview tests cover the gap.
- The reported single full-suite failure (test_slack_oauth_state tampered signature) is claimed flaky and passes in isolation. I did not re-run it.

### Assessment
**Task quality:** Approved
**Reasoning:** The service extraction is faithful, the router behaviour is unchanged, the layering rule holds, and the tests cover all four rulings. Only minor cleanup remains.

## Final review
# Final whole-branch review: Birdy MCP connector

- Backend: `feat/birdy-mcp`, b0a3511..2240c02 (42 commits, 105 files)
- Frontend: 50b36e3..55404a2 (7 commits)
- Reviewer: final cross-cutting seat. I did the review myself, in several passes. Core auth, OAuth and confirm modules plus every LIVE tool were read in full. The other tool modules, router extractions and tests were skimmed.

Checks I ran (read-only, nothing in the tree changed):
- I imported `ai.mcp` and inspected `sys.modules`. No `routers.*` module loads, even transitively.
- I enumerated the registered tools: 119 tools. Each has exactly one of the `read`/`write` tags. 12 are `live`, and all 12 go through `two_step` and contain the required docstring sentence. `analyze_call_recordings` is the only non-live tool with `confirm_code`; it is intentional, because it spends credits.
- Targeted tests: test_mcp_access, test_slack_oauth_state, test_oauth_provider, test_mcp_oauth_e2e, test_mcp_oauth_router, test_mcp_confirm, test_mcp_tokens, test_alert_email, test_agent_mcp, test_mcp_registration and test_mcp_client. 99 passed.
- Ad-hoc probes:
  - a read-only OAuth JWT is accepted by `dependencies.get_current_user` as a session cookie;
  - `resolve_agent(kind="stock")` fails when the default agent is a custom one;
  - flipping the last base64url character of a signature can leave its decoded bytes unchanged.

## Strengths

- **Single auth choke point.** `verify_birdy_token` (oauth_provider.py:67) checks the signature and expiry, plus a live `mcp_tokens` row and grant on every request. Revocation therefore takes effect on the next call, as Review Focus 5 requires.
- **Single tool gate.** `BirdyAccessMiddleware` (access.py:75) is the only place that enforces scope, subscription and rate limits. It treats every token type other than `access` as external, which is the safe default.
- **Solid OAuth storage.** In `oauth_store.py`, codes and refresh tokens are stored as sha256 hashes. Every read also filters on `expires_at` instead of relying on the TTL index. Requests are consumed with an atomic `find_one_and_delete`, and refresh rotation is an atomic delete. Replay is rejected and the rotated token keeps working (Review Focus 2).
- **Sound confirm codes.** `confirm.py` uses an atomic burn bound to user, tool, the canonical hash of the arguments, and expiry. Every LIVE tool binds the resolved `agent_id` or canonical `test["id"]`, not the raw name. `scale_creative_test` canonicalises the order of its lists. Review Focus 3 holds for argument changes.
- **Ownership by id.** `resolve_client` (client_mcp.py:23) and `resolve_agent` (agent_mcp.py:51) search only the caller's own documents. They raise on ambiguous names and never pick a candidate silently (Review Focus 4). Lead ownership goes through `find_lead_contact(user_id, ...)`.
- **Clean extractions.** The router extractions (client_refresh, views, report_drafts, slack_report_ops, attribution_reports, client_notes) are faithful. The rule that `ai/mcp` never imports `routers` holds transitively.
- **Escaped alert email.** The alert email escapes every user-controlled string, and `send_email` never raises into the cron.

---

## Critical (must fix)

### C1. MCP and OAuth tokens work as full session cookies on the whole REST API, which defeats read-only scope and revocation
- **Where:** `dependencies.py:72-82` (`get_current_user`) and `dependencies.py:108-130` (`get_current_claims`).
- **What's wrong:** Both functions accept any JWT signed with `JWT_SECRET` that has a `sub`. A `type:"mcp"` token can therefore be sent as `Cookie: auth_token=<token>`, and every REST route then treats it as a logged-in session. That includes write routes, billing, removing integrations, and `POST /api/mcp/tokens`. I probed this: a token with `scope: "birdy.read"` and a `grant_id` was accepted.
- **Why it matters:**
  - The consent screen says "View my data … Nothing gets changed". A read-only grant can still make any change through REST. It can even mint a 365-day write-scope manual token that survives revoking the grant.
  - Revocation does not reach REST at all. A revoked OAuth access token keeps working for up to an hour. A revoked manual token keeps working for up to 365 days.
  - The tokens are now handed to third-party apps through DCR, so this is no longer only a developer convenience.
  - The Task 5 review flagged the gap and deferred it to this review. The branch makes it materially worse.
- **Fix:** In `get_current_user` and `get_current_claims`, reject any payload whose `type` is not `"access"` with a 401. Every legitimate cookie issuer already sets `type:"access"`: `generate_tokens`, admin impersonation, and `ai/mcp_client`. Add a test that a `type:"mcp"` token sent as a cookie gets a 401 on one REST route. `session_claims` in mcp_oauth.py already does this check for the consent API, so the same check now becomes global.

---

## Important (should fix)

### I1. The consent page can be framed (clickjacking of OAuth approval)
- **Where:** birdy-beta `next.config.mjs` has no `headers()`; `src/app/connect/authorize/page.jsx`.
- **What's wrong:** DCR is open, cookies are SameSite=none, and the page sends no X-Frame-Options or `frame-ancestors`. An attacker can register a client, start `/authorize`, and frame `/connect/authorize?request=…` under a decoy. When the request asked for write access, "View and make changes" is preselected (ConsentCard `setAccess(...)`), so one click on Allow approves a write grant. The code is then sent to the attacker's redirect_uri. RFC 6819 §4.4.1.9 requires frame protection on the authorization page.
- **Fix:** Add `async headers()` returning `X-Frame-Options: DENY` and `Content-Security-Policy: frame-ancestors 'none'` for `/connect/:path*`, or for the whole app. As defence in depth, the backend decision endpoint could also require `Sec-Fetch-Dest` to be absent or `empty`.

### I2. An outside agent can give the media buying agent autonomy over live ads without the two-step
- **Where:** `ai/mcp/stock_agent_mcp.py:486-517` (`update_agent_settings`) and `ai/mcp/custom_agent_mcp.py:412-431` (`graduate_rule`). Both are tagged WRITE.
- **What's wrong:** `update_agent_settings` can set any zone or client `mode` to `"auto"` (settings.py:15 `MODES`) or set `weekly.auto_post: true`. `graduate_rule` can choose `auto_notify` or `auto_silent`. From then on, the agent pauses or scales live ads, or posts to Slack, on its own schedule with no confirm code. `activate_agent` is LIVE for exactly this reason: it lets an agent act. Switching a rule or zone to auto is the same kind of risk and should follow the same rule. The spec's own test is "any tool that changes live ads". These calls cause every future live change.
- **Fix:**
  - In `update_agent_settings`, compute whether the patch raises any `mode` to `auto`, or flips `weekly.auto_post` from false to true. If it does, route the call through `two_step` with args `{agent_id, settings, client_group_ids, move_claims, only_flag}`. The preview should list "clients/zones that will act on their own" and say that the weekly post is automatic. Tag the tool `LIVE`.
  - Do the same for `graduate_rule` when `choice` is not `keep_asking`. A split tool such as `allow_rule_to_run_on_its_own` would also work.
  - Lowering autonomy can stay a single-step WRITE.

### I3. Confirm codes bind the arguments but not the state that was previewed
- **Where:**
  - `creatives_mcp.py:161` binds `{"test_id"}`, yet the draft is mutable via `draft_creative_test(test_id=…)`;
  - `stock_agent_mcp.py:294` with `line_ids=None` binds "every proposed line";
  - `slack_reports_mcp.py:209` binds `{"report_id"}`, yet the channel and template are mutable via `update_slack_report`;
  - `suggestions_mcp.py:160` binds `{"suggestion_id"}`.
- **What's wrong:** The second call executes whatever the object is when it runs. Take this sequence: preview the creative test, edit its copy, accounts or targets with `draft_creative_test`, then confirm with the old code. The confirm posts content the owner never saw. The same happens if a report's channel is changed between preview and send. Review Focus 3 holds for different arguments but not for a changed object. The owner said yes to the preview, not to the id.
- **Fix:** Add a state fingerprint to the hashed args in both calls. Each tool recomputes it on the confirm call, so a change gives a hash mismatch and the code is refused with nothing changed:
  - creative tests: `test.get("updated_at")`, or a sha of creative, accounts and targets;
  - cards: the sorted `(line.id, line.state)` of the lines `_lines_of` touches;
  - Slack reports: `report["updated_at"]`;
  - suggestions: `doc.get("updated_at")`, or a hash of `action`.

  Add one test per tool: preview, mutate, confirm, then assert the code is refused and nothing was executed.

### I4. Making a custom agent the default breaks every media buying agent tool
- **Where:** `ai/mcp/agent_mcp.py:51-72`, `resolve_agent`.
- **What's wrong:** With `agent=None`, it returns the user's default agent and then enforces `kind`. After `set_default_agent("Side agent")`, every stock tool (`_stock(None)`, for example `get_agent_cards` or `apply_card`) raises "'Side' is a custom agent, not the Birdy Media Buying Agent". I reproduced this. Their docstrings promise "Defaults to the owner's Birdy Media Buying Agent". The reverse case is also unfriendly: with the stock agent as default, every custom tool needs an explicit `agent`.
- **Fix:** When `agent` is None and `kind` is not `"any"`, use the default only if its kind matches. Otherwise:
  - for `kind="stock"`, fall back to the user's stock agent;
  - for `kind="custom"`, fall back to the user's only custom agent, or raise a ToolError listing the custom agents.

  Add tests for both directions.

### I5. Alert email is never sent for a firing that starts at creation, or for a second client joining a per-client alert
- **Where:**
  - `jobs/alert_jobs.py:71,85`: `newly_triggered = alert.get("status") != "triggered"`;
  - `ai/mcp/alert_mcp.py:185-187` and `routers/alerts.py:194-196`: create-time auto-evaluation sets `status="triggered"`.
- **What's wrong:**
  - An owner creates "email me when CPL > $50" while CPL is already above $50. Create sets the status to triggered without emailing. The hourly job then never sees a transition, so no email arrives until the alert clears and fires again. This is the most common way an owner would test the feature.
  - Per-client alerts have the same gap. If Acme is already triggered and Beta starts triggering, no email is sent for Beta, because the transition test is per alert.
- **Fix:**
  - Track email delivery per firing. Set `email_notified_for = last_triggered_at` (per client for per-client alerts, for example a `emailed_clients` set) when a send succeeds. Clear it when the alert or client returns to active. In the cron, email when the alert or client is triggered and has not been emailed for this firing.
  - Also send from create and update when the initial evaluation fires, or leave the status for the cron to set. Keep one-email-per-firing (Review Focus 1).
  - Add tests: "created already-triggered → one email on next pass" and "second client triggers → one email for it".

### I6. `leads_by_ad` treats `end_date` as exclusive, while `funnel` treats it as inclusive, so counts are silently short
- **Where:** `ai/mcp/attribution_mcp.py:78-92` and `services/attribution_reports.py:24-28,48`.
- **What's wrong:** `parse_timestamp("2026-11-14")` gives midnight, so leads created on the end day are dropped. An outside agent asking for "last week" or "through today" gets an undercount with nothing to show it. `funnel` in the same module includes the end day. This was deferred as a Task 17 minor. I promoted it because it produces wrong numbers that the agent will report as fact.
- **Fix:** In the tool, or in `leads_by_ad_report` when called from MCP, add one day when `end_date` is a bare `YYYY-MM-DD` value, which makes the end day inclusive. Update the docstring ("inclusive") and add a test with a lead created on the end date.

### I7. Order- and time-dependent flaky tests (requested root causes)

**a) `tests/test_mcp_access.py`** (`test_external_calls_rate_limited_per_user` and `test_unknown_or_missing_type_is_rate_limited[None|refresh]`)
- **Root cause:** `_enforce_rate_limit` (access.py:55) uses a wall-clock fixed window, `minute = int(time.time() // 60)`. Each of these tests makes 121 calls through a fresh ASGI app and takes about 2 s; the three together take about 6 s. When a test straddles a minute boundary, the counts split across two buckets and the 121st call passes. That happens on roughly 10% of runs, more on a loaded machine.
- **Why it looks order-dependent:** Whatever ran earlier in the suite decides where in the minute these tests land. The database is a fresh mongomock per test, and `_index_ready` is harmless, so neither is the cause.
- **Fix:** Pin the clock in those tests, for example `monkeypatch.setattr("ai.mcp.access.time.time", lambda: 1_700_000_000.0)`. A cleaner option is to extract `_minute()` in access.py as the patch point.

**b) `tests/test_slack_oauth_state.py::test_tampered_signature_rejected` (lines 46-49)**
- **Root cause:** The test replaces the last character of the JWT, which is the last character of the 43-character base64url HMAC-SHA256 signature. That character carries only 4 significant bits; its lowest 2 bits are padding that `urlsafe_b64decode` discards. When the original last character is one of A, B, C or D, swapping it to "A" or "B" leaves the decoded signature bytes unchanged, so verification succeeds and `InvalidSignatureError` is not raised. I verified this: "A"→"B", "C" and "D" all decode to the same bytes. The signature changes with `exp` (time), so the test fails at random about 1 time in 16. This is not order-dependent.
- **Fix:** Tamper with a character in the middle of the signature segment, for example `h, p, s = state.split("."); s = s[:10] + ("A" if s[10] != "A" else "B") + s[11:]`. Alternatively, re-sign with a different key.

---

## Minor (nice to have)

- **M1. Read tools need no scope.** `access.py:81` gates only `write`. A correctly signed token with an empty scope set can still call every READ tool. Examples are the Slack install `state` JWT (routers/slack.py:61, no `type`, 10 minutes, carried in a URL) and a `type:"refresh"`-shaped token. **Fix:** refuse any tool when `READ_SCOPE not in token_scopes(claims)`, and update `test_unknown_or_missing_type_*` to match.
- **M2. Admin impersonation can mint MCP tokens.** `routers/mcp_tokens.py:26` (`create_token`) and `delete_connection` allow it. They use `get_current_user`, so an admin impersonating a user can mint a 365-day write token for that user. The consent decision already blocks `imp`. **Fix:** use `session_claims` and refuse when `imp` is set.
- **M3. Spoofable client names.** `AppLogo` (frontend `components/connect/AppLogo.jsx`) styles any DCR client named "Claude…" with Claude's coral tile, which makes a spoofed name look more convincing. The consent page does show `redirect_host`. **Fix:** consider showing "unverified app" when the redirect host is not claude.ai, chatgpt.com or cursor.
- **M4. Inconsistent error shapes.** Errors come back as raised `ToolError` (is_error), `{"error"}`, or `{"ok": false, "error"}`. This was deferred in Task 14. Document the rule in `docs/mcp.md`: ToolError for not-found and bad input, `{ok:false, error}` for refused business outcomes.
- **M5. Orphan grants.** `oauth_store.approve_request` creates the grant before the code is exchanged. An abandoned flow leaves an unused connection in the list. **Fix:** list only grants that have ever issued a token, or create the grant on exchange.
- **M6. Possible double alert email.** `jobs/alert_jobs.py:71` reads the status from the start-of-run snapshot. Two overlapping cron runs (a Vercel retry) can both email. **Fix:** make the transition a conditional update (`{"id": ..., "status": {"$ne": "triggered"}}`) and email only when `modified_count == 1`.
- **M7. Missing `source="mcp"`.** `post_weekly_report` (stock_agent_mcp.py:396) and `custom_ops.activate` and `deactivate` do not pass `source="mcp"`, which the Global Constraint asks for wherever a service records origin. Add the kwarg where the service stores a source.
- **M8. Unthrottled OAuth endpoints.** `/mcp/register` and `/mcp/token` have no rate limit. Open DCR can fill `oauth_clients`. **Fix:** add a per-IP limit, or a TTL on clients that never completed a grant.

---

## Triage of deferred minors

### MUST-FIX before merge
1. **Task 5: `get_current_user` app-wide doesn't check type.** This is C1: read-only and revoked tokens can act as full sessions on REST.
2. **Task 17: `leads_by_ad` end_date exclusive and undocumented.** This is I6: silently wrong numbers reach an outside agent.
3. **Task 6: `client_secret_basic` advertised but needs a form `client_id`.** A DCR client that picks the advertised method cannot get tokens. Remove it from `CLIENT_AUTH_METHODS` (oauth_provider.py:56), a one-line change.
4. **Task 13: reopen-ack undo says "Undid the suggestion in Meta." after a "nothing changes" preview.** This is a false statement about live ads. Check for `applied_targets` or the flag-only case at suggestions_mcp.py:198, a one-line change.
5. **Task 1 and Task 27: flaky rate-limit test and `test_tampered_signature_rejected`.** This is I7; the fix wave must make them deterministic.

### OK to leave
- **Task 1:**
  - `create_index` on every external call: already fixed by `_index_ready`.
  - `claims["sub"]` KeyError (access.py): already uses `.get`.
  - Unused WRITE imports: lint only.
  - No TTL index test: the TTL index is a sweep only, and expiry is enforced in queries.
  - `_index_ready` never resets: harmless, because indexes are idempotent.
  - Minute rollover: covered by I7.
  - Missing `sub` gives a shared rate bucket: verified safe, because `require_active_subscription(None)` raises 402 first, and minting such a token needs JWT_SECRET.
- **Task 2:**
  - DuplicateKeyError on a code collision: odds of about 2^-48, and the failure is loud.
  - utcnow is deprecated: warning noise only. Fix it branch-wide later.
  - `default=str` in the hash, and missing key-order, collision and execute-error tests: `sort_keys` covers key order, and the remaining gaps only touch tests.
- **Task 3:**
  - No-op update round trips: performance only.
  - List hides nothing expired: cosmetic.
  - Legacy `DELETE /api/mcp/tokens/{id}` has no kind filter: owner-scoped, so the worst case is revoking one of the user's own hourly OAuth access tokens.
- **Task 4:**
  - Code replay doesn't revoke the grant: a SHOULD, not a MUST, in OAuth 2.1, and replay is already refused.
  - DCR secret stored in plaintext: the SDK compares raw values, and these are low-value public clients.
  - No IPv6 loopback: affects only `http://[::1]`, which no target client uses.
  - No aud binding: single resource, one signing key.
  - Refresh race untested: it is an atomic `find_one_and_delete`.
- **Task 5:** tests override `get_current_claims`. The e2e and live check cover the real path.
- **Task 6:**
  - `_RevocationHandler` copies the SDK handler: pin fastmcp and mcp and add a comment.
  - `_get_resource_url` ignores path: single mount.
  - Loose e2e issuer assertions, no lifespan smoke test, and the fixture patching `_base_app`: covered by Task 25's live run.
- **Task 7:**
  - "Signed in as" comes from localStorage: display only.
  - `redirect_to` is not https-checked client-side: the server validates redirect URIs at DCR.
  - Hardcoded `#FBFAFF`: style only.
- **Task 8:** hardcoded accent colour, and no tests for the failure toasts. UI polish.
- **Task 9:**
  - `find_stock_agent` is unused: dead code.
  - No stale-default test: the stale-id fallback exists. The real default-agent bug is I4.
  - `set_default_agent` is a no-op without a users doc: every real account has one.
  - The stock agent (name None) can't be matched by name: the error points to `list_agents`, which shows the id. A "media buying" alias would be nice.
- **Task 10:** router aliases, two clocks, scoreboard cap of 100 (pre-existing), and `list_cards` converting 51 rows. Tidy-ups with no behaviour impact.
- **Task 11:**
  - `edit_card` summary counts and `_what_happened` wording: cosmetic.
  - Apply-all summary: now outcome-based.
  - Noisy undo test: cosmetic.
  - Missing cross-account tests on settings and template tools: ownership goes through the cross-account-tested `resolve_agent` and these tools take no other ids. Adding them is still recommended.
  - Plain-text weekly preview: acceptable for an outside agent.
- **Task 12:**
  - Duplicated rule-mode mapping: drift risk only.
  - `get_agent_confidence` (READ) persisting `confidence.*`: a derived cache write, not a user-visible change.
  - The name of `custom_ops` and the weak "Candidates" regex: cosmetic and test-only.
  - No test for state changing between preview and confirm: `custom_ops.activate` re-checks readiness. See I3 for the general pattern.
  - `deactivate` docstring: doc fix.
- **Task 13:**
  - Duplicate `_find` pre-check and redundant tuple: cosmetic.
  - No test for the reopen path: add one with the must-fix item #4.
- **Task 14:**
  - Error shapes: M4.
  - Private resolvers: now public.
  - Scale re-resolving ad sets at execute: it re-validates, which is the safer choice.
  - Weak `get_closes_by_ad` cross-account test: the query is user-scoped.
  - `**_` on `created_via` / `source`: an attribution label only.
- **Task 15:** duplicated `_iso`, and a redundant slice. Cosmetic.
- **Task 16:** vacuous journey assertion, no None-journey guard (ownership is already checked first), and a duplicate query. Low risk.
- **Task 17:**
  - `_bad_input` keys off a message prefix: brittle but tested.
  - `_group` pass-through and double date parse: cosmetic.
  - The end_date issue itself: must-fix, above.
- **Task 18:** "no channel" string match, a stray "#", an unused test param, and an untested brief-sync side effect. Cosmetic and test-only.
- **Task 19:** the route's claim can overlap an MCP-started GHL refresh. Pre-existing, and the shared stale-claim window bounds it.
- **Task 20:** the TOOL_NAME regex skips single-word tools. Test-only.
- **Task 21:** the private `_symbol` import. Move it to a shared util later.
- **Task 22:**
  - `client_name` fallback when a group is deleted: cosmetic.
  - Import ordering: cosmetic.
  - `explain_alert` returns `user_id`: it is the caller's own id.
- **Task 23:**
  - Serial inline sends: worst case one lost email per hour, with no duplicates. Revisit with I5.
  - Outcome logged twice, and the email in logs: the app already logs `user_id` (the email) everywhere.
  - No test for `users.email` priority: test-only.
- **Task 24:** email from localStorage with a one-render flash, and import style. UI polish.
- **Task 25:** the live-check script's leftovers, the "non-local" message, and the DB-target guard. It is a dev script, but add a guard against a production `MONGODB_URI` before anyone else runs it.
- **Task 26:** the mask edge cases, the unreachable channel branch, limits enforced twice with shared constants, the `check_sendable` test scope, and utcnow noise. Cosmetic or test-only.
- **Task 27:** duplicate `_clients`, noqa re-exports, utcnow, and a mock-heavy preview test. Cosmetic.

---

## Declined to judge

- **Metered lesson shaping on MCP paths** (`add_agent_lesson`, dismiss reason). This was ruled twice, and the owner was told.
- **GHL refresh via `create_task` on Vercel.** This was ruled; the task may be cut off with the function, and the stale-claim window bounds the damage.
- **`deactivate_agent` only turns off the stock agent.** This was ruled.
- **`propose_report` as WRITE without the two-step.** This was ruled.
- **Session `type:"access"` cookies (including impersonation) calling `/mcp` with full scope and no subscription or rate gate.** This is by design per the Global Constraints.
- **Ask Birdy create mode can't set email.** This was ruled for v1.
- **Settings tab-bar overflow.** Pre-existing, and deferred.

## Recommendations

- Apply C1 and I1 together, and re-run the Task 25 live check after them, because both touch the auth edge.
- Treat "the code binds the previewed state" (I3) and "autonomy changes are live" (I2) as written rules in `docs/mcp.md` so future LIVE tools follow them.

## Assessment

**Ready to merge: With fixes.**

The core OAuth, confirm-code and tenancy machinery is well built and well tested. However, C1 means a read-only or revoked connection is a full session on the REST API, and I1 lets an attacker clickjack the consent page. Both must be fixed before third-party apps are invited to connect. I2–I6 are concrete behaviour gaps with small, local fixes.

## Final re-reviews
# Final fix wave re-review: Birdy MCP connector

- Backend 2240c02..5f8ffef (8 commits); frontend 55404a2..ccaab60 (1 commit).
- Read: final-review.md, final-fix-report.md, both diff files in full, plus the surrounding source where a verdict depended on it.
- Checks run (read-only):
  - grep of every `jwt.encode` / `jwt.decode` / `auth_token` reader outside tests. The only session-cookie issuers are `generate_tokens`, admin impersonation and `ai/mcp_client`, and all three set `type:"access"`. `"type": "access"` was added in 2b396fd. Access tokens are short-lived, and the refresh path re-mints through `generate_tokens`, so no live session lacks the type. The only cookie decoders are `get_current_user` and `get_current_claims`. Slack `state` is decoded separately and never as a cookie.
  - `.next/routes-manifest.json` of the fixer's build: header rule regex `^/connect/authorize(?:/)?$`, so the trailing-slash variant is covered too.
  - `pytest tests/test_rest_rejects_mcp_tokens.py tests/test_slack_oauth_state.py tests/test_mcp_access.py -k "rest or tampered or rate_limited"`: 12 passed.
  - Read `cards.edit_card` (ai/agents/stock/cards.py:229-258) for concern 5, `agent_settings.update_settings` for concern 4, and the pre-branch alerts page (50b36e3) for concern 2.

## Finding verdicts

- **C1. MCP/OAuth tokens work as REST session cookies.** ADDRESSED.
  - `dependencies.py:73-80` `_require_session_type` is applied in `get_current_user` (:89) and `get_current_claims` (:135). Both raise 401 "Invalid authentication token" when `type != "access"`.
  - The expired-token refresh dance is unchanged: an expired token raises `ExpiredSignatureError` before the type check, and that path still needs a valid `type:"refresh"` cookie signed with JWT_REFRESH_SECRET.
  - Nothing else changes in REST bodies or status codes, so existing sessions are not logged out.
  - Test: tests/test_rest_rejects_mcp_tokens.py. A manual token, an OAuth token, a refresh-typed token and an untyped token each get 401. A login token gets 200. Impersonation claims survive. The MCP verifier still accepts an MCP token.
- **I1. Consent page can be framed.** ADDRESSED.
  - next.config.mjs `headers()` sends `X-Frame-Options: DENY` and `frame-ancestors 'none'` on `/connect/authorize`, and the built regex also matches the trailing slash.
  - No other page carries the consent decision.
  - The fixer verified it with curl against a production build. There is no automated test, which is acceptable for a config header.
- **I2. Autonomy without the two-step.** ADDRESSED for the three ruled cases.
  - `agent_settings.autonomy_raised` (agent_settings.py:183-205) covers:
    - agency modes moving to auto;
    - per-client effective modes moving to auto, including clearing an override with null;
    - `weekly.auto_post` turning on.
  - `update_agent_settings` validates first through `check_update`, so no code is issued for an invalid patch. It goes through `two_step` only when the list is non-empty, and is tagged LIVE.
  - `graduate_rule` validates through `check_graduation`, which also refuses a rule that is no longer on ask. `auto_*` choices go through `two_step` and `keep_asking` stays single-step.
  - The HTTP PATCH /settings route calls `update_settings` unchanged, so its bodies and codes are the same.
  - Tests:
    - auto_post needs a code, and a different patch with that code is refused;
    - the mode preview lists who acts alone;
    - lowering is one step;
    - an invalid patch issues no code;
    - a change since the preview is refused;
    - graduate: needs a code, keep_asking is one step, no offer means no code, an edit after the preview is refused.
  - Concern 3 below is a gap in the ruling's scope, not a defect against these three cases.
- **I3. Codes bind args, not previewed state.** ADDRESSED.
  - `confirm.two_step(..., state=)` stores `state_hash` with the code. On confirm, a code whose state differs is burned and returns STATE_CHANGED_ERROR, and `execute` is never called (confirm.py:509-514).
  - Fingerprints, all recomputed from the freshly loaded object on each call:
    - creative test: name, creative, targets and draft accounts;
    - Slack report: name, channel_id, template and trigger;
    - suggestion: action;
    - card: sorted (line id, state);
    - run: card id plus line fingerprint.
  - One test per tool mutates between preview and confirm and asserts refusal with no execution. There is also a unit test in test_mcp_confirm.
- **I4. Custom default breaks stock tools.** ADDRESSED.
  - `agent_mcp._default_of_kind` (agent_mcp.py:305-322) uses the default only when its kind matches. Otherwise it falls back to the stock agent for kind="stock", and to the only custom agent, an ask listing the names, or "don't have a custom agent yet" for kind="custom".
  - Tests: test_custom_default_does_not_break_stock_tools reproduces the bug through `get_agent_cards`, and test_stock_default_custom_tools_use_the_only_custom_or_ask covers the reverse.
- **I5. Alert email missed at creation or for a second client.** ADDRESSED.
  - jobs/alert_jobs.py:1057-1082 tracks `emailed_keys`: group ids for a per-client alert, `"*"` for total mode.
  - It emails each key that is newly firing, drops keys that stop firing, and clears all keys when the alert returns to active.
  - Tests: an alert created already triggered gets one email over two passes. A second client gets one email for it. A clear and re-fire of one client while another holds gets one more email.
  - One firing still sends one email.
- **I6. leads_by_ad end_date exclusive.** ADDRESSED.
  - `leads_by_ad_report(..., inclusive_end_day=True)` moves a bare YYYY-MM-DD end to end + 1 day − 1 ms (services/attribution_reports.py:1146-1147). Only the MCP tool passes it, and the docstring says the start and end days are inclusive.
  - The HTTP route behaviour is unchanged and asserted in test_leads_by_ad_end_date_is_inclusive.
- **I7a. Rate-limit flake.** ADDRESSED.
  - `access._minute()` is the patch point and `pinned_minute` pins it in both 121-call tests.
  - Patching `access.time.time` would have patched the global clock, so the fixer was right not to.
- **I7b. Tampered-signature flake.** ADDRESSED.
  - The test now changes signature character 10. That character's 6 bits all land in the decoded bytes, so the change always alters the signature and the test is deterministic.
- **Must-fix minor: client_secret_basic advertised.** ADDRESSED. oauth_provider.py:637 removes it, and test_mcp_oauth_e2e asserts it is absent from the metadata.
- **Must-fix minor: flag-only undo says "Undid the suggestion in Meta."** ADDRESSED.
  - suggestions_mcp.py:928-930 says "Reopened the suggestion; nothing changed in Meta." when `_INVERSE.get(applied_type)` is None. `applied_type` is in scope at line 182.
  - The flag-only apply now says "Acknowledged it; nothing changed in Meta."
  - Test: test_flag_only_undo_and_ack_say_nothing_changed_in_meta.
- **Must-fix minor: leads_by_ad inclusive end.** ADDRESSED; this is I6.
- **Must-fix minor: two flaky tests.** ADDRESSED; this is I7.

## Fixer's concerns

1. **The I5 email is marked when it is attempted, so there is no retry. OK.** This matches the documented v1 no-retry behaviour. The pre-fix code also had one attempt per firing. Retrying on failure would loop hourly for owners with no address. Revisit with M6 (overlapping cron runs) later.
2. **First deploy sends one email per already-triggered alert with the email channel. OK.** Before this branch the alerts page showed Email as "Coming Soon" and disabled (50b36e3 src/app/alerts/page.jsx:1527). Only alerts created on this branch, or legacy data, can carry `"email"`. One email for an alert that is firing is the correct behaviour, not spam.
3. **Autonomy raised by scope changes stays single-step. MUST-FIX (Important), even though the ruling scoped it out.**
   - When the agency mode for a change type is auto, which is exactly the configuration where I2 matters, `update_agent_settings` raises autonomy in one call without a code in two ways:
     - it adds a client to `client_group_ids`, so the new client follows the auto default;
     - it removes a client from `only_flag`. That lifts an explicit "may only flag, never change" restriction the owner set.
   - Either way the agent can then pause, add or revive ads in that client's live Meta account on its own. The result is the same as setting that client's mode to auto, which now needs a code. It is a one-call bypass of I2, and a prompt-injected outside agent could use it.
   - Fix, local and small:
     - Give `autonomy_raised` the scope diff: newly watched ids, and ids leaving `only_flag` (both only apply when `client_group_ids` is passed).
     - Emit one entry per such client for each kind whose effective mode is auto.
     - Add one test for each path.
   - If the controller keeps the ruling, record the gap in docs/mcp.md as an accepted risk.
4. **The 409 claim conflict in update_agent_settings is checked only at execute. OK.**
   - `update_settings` raises the 409 before `agent_store.update_agent` (agent_settings.py:95-101), so nothing is saved.
   - The only cost is a burned code and one more round trip, and the error tells the agent to pass `move_claims`.
   - Safe direction; UX only.
5. **The apply_card fingerprint is (line id, line state), so content edits are not detected. OK.**
   - `cards.edit_card` (cards.py:229-258) states that a line "can be dropped or reordered, nothing else about it changes". It copies stored lines verbatim.
   - No other code path rewrites a line's content. Line updates in apply.py change state and results, and those changes do reach the fingerprint.
   - A drop changes the id set, so it is detected, as the tests show.
   - The card-level `additional_changes` text is not fingerprinted. It is free text that apply does not execute in Meta, so this is not a gap.
6. **The I2 and I3 changes are mixed in commit 788b2c4. OK.** This affects commit hygiene only. The code and tests are correct, and the branch will be reviewed and merged as a whole.

## New breakage in the fix diff

None at Critical or Important level.

Minor items:
- Moving `update_agent_settings` and `graduate_rule` to LIVE also changes their success response: an autonomy-raising call now returns `needs_confirmation` first. That is intended and documented in both docstrings.
- The `update_agent_settings` fingerprint includes `settings.clients.*.prepaid`, which ai/agents/stock/alerts.py:378 writes in the background. A rare false "changed since the preview" can follow. It is safe; the agent just previews again.

## Out-of-scope observations

- `undo_suggestion` and `undo_card_line` carry no state fingerprint. Their previews come from applied, immutable records, so this is low risk.
- M6 (a double alert email from overlapping cron runs) is unchanged. `emailed_keys` comes from the start-of-run snapshot, as before.

## Verdict

**Fix round:** every listed finding is addressed and no new Critical or Important breakage was found. One fixer concern (3) is judged a must-fix gap: scope changes raise autonomy without the two-step.
# Re-review 2: concern 3 (I2 scope bypass), 5f8ffef..87be906

## Finding Verdicts
- **Watching a client, or leaving only_flag, skipped the confirm under auto** - ADDRESSED.
  - agent_settings.py `_newly_acted_on` mirrors update_settings. It returns clients in the new list that are not in the after-flag set and were not acted on before. It returns [] when client_group_ids is None, as update_settings does.
  - `autonomy_raised` then adds one entry per kind where `resolve_mode(merged, gid, kind) == "auto"`. That covers an agency-level auto and a per-client override auto. Under an agency "auto", a per-client "ask" override correctly gives no raise. With the Add gate on, `resolve_mode` returns "flag", so no raise either.
  - Empty list: in update_settings and in the runner (`scope.client_group_ids or []`), [] means no clients, not all. Nothing is newly acted on, so no raise is correct.
  - only_flag=None keeps the existing flag set intersected with ids, same as update_settings. only_flag=[] with a client leaving the flag raises. Passing the new client in only_flag does not raise.
  - Dedupe is correct: agency-level and per-client entries have distinct keys.
  - The confirm state binds settings and scope (stock_agent_mcp.py:~570), so a concurrent scope change invalidates the code. args include client_group_ids and only_flag.

## Tests
- Three new tests, all in tests/test_stock_agent_mcp.py:
  - Watching under auto: asserts the preview `clients` content, that nothing is stored before confirm, and that confirm applies.
  - Leaving only_flag under auto: asserts the preview and no mutation. It also asserts that keeping flag-only and dropping a client stay single-step.
  - Watching under ask: single-step, no code issued.
- File run: 42 passed (focused run; not the full suite).
- Not covered by a test (Minor): a per-client override of auto with agency "ask" on the scope path, and an agency+scope combined dedupe. The logic is correct by reading.

## New Breakage in the Fix Diff
None. The earlier I2 cases (agency mode, per-client mode, weekly auto_post) are untouched, and their tests still pass. routers/agent_stock.py:87 calls update_settings only and never `autonomy_raised`, so the PATCH /settings HTTP route is unchanged. A paused agent gets a confirm it does not strictly need; this is harmless (Minor).

## Out-of-Scope Observations
None.

## Verdict
**Fix round:** All findings addressed, no new Critical/Important breakage.
