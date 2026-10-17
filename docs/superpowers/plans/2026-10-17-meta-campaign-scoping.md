# Meta Campaign Scoping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several Birdy client groups share one Meta ad account, each owning a chosen set of its campaigns, with every Meta figure for a group scoped to its campaigns.

**Architecture:** A new `client_groups.meta_campaign_ids` list (null = all campaigns) is turned into a Graph `filtering` param on every Meta fetch, so each group's cached data is already scoped and the downstream consumers stay untouched. One service module (`services/meta_campaign_scope.py`) owns the rules, the filter, the scope summary and the selection-change side effects; routers stay thin. The frontend adds one reusable `MetaCampaignPicker` used by the create wizard and the client settings dialog.

**Tech Stack:** FastAPI, Motor/Mongo, httpx, pytest + pytest-asyncio + mongomock-motor (backend); Next.js 16, React 19, SWR, Vitest + Testing Library, pd-* design kit (frontend).

**Spec:** `docs/superpowers/specs/2026-10-17-meta-campaign-scoping-design.md` (birdy-backend)

## Global Constraints

- Backend work on branch `feat/meta-campaign-scoping` in `birdy-backend` (already created). Frontend work on a new branch `feat/meta-campaign-scoping` cut from `ui-revamp` in `birdy-beta`.
- Graph API version stays `v25.0`.
- `meta_campaign_ids: null` (or missing) must behave byte-identically to today: no `filtering` param is sent anywhere.
- Filter values: campaign-level edges use `{"field": "id", "operator": "IN", "value": [...]}`; insights and ads edges use `{"field": "campaign.id", "operator": "IN", "value": [...]}`.
- Rule violations return HTTP 409; the `detail` names the conflicting group and campaign name(s).
- Unassigned counts only consider `effective_status` in `{"ACTIVE", "PAUSED"}`.
- UI copy, verbatim: "No campaigns selected", "Removed in Meta", "The rest stay with {group}", "Campaigns: {n} of {total} · Edit", "{n} unassigned campaign(s) · Assign", "Meta: {n} campaigns", "Link Meta campaigns in client settings", "All campaigns".
- UI uses pd-* tokens and existing kit components only (Card, Badge, Dialog, Input, Checkbox, Skeleton). The Birdy mascot is not used in these surfaces.
- Backend tests: `cd birdy-backend && python -m pytest <path> -v`. Frontend tests: `cd birdy-beta && npx vitest run <path>`.
- Local verification builds the frontend with `NEXT_PUBLIC_API_URL` pointed at the local API; the dev DB is shared, so any test groups created must be deleted afterwards.

## Review Focus

1. **Cache empty right after a selection change** — `GET /api/client-groups/{id}` falls back to a live account-wide `/campaigns` call and to the account-keyed `account_data` cache; a scoped group must get an empty "syncing" payload instead (test in Task 7).
2. **A refresh already running when the selection changes** writes account-wide or old-scope data after the clear — pending `meta_refresh_jobs` for the group must be marked superseded and a fresh job started (test in Task 6).
3. **Ask Birdy live tools** (`meta_live_tools`, `meta_live_mcp`, `multi_window_tools`) call Graph directly and would answer with whole-account spend (test in Task 5).
4. **Long campaign-ID lists inside Batch API `relative_url`** — the filter JSON must be URL-encoded, or Meta rejects/misparses it (test in Task 3).
5. **Deleting one of two groups sharing an account** — the survivor keeps its explicit list (rules allow a list on an unshared account) and its numbers do not change; leads of the deleted group's campaigns become unassigned (test in Task 6).

---

### Task 1: Confirm Graph filtering works (spike, no product code)

**Files:**
- Create: `scripts/spike_campaign_filtering.py` (throwaway, not committed)

**Interfaces:**
- Produces: a yes/no answer recorded in the PR description for each of the four filtered calls.

- [ ] **Step 1:** WebFetch Meta's Marketing API docs for the ad account `campaigns` edge, the `insights` edge and Batch API, and confirm `filtering` is accepted with `id` / `campaign.id` and `IN`.
- [ ] **Step 2:** Write the script: for one real ad account with ≥2 campaigns (token from the local dev DB `users.integrations.facebook`), call (a) `/{act}/campaigns` with `filtering=[{id IN [c1]}]`, (b) `/{act}/insights?date_preset=last_30d` with `filtering=[{campaign.id IN [c1]}]`, (c) same as (b) with `time_increment=1`, (d) `/{act}/ads` with `filtering=[{campaign.id IN [c1]}]`, and (e) (a)+(b) inside one Batch API POST with URL-encoded `relative_url`.
- [ ] **Step 3:** Run `python -I scripts/spike_campaign_filtering.py act_XXX`. Expected: (a)/(d) return only c1's rows; (b)/(c) spend equals c1's own spend in Ads Manager; (e) matches (a)/(b).
- [ ] **Step 4:** If any call rejects the filter, STOP and report to the user before Task 3. Delete the script.

---

### Task 2: Scope rules, filter builder and summary

**Files:**
- Create: `services/meta_campaign_scope.py`
- Modify: `core/indexes.py` (add index)
- Test: `tests/test_meta_campaign_scope.py`

**Interfaces:**
- Produces:
  - `class ScopeConflict(Exception)` — `.message: str`
  - `campaign_filter(campaign_ids: list[str] | None, field: str = "campaign.id") -> str | None` — JSON string for the Graph `filtering` param, `None` when `campaign_ids is None`.
  - `async siblings(db, user_id: str, ad_account_id: str, exclude_group_id: str | None = None) -> list[dict]` — other groups on the same account, projected to `id, name, meta_campaign_ids, meta_account_campaigns`.
  - `validate_selection(group_id: str, campaign_ids: list[str] | None, others: list[dict], campaign_names: dict[str, str]) -> None` — raises `ScopeConflict`.
  - `scope_summary(group: dict, others: list[dict]) -> dict | None` — `None` when the group has no Meta account; else `{"mode": "all"|"selected", "selected": int, "total": int, "shared": bool, "unassigned": [{"id", "name"}]}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_filter_none_means_no_param():
    assert campaign_filter(None) is None

def test_filter_builds_in_clause():
    assert json.loads(campaign_filter(["1", "2"], field="id")) == [
        {"field": "id", "operator": "IN", "value": ["1", "2"]}]

def test_shared_account_requires_list():
    others = [{"id": "g2", "name": "Plush Face", "meta_campaign_ids": ["c2"]}]
    with pytest.raises(ScopeConflict):
        validate_selection("g1", None, others, {})

def test_double_claim_rejected_and_named():
    others = [{"id": "g2", "name": "Plush Face", "meta_campaign_ids": ["c2"]}]
    with pytest.raises(ScopeConflict) as e:
        validate_selection("g1", ["c1", "c2"], others, {"c2": "Face Sculpt"})
    assert "Plush Face" in e.value.message and "Face Sculpt" in e.value.message

def test_unshared_null_is_valid():
    validate_selection("g1", None, [], {})

def test_summary_counts_unassigned_active_and_paused_only():
    campaigns = [{"id": "c1", "name": "Body", "effective_status": "ACTIVE"},
                 {"id": "c2", "name": "Face", "effective_status": "ACTIVE"},
                 {"id": "c3", "name": "New", "effective_status": "PAUSED"},
                 {"id": "c4", "name": "Old", "effective_status": "ARCHIVED"}]
    g1 = {"meta_ad_account_id": "act_1", "meta_campaign_ids": ["c1"], "meta_account_campaigns": campaigns}
    g2 = {"meta_ad_account_id": "act_1", "meta_campaign_ids": ["c2"]}
    s = scope_summary(g1, [g2])
    assert s == {"mode": "selected", "selected": 1, "total": 4, "shared": True,
                 "unassigned": [{"id": "c3", "name": "New"}]}

def test_summary_all_mode_has_no_unassigned():
    g = {"meta_ad_account_id": "act_1", "meta_campaign_ids": None, "meta_account_campaigns": [{"id": "c1", "name": "x", "effective_status": "ACTIVE"}]}
    assert scope_summary(g, [])["unassigned"] == []

async def test_siblings_excludes_self_and_other_users(mock_db): ...
    # seed g1,g2 (user A, act_1), g3 (user B, act_1), g4 (user A, act_2)
    # siblings(mock_db, "A", "act_1", exclude_group_id="g1") -> ids == ["g2"]
```

- [ ] **Step 2:** Run `python -m pytest tests/test_meta_campaign_scope.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement the five interfaces in `services/meta_campaign_scope.py`. Add `client_groups` index `[("user_id", 1), ("meta_ad_account_id", 1)]` (non-unique) to `core/indexes.py` following the existing entries.
- [ ] **Step 4:** Run the tests. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): rules, filter builder and scope summary"`.

---

### Task 3: Scoped preset fetch (campaigns + headline totals)

**Files:**
- Modify: `services/meta_service.py` — `_fetch_meta_campaigns_for_preset` (985), `_fetch_meta_presets_batch` (1112), `fetch_meta_all_presets_for_group` (1315), `fetch_meta_data_for_group` (116; the `/campaigns` calls at 143 and 173)
- Test: `tests/test_meta_campaign_filtering.py`

**Interfaces:**
- Consumes: `campaign_filter` (Task 2).
- Produces: `_fetch_meta_campaigns_for_preset(..., campaign_ids: list[str] | None = None)`, `_fetch_meta_presets_batch(..., campaign_ids: list[str] | None = None)`, `fetch_meta_data_for_group(..., campaign_ids: list[str] | None = None)`. `fetch_meta_all_presets_for_group` reads `meta_campaign_ids` off the group document itself (signature unchanged).

- [ ] **Step 1: Write the failing tests** (mock httpx with `httpx.MockTransport`, as `tests/test_meta_daily_spend.py` does, recording requests)

```python
async def test_single_preset_sends_id_filter_on_campaigns_and_campaign_id_on_insights(): ...
    # campaign_ids=["c1"]: /campaigns URL has filtering=[{"field":"id",...["c1"]}],
    # /insights URL has filtering=[{"field":"campaign.id",...["c1"]}]

async def test_single_preset_no_filter_when_none(): ...
    # campaign_ids=None: no "filtering" in either URL

async def test_batch_relative_urls_carry_urlencoded_filter(): ...
    # POST body batch[i]["relative_url"] contains urllib.parse.quote(campaign_filter([...]))
    # and no raw '[' or '"' characters; 300 ids still produce one batch POST

async def test_empty_list_makes_no_graph_call_and_returns_zero_preset(): ...
    # campaign_ids=[]: transport handler asserts never called;
    # result["metrics"]["insights"]["spend"] == 0 and result["campaigns"] == []

async def test_all_presets_reads_selection_from_group(mock_mongo_client): ...
    # group doc meta_campaign_ids=["c1"]; fetch_meta_all_presets_for_group sends the filter
```

- [ ] **Step 2:** Run `python -m pytest tests/test_meta_campaign_filtering.py -v`. Expected: FAIL.
- [ ] **Step 3:** Thread `campaign_ids` through the listed functions. In the batch builder append `&filtering=` + `urllib.parse.quote(filter_json)` to both relative URLs; in single calls pass `filtering` in `params`. For `campaign_ids == []` return `_finalize_preset_result(date_preset, [], [], [], <zero totals>)` without any HTTP call. `fetch_meta_all_presets_for_group` loads `meta_campaign_ids` with the group it already reads.
- [ ] **Step 4:** Run the new tests plus `tests/test_meta_account_totals.py tests/test_meta_preset_failure.py tests/test_facebook_cache_shape.py`. Expected: all PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): filter preset fetches by selected campaigns"`.

---

### Task 4: Scoped daily spend, account campaign list, leads

**Files:**
- Modify: `integrations/facebook_utils/meta_daily_spend.py` — `fetch_account_daily_spend` (157), `cache_account_daily_spend` (232)
- Modify: `integrations/facebook_utils/facebook_leads.py` — `stage1_get_all_ad_ids` (337), `fetch_all_facebook_leads_staged` (519), `fetch_and_cache_facebook_leads_FIXED` (64)
- Modify: `integrations/facebook_utils/meta_incremental_refresh.py` — `fetch_todays_facebook_leads_incremental` (391)
- Modify: `services/meta_campaign_scope.py` — add account campaign list
- Modify: `services/meta_refresh_manager.py` — `execute_refresh` (181), after the presets phase
- Test: `tests/test_meta_campaign_filtering.py` (extend), `tests/test_meta_daily_spend.py` (extend)

**Interfaces:**
- Consumes: `campaign_filter`, `siblings` (Task 2).
- Produces:
  - `fetch_account_daily_spend(..., campaign_ids: list[str] | None = None)`; `cache_account_daily_spend` reads `meta_campaign_ids` off the group.
  - `stage1_get_all_ad_ids(ad_account_id, access_token, campaign_ids: list[str] | None = None)`; same keyword added to `fetch_all_facebook_leads_staged` and `fetch_todays_facebook_leads_incremental`; `fetch_and_cache_facebook_leads_FIXED` passes the group's value.
  - `async fetch_account_campaigns(ad_account_id: str, access_token: str) -> list[dict] | None` — paged `/{act}/campaigns?fields=id,name,effective_status&limit=500`; `None` on failure.
  - `async refresh_account_campaigns(db, user_id: str, ad_account_id: str, access_token: str, max_age_minutes: int = 15) -> None` — skips if any group on the account has `meta_account_campaigns_at` newer than `max_age_minutes`; otherwise writes `meta_account_campaigns` + `meta_account_campaigns_at` to every group on the account. Never raises.

- [ ] **Step 1: Write the failing tests**

```python
async def test_daily_spend_sends_campaign_filter(): ...       # filtering in URL for ["c1"]
async def test_daily_spend_empty_list_writes_no_rows(): ...   # [] -> no HTTP, returns []
async def test_stage1_ads_filtered_by_campaign(): ...         # /ads URL filtering campaign.id
async def test_account_campaigns_follows_paging(): ...        # 2 pages -> 2 items, None on HTTP 500
async def test_refresh_account_campaigns_writes_all_siblings_once(mock_mongo_client): ...
    # g1,g2 on act_1; one call writes both; second call within 15 min makes no HTTP request
async def test_refresh_account_campaigns_failure_keeps_previous(mock_mongo_client): ...
```

- [ ] **Step 2:** Run the two test files. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. `cache_account_daily_spend` already rewrites the whole 400-day window, so no separate history refetch is needed after a selection change. In `execute_refresh`, call `refresh_account_campaigns` once after the presets loop, inside `try/except` like the daily-spend phase, and outside `_finalize_job`'s status set.
- [ ] **Step 4:** Run the two files plus `tests/test_ad_leads.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): scope daily spend and lead fetch; cache account campaign list"`.

---

### Task 5: Ask Birdy live Meta tools respect the selection

**Files:**
- Modify: `ai/tools/meta_live_tools.py` (Graph calls at 73, 216, 261; callers at 334, 381, 409)
- Modify: `ai/mcp/meta_live_mcp.py` (85, 228, 273; callers at 364, 424, 469)
- Modify: `ai/tools/multi_window_tools.py` (`_fetch_daily_insights` at 55; group projection at 203)
- Test: `tests/test_meta_live_scope.py`

**Interfaces:**
- Consumes: `campaign_filter` (Task 2).
- Produces: each Graph helper gains `campaign_ids: list[str] | None = None`; callers pass `g.get("meta_campaign_ids")` and the group projections include `meta_campaign_ids`.

- [ ] **Step 1: Write the failing tests:** one per file, calling the public tool with a seeded group whose `meta_campaign_ids=["c1"]` and asserting the recorded Graph URL carries the filter (`id` for `/campaigns`, `campaign.id` for `/insights` and `/ads`), and that a group with `None` sends none.
- [ ] **Step 2:** Run `python -m pytest tests/test_meta_live_scope.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the new test plus `tests/test_suggestion_account_scope.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): scope Ask Birdy live Meta tools"`.

---

### Task 6: Selection change service

**Files:**
- Modify: `services/meta_campaign_scope.py`
- Test: `tests/test_meta_campaign_scope.py` (extend)

**Interfaces:**
- Consumes: Task 2 functions; `facebook_leads` collection.
- Produces:
  - `async reassign_account_leads(db, user_id: str, ad_account_id: str) -> int` — for every `facebook_leads` row on the account, set `client_group_id`/`client_group_name` to the owner of `lead_data.campaign_id`; if exactly one group is on the account and its list is `None`, that group owns all; otherwise unclaimed → `client_group_id: None`. Returns rows modified.
  - `async apply_selection(db, user_id: str, group_id: str, campaign_ids: list[str] | None, campaign_names: dict[str, str]) -> dict` — validates (raises `ScopeConflict`), auto-splits siblings whose list is `None` into "account campaigns not in `campaign_ids`", saves, for every changed group unsets `facebook_cache`, `meta_daily_spend`, `last_meta_refresh` and marks its non-terminal `meta_refresh_jobs` `status: "superseded"`, then runs `reassign_account_leads`. Returns `{"changed_group_ids": [...], "converted": [{"id", "name"}]}`.
  - `async on_group_deleted(db, user_id: str, ad_account_id: str) -> None` — runs `reassign_account_leads`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_reassign_moves_leads_by_campaign(mock_db): ...
    # leads for c1,c2,c3 all on g1; g1=["c1"], g2=["c2"] -> c1->g1, c2->g2, c3->None
async def test_reassign_single_null_group_owns_everything(mock_db): ...
async def test_first_share_auto_splits_null_sibling(mock_db): ...
    # g1 null, account campaigns c1,c2,c3; apply_selection(g2, ["c2"])
    # -> g1 == ["c1","c3"], converted == [{"id":"g1","name":"Plush Body"}], both changed
async def test_apply_clears_caches_and_supersedes_jobs(mock_db): ...
    # facebook_cache/meta_daily_spend/last_meta_refresh gone; pending job status == "superseded"
async def test_apply_conflict_writes_nothing(mock_db): ...
async def test_delete_one_of_two_keeps_survivor_list(mock_db): ...
    # remove g2 doc, on_group_deleted -> g1 list unchanged, c2 leads -> None
```

- [ ] **Step 2:** Run the file. Expected: new tests FAIL.
- [ ] **Step 3:** Implement. Check how `claim_next_jobs` (`services/meta_refresh_manager.py:843`) selects jobs and make sure `"superseded"` is excluded from claims (add to its terminal set if it filters by allow-list).
- [ ] **Step 4:** Run the file plus `tests/` files matching `meta_refresh`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): apply selection, auto-split and lead reassignment"`.

---

### Task 7: API endpoints and group payloads

**Files:**
- Modify: `routers/client_groups.py` — create (622), list projection + payload (155–482), detail (1124; fallback at 1282–1330), delete (2873), new PATCH route
- Modify: `routers/meta.py` — new GET route
- Modify: `core/models.py` — `ClientGroupRequest` (35) gets `meta_campaign_ids: Optional[list[str]] = None`; new `MetaCampaignsRequest(BaseModel)` with `campaign_ids: Optional[list[str]]`
- Test: `tests/test_meta_campaign_routes.py` (call router functions directly with `get_mongo_client` monkeypatched, as `tests/test_client_targets.py` does)

**Interfaces:**
- Consumes: Tasks 2, 4, 6.
- Produces (frontend relies on these):
  - `GET /api/facebook/adaccounts/{ad_account_id}/campaigns` → `{"campaigns": [{"id", "name", "effective_status", "claimed_by": {"group_id", "name"} | null}]}`; 502 with detail when Graph fails.
  - `PATCH /api/client-groups/{group_id}/meta-campaigns` body `MetaCampaignsRequest` → `{"meta_campaign_ids": [...] | null, "converted": [{"id","name"}], "meta_scope": <scope_summary>}`; 404 unknown group; 409 `ScopeConflict.message`. Starts `_run_refresh(gid, "meta", ...)` for each changed group as a background task.
  - `POST /api/client-groups` accepts `meta_campaign_ids`; response adds `converted`.
  - List and detail group payloads add `meta_campaign_ids` and `meta_scope` (from `scope_summary`; siblings looked up once per user in the list route, not per group).

- [ ] **Step 1: Write the failing tests**

```python
async def test_patch_saves_and_returns_scope(api, mock_db): ...
async def test_patch_conflict_is_409_with_message(api, mock_db): ...
async def test_patch_other_users_group_is_404(api, mock_db): ...
async def test_create_same_meta_different_ghl_with_list_is_allowed(api, mock_db): ...
    # previously rejected only for exact pair; now: g1 null on act_1, create g2 with ["c2"] -> 200, converted g1
async def test_create_same_meta_without_list_when_shared_is_409(api, mock_db): ...
async def test_list_payload_has_meta_scope(api, mock_db): ...
async def test_detail_scoped_group_with_empty_cache_does_not_call_graph(api, mock_db, monkeypatch): ...
    # meta_campaign_ids=["c1"], facebook_cache={} -> facebook block empty with syncing flag; httpx never called; account_data cache not read
async def test_campaigns_endpoint_marks_claimed_by(api, mock_db, monkeypatch): ...
async def test_delete_group_reassigns_account_leads(api, mock_db): ...
```

- [ ] **Step 2:** Run `python -m pytest tests/test_meta_campaign_routes.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement. Create: replace the Meta half of the duplicate check with `validate_selection` against `siblings` (keep the GHL-location duplicate check as is); when the account is shared, call `apply_selection` after insert so the auto-split runs. Detail: if `meta_campaign_ids is not None` and the group's own cache is empty, return the empty Meta block with `"syncing": True` instead of the live/`account_data` fallback. Delete: call `on_group_deleted` after the group is removed.
- [ ] **Step 4:** Run the new file and the whole suite `python -m pytest -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): campaigns picker API, PATCH selection, scoped payloads"`.

---

### Task 8: Bulk import skips shared Meta accounts

**Files:**
- Modify: `routers/onboarding.py` — `import_subaccounts` (1162; row loop 1191–1250)
- Test: `tests/test_meta_campaign_routes.py` (extend)

**Interfaces:**
- Produces: each created-row result may include `"meta_skipped_reason": "shared_account"`.

- [ ] **Step 1: Write the failing test:** `test_bulk_import_row_on_used_account_created_without_meta` — existing group on `act_1`; import rows A (`act_1`) and B, C both `act_2` → A has no `meta_ad_account_id` and `meta_skipped_reason == "shared_account"`; B keeps `act_2`; C has no Meta and the reason.
- [ ] **Step 2:** Run it. Expected: FAIL.
- [ ] **Step 3:** Implement: build the set of used accounts once before the loop, add each accepted row's account to it.
- [ ] **Step 4:** Run the file. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): bulk import never silently shares a Meta account"`.

---

### Task 9: `MetaCampaignPicker` component (birdy-beta)

**Files:**
- Create: `src/components/clients/MetaCampaignPicker.jsx`
- Test: `src/components/clients/__tests__/MetaCampaignPicker.test.jsx`

**Interfaces:**
- Consumes: `GET /api/facebook/adaccounts/{id}/campaigns` via `apiRequest` (`src/lib/api.js`).
- Produces: `<MetaCampaignPicker adAccountId groupId value onChange required shareNote />`
  - `value: string[] | null` (null = All campaigns), `onChange(next: string[] | null)`.
  - `groupId` (optional) — campaigns whose `claimed_by.group_id === groupId` are selectable, others with `claimed_by` are disabled and show the owner name.
  - `required: bool` — hides the "All campaigns" toggle.
  - `shareNote: string | null` — rendered under the list (wizard passes "The rest stay with {group}").
  - IDs in `value` absent from the response render as rows labelled "Removed in Meta" that can be unticked.

- [ ] **Step 1: Write the failing tests** (mock `@/lib/api` as `ClientTargetsForm.test.jsx` does)

```js
it("defaults to All campaigns when optional and value is null")
it("hides All campaigns and starts empty when required")
it("disables a campaign claimed by another group and shows its name")
it("keeps this group's own claimed campaigns selectable")
it("shows Removed in Meta for a selected id missing from Meta")
it("filters rows by search text")
it("shows an error with Retry when loading fails")
it("calls onChange with the ticked ids")
```

- [ ] **Step 2:** Run `npx vitest run src/components/clients/__tests__/MetaCampaignPicker.test.jsx`. Expected: FAIL.
- [ ] **Step 3:** Implement with pd-* tokens, search `Input`, status `Badge` per row, `Skeleton` while loading, matching the ad-account list styling in `src/app/clients/page.jsx` step 3.
- [ ] **Step 4:** Run the test. Expected: PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): MetaCampaignPicker"`.

---

### Task 10: Wire picker into the create wizard and client settings; nudge and scope chip

**Files:**
- Modify: `src/app/clients/page.jsx` — wizard step 3 (864+), `handleCreateClientGroup` (460; POST body at 489/530), Clients table name cell
- Modify: `src/components/integrations/IntegrationsContent.jsx` — Meta `IntegrationCard` (250)
- Modify: `src/app/clients/[id]/page.jsx` — header (381)
- Modify: `src/lib/normalize-group.js` — pass through `meta_campaign_ids`, `meta_scope`
- Create: `src/components/clients/MetaCampaignsDialog.jsx`
- Test: `src/components/clients/__tests__/MetaCampaignsDialog.test.jsx`, extend `src/lib/__tests__` normalize-group test if present

**Interfaces:**
- Consumes: `MetaCampaignPicker` (Task 9); `meta_scope` and `meta_campaign_ids` from list/detail payloads; PATCH and POST from Task 7.
- Produces: `<MetaCampaignsDialog open onOpenChange group onSaved />` — PATCHes, shows the 409 `detail` inline, toasts "Campaigns saved — Meta data is refreshing", and if `converted` is non-empty also toasts "{name} now keeps the remaining campaigns".

- [ ] **Step 1: Write the failing tests** for `MetaCampaignsDialog`: saves via PATCH with ticked ids; shows the 409 detail and keeps the dialog open; toasts the converted group name.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement:
  - Wizard: after an ad account is picked, render the picker. `required` and `shareNote` are set when another group already uses the account (the list payload already holds every group's `meta_ad_account_id`). Send `meta_campaign_ids` in the POST.
  - Meta card details gain "Campaigns: {n} of {total} · Edit" when `meta_scope.mode === "selected"` (else "All campaigns · Edit"), opening `MetaCampaignsDialog`; when `meta_scope.unassigned.length` > 0 show "{n} unassigned campaign(s) · Assign" opening the same dialog.
  - Clients table: the same unassigned nudge as a small `Badge` beside the name.
  - Client Hub header: "Meta: {n} campaigns" chip when `meta_scope.mode === "selected"`.
  - Scoped group with `facebook.syncing` true and no data shows "No campaigns selected" when the list is empty, otherwise the existing awaiting-first-data state.
- [ ] **Step 4:** Run `npx vitest run`. Expected: all PASS.
- [ ] **Step 5:** Commit: `git commit -m "feat(meta-scope): campaign selection in wizard and client settings"`.

---

### Task 11: Live verification

**Files:** none (evidence goes in the PR description)

- [ ] **Step 1:** Run the backend locally and build the frontend with `NEXT_PUBLIC_API_URL=http://localhost:8000`.
- [ ] **Step 2:** On a real ad account with ≥2 campaigns, create test group A (all campaigns), then test group B claiming one campaign. Expected: the wizard says "The rest stay with A"; A converts to an explicit list.
- [ ] **Step 3:** After both refresh, check for `last_30d`: spend(A) + spend(B) + spend(unassigned) = account spend from an unfiltered `/insights` call.
- [ ] **Step 4:** Screenshot the Client Hub, Clients list, Marketing Hub and Integrations tab for A and B; confirm the scope chip, the counts and that no A campaign appears under B.
- [ ] **Step 5:** Launch a new campaign (or un-assign one) and confirm the "unassigned" nudge appears and assigning it clears it.
- [ ] **Step 6:** Delete test groups A and B from the shared dev DB and confirm their `facebook_leads` rows are gone or unassigned.
