# Meta campaign scoping per client group — design

Date: 2026-10-17
Status: Draft, awaiting review
Repos: `birdy-backend` (FastAPI + Mongo), `birdy-beta` (Next.js)

## Problem

One client can run separate offers out of a single Meta ad account, each with
its own GHL sub-account. Example: Plush Aesthetics runs a Body Sculpting
campaign and a Face Sculpting campaign in one ad account, with two GHL
locations. In Birdy these are two client groups that share one
`meta_ad_account_id`.

Today every group pulls the whole ad account. Both Plush groups would show the
combined spend, and `facebook_leads` rows flip between the groups on each
refresh (the unique key is `(user_id, ad_account_id, lead_id)` and
`client_group_id` is overwritten by whichever group refreshed last).

## Goal

When a Meta ad account is linked to a group, the user can choose which of its
campaigns belong to that group. Every Meta figure for that group then reflects
only those campaigns: dashboards, Client Hub, Marketing Hub, Leads page,
reports, Slack briefs, alerts, custom metrics, AI suggestions and Ask Birdy.

## Decisions (agreed)

1. **Scope: everything Meta.** The selection filters every Meta consumer, not
   only reporting.
2. **New campaigns are excluded until assigned.** No name rules, no
   auto-assignment. Unassigned campaigns are surfaced with a nudge.
3. **Filter at fetch time.** The refresh asks Meta only for the selected
   campaigns, so each group's cached data is already scoped and the ~30
   downstream consumers need no change. Read-time filtering was rejected: it
   needs a change in every consumer, and the headline totals come from the
   account-level insights edge, which cannot be filtered after the fact.

## Data model

New field on `client_groups`:

```
meta_campaign_ids: list[str] | null
```

- `null` / missing: all campaigns in the ad account (today's behaviour). Every
  existing group is `null`; no backfill.
- list: only these campaign IDs. Never extended automatically.

New cached field, written by the refresh on every group sharing the account:

```
meta_account_campaigns: [{id, name, effective_status}]
meta_account_campaigns_at: datetime
```

Rules, enforced on create and on campaign edits:

1. **Shared account requires lists.** If two or more groups of the same user
   have the same `meta_ad_account_id`, every one of them must have a non-null
   `meta_campaign_ids`.
2. **No double-claiming.** A campaign ID appears in at most one group's list
   for a given `(user_id, meta_ad_account_id)`.
3. Violations return HTTP 409 with a message naming the conflicting group and
   campaign(s).

Unassigned campaigns = `meta_account_campaigns` (status ACTIVE or PAUSED)
minus the union of `meta_campaign_ids` across the groups sharing the account.
Computed, never stored.

Index: non-unique `(user_id, meta_ad_account_id)` on `client_groups`, added to
`core/indexes.py`.

## Refresh pipeline

A single helper, `campaign_filter(group) -> list | None`, returns `None` for
`meta_campaign_ids is None`, otherwise the Graph `filtering` value.

| Call | Location | Filter field |
|---|---|---|
| `/{act}/campaigns` per preset (single + batch) | `services/meta_service.py` `_campaigns_fields_for_preset` callers, `_fetch_meta_presets_batch` | `id IN [...]` |
| `/{act}/insights` per preset headline totals (single + batch) | `meta_service.py` ~1082 and batch builder | `campaign.id IN [...]` |
| `/{act}/insights?time_increment=1` daily spend | `integrations/facebook_utils/meta_daily_spend.py` | `campaign.id IN [...]` |
| `/{act}/ads` lead stage 1 | `integrations/facebook_utils/facebook_leads.py` `stage1_get_all_ad_ids` | `campaign.id IN [...]` |

Headline totals keep today's semantics within the selection: deleted or
archived campaigns that are in the list still count.

Account campaign list: `/{act}/campaigns?fields=id,name,effective_status&limit=500`
(paged), fetched once per ad account per refresh cycle and written to every
group sharing the account.

Empty list: no Graph insights calls; the group's Meta figures are zero and the
UI says "No campaigns selected".

Selected campaign missing from Meta: kept in the list, labelled "Removed in
Meta" in the picker, otherwise ignored.

**On selection change**, for that group:
1. Unset `facebook_cache`, `meta_daily_spend` and `last_meta_refresh`.
2. Reassign leads (below).
3. Queue the existing manual Meta refresh, with a full daily-spend history
   refetch (the incremental path would leave old days account-wide).

## Leads

- **Instant-form leads (`facebook_leads`)**: fixed by the stage-1 ad filter.
  Since a campaign has at most one owner, two groups never fetch the same
  lead. On selection change, a one-time pass over that account's rows sets
  `client_group_id` from `lead_data.campaign_id`: owner group if claimed,
  `null` if unassigned. When the account is not shared and the list is `null`,
  every row goes to that group.
- **GHL ad-attributed contacts**: unchanged. Each group has its own GHL
  location; the location is the client's source of truth.
- **Tracker leads**: unchanged; already group-scoped via the tracking site.

Lead counts and CPL (`update_preset_lead_counts`, `services/ad_leads.py`)
read by `client_group_id`, so they are correct once the above holds.

Not affected: GHL opportunities, funnel, revenue and `ghl_daily_leads`, which
are location-wide and never campaign-linked.

## API

- `GET /api/facebook/adaccounts/{act}/campaigns`: live list for the picker,
  each item `{id, name, effective_status, claimed_by: {group_id, name} | null}`.
  Live Graph call so a new campaign shows immediately.
- `PATCH /api/client-groups/{id}/meta-campaigns`, body
  `{campaign_ids: list[str] | null}`: validate rules, save, run the
  selection-change steps, return the updated group summary.
- `POST /api/client-groups`: accepts optional `meta_campaign_ids`.
- **First share auto-split**: when a create (or PATCH) makes an account shared
  and an existing group on that account has `null`, that group is converted to
  the explicit list of the account's campaigns not taken by the new group, and
  runs the selection-change steps too. The response reports the conversion so
  the UI can say so.
- `POST /api/onboarding/import-subaccounts`: a row whose Meta account is
  already used by another group (or by an earlier row) is created without
  Meta and flagged `meta_skipped_reason: "shared_account"`.

## UI (pd-* kit)

1. **`MetaCampaignPicker`** (new, `src/components/clients/`): searchable
   checklist with status pill per campaign; claimed campaigns disabled with
   the owner's name; "Removed in Meta" rows; optional "All campaigns" toggle
   only when the account is not shared.
2. **Create wizard, Meta step** (`src/app/clients/page.jsx` step 3): picker
   appears under the selected account. Not shared: optional, defaults to All.
   Shared: required, with the note "The rest stay with <group>".
3. **Client settings → Integrations** (`src/app/clients/[id]/page.jsx`,
   `IntegrationsContent`): Meta row shows "Campaigns: 3 of 7 · Edit", opening
   the picker in a dialog that PATCHes.
4. **Unassigned nudge**: Clients list card and Integrations tab, for shared
   accounts with unassigned ACTIVE/PAUSED campaigns: "2 unassigned campaigns ·
   Assign".
5. **Scope chip**: Client Hub header shows "Meta: N campaigns" when the list
   is set.
6. **Bulk import result**: rows with `meta_skipped_reason` show "Link Meta
   campaigns in client settings".

## Error handling

- Picker load fails: inline error with retry; save disabled.
- PATCH 409: show the server message in the dialog; nothing saved.
- Refresh with a filter fails: existing behaviour (keep previous cache, mark
  job failed). Because the cache was cleared on selection change, the UI shows
  the empty/loading state rather than stale account-wide data.

## Testing

Backend (pytest, mongomock-motor):
- Rule validation: shared requires lists; no double-claim; 409 messages.
- `campaign_filter` and that single, batch, daily-spend and lead-stage-1
  requests carry `filtering` (mocked httpx, asserting params).
- Lead reassignment by `lead_data.campaign_id`, including unassigned → null.
- First-share auto-split.
- PATCH and POST endpoints end to end against mongomock.
- Unassigned computation.

Frontend (Vitest):
- `MetaCampaignPicker` states: optional, required, claimed, removed, empty.
- Nudge count helper.

Live verification (local build against the local API, per the local verify
setup):
- Verify against Meta's live docs that `/campaigns` accepts `filtering` on
  `id`, and the insights edge on `campaign.id`, inside Batch API requests.
- One real shared account split into two groups. Screenshot Client Hub,
  Clients list and Marketing Hub for both. Check: group A spend + group B
  spend + unassigned spend = account spend for the same preset.

## Out of scope

- Name-rule or auto assignment of new campaigns.
- Campaign picker inside bulk import / onboarding review.
- Filtering GHL data by campaign.
- Ad-set-level splits (one campaign shared by two groups).
