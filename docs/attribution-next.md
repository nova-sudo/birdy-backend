# Attribution — handoff

This is a self-contained brief for picking up the attribution feature in a fresh
session. Read `docs/attribution.md` first — it is the design writeup for what is
already built and explains *why* each decision was made. This document covers only
what is left, and where it must not collide with work happening in parallel.

## What attribution is, and what it is not

**Attribution answers: which ad produced *this specific person*, and what did they
go on to be worth?** Ad → click → lead → GHL contact → opportunity → revenue.

It is **not** the same feature as landing-page lead parity, which is being built
separately (see "The seam" below). That feature answers "how many leads did this
ad produce and what did they cost" for clients on landing pages, and it gets most
of the way there by reading `ghl_contacts.contact_data.attributionSource` — data
GHL already collects and already syncs.

Attribution exists to cover what GHL's own attribution cannot: measured against
production, 21,400 of 47,775 `ghl_contacts` carry an `attributionSource.adId`.
The other ~55% have no ad link at all, and anything submitted through a non-GHL
form (Typeform, ROASForm, a custom HTML form, a bespoke landing page) never gets
one. That gap is what our own tracking script closes.

## Already built and merged

| Path | What it does |
|---|---|
| `services/attribution_service.py` | Core. Visitor upsert, identity capture, the visitor → GHL contact match, the retry queue, per-ad reporting, index creation. |
| `services/tracker_script.py` | The first-party JS snippet, served with the account's site id baked in. Mints a `visitor_id`, captures Meta/UTM identifiers, stamps hidden fields into on-page forms, decorates Typeform/ROASForm/GHL iframe and link URLs, scrapes email/phone on submit. |
| `routers/tracking.py` | Public, unauthenticated edge: `GET /t/{site_id}.js`, `POST /t/collect`, `POST /t/identify`. |
| `routers/attribution.py` | Authenticated: `GET /attribution/setup/{group_id}`, `/status/{group_id}`, `/leads-by-ad/{group_id}`. |
| `routers/cron.py` | `GET /api/cron/attribution-tick`, every 5 min (registered in `vercel.json`). |
| `tests/test_attribution.py` | 32 tests, all passing. |
| `docs/attribution.md` | The design writeup. |

Collections: `attribution_visitors` (TTL 180 days) and `attribution_matches`
(permanent, unique per `(client_group_id, ghl_contact_id)`).

`create_attribution_indexes` is registered in `core/indexes.py`, not in `main.py`'s
lifespan — index creation was moved there by commit `1c4786a` ("Stop rebuilding
every index in front of the first request"). Add any new index creator to that
module's `_load_creators()`.

Two properties of the public edge that are load-bearing and easy to break:

- Bodies arrive as `text/plain` via `sendBeacon`, which makes them CORS **simple
  requests**. No preflight ever reaches the API, so the app's strict
  `CORS_ORIGINS` allowlist is untouched. Sending JSON content-type would trigger a
  preflight the API would reject.
- Every write path answers **204, always** — bad site id, malformed body, dead
  account. These run on strangers' browsers on customers' landing pages: an error
  there is a red console message on a page we don't own, and an endpoint that
  answers differently for real and fake ids is an account oracle.

## What is left

### 1. Source tiers on `attribution_matches`

Add `source` to every match: `"birdy_tracker"` (confidence 90–100). Keep
first-match-wins within a source.

**Note the change from the original plan:** a `"ghl_native"` tier that backfilled
`attribution_matches` from `attributionSource.adId` was planned and has been
**dropped**. The parity feature reads `attributionSource` live through
`services/ad_leads.py`, so materialising it here would be a second copy of the
same fact, with two things to keep in sync and no benefit.

### 2. Meta URL-parameter tagging

New `services/meta_url_tags.py`, modelled directly on
`services/meta_status_service.py` — same `httpx` client, same `get_facebook_token`,
a `MetaUrlTagError` mirroring `MetaStatusError`. `ads_management` is already in the
OAuth scopes (`integrations/facebook_utils/facebook.py:21`) and we already POST ad
status changes, so the write path is proven.

- `audit_url_tags(...)` — page `/{ad_account_id}/ads?fields=id,name,status,url_tags`,
  classify each ad `tagged` (contains `ad_id={{ad.id}}`), `partial` (has url_tags,
  no ad_id macro) or `untagged`. Cache on the client group as
  `attribution_meta_tagging` so the UI never hits Graph on render.
- `apply_url_tags(...)` — **merge, never clobber.** Parse the existing `url_tags`,
  keep every parameter the client set, add only what is missing. Record the
  previous value in a `meta_url_tag_changes` collection.
- `revert_url_tags(...)` — restore exactly.
- ACTIVE ads by default; paused ads opt-in.

This writes to a customer's live ad account. Preview, explicit confirm, per-ad
list, undo — and never as a side effect of anything else.

### 3. The install and verify surface

This is the real product problem, and worth stating plainly: **the landing pages
are not ours, and frequently not even our user's.** A Birdy user is an agency; the
page belongs to their client, and the person who can paste a script tag is often a
third party neither of them controls. So the UI cannot assume "go edit your code".
It has to hand out something forwardable, then verify from the outside whether it
landed.

- **Per-client Tracking tab.** `src/app/clients/[id]/page.jsx:587` — add to the
  settings-modal `PageTabs` array, with a `<TabsContent>` beside `:700`. Named
  *Tracking*, not *Attribution*: it reports on the pixel half too.
- **Shareable install page.** `src/app/install/[siteId]/page.jsx` — public, no
  auth, no app chrome, fed by a new `GET /t/install/{site_id}` returning the
  client name, snippet, script URL, Meta URL parameters and whether a hit has
  arrived. The agency forwards the link; the recipient never logs in and never
  sees account data. Unknown site ids must return the same shape with
  `known: false` — no account oracle. Add `/install` to the unprotected paths in
  `src/lib/constants.js:66` and exclude it from chrome at `src/app/layout.jsx:81`,
  the way `/onboarding` is handled. Fetch with `publicRequest`, **not**
  `apiRequest` — a 401 redirect to `/login` is a dead end for someone with no
  account.
- **Agency-wide Tracking table.** New tab in `SETTINGS_TABS`
  (`src/app/settings/page.jsx:79`), a `StyledTable` of every client: Client · Lead
  source · Tracking · Last hit · Ads tagged · Attributed leads (30d) · Copy
  install link. Onboarding imports subaccounts in bulk — an agency finishes the
  wizard with fourteen clients and needs one screen to chase them, not fourteen
  modals.
- **Diagnostics endpoint** `GET /attribution/diagnostics/{group_id}` — a staged
  checklist where each stage failure has one likely cause and one instruction:
  script not seen → the tag isn't on the page; script seen but no ad clicks → Meta
  URL parameters missing, offer the one-click fix; ad clicks but no identities →
  the form is a third-party embed we can't read, so matching falls back to
  email/phone, which is fine; identities but no matches → those contacts aren't
  reaching this GHL location.

### 4. Frontend groundwork

There is **no clipboard, snippet, or code-display component anywhere in the app**
— all three are new. Everything else reuses existing primitives: `PdCard`
(`src/components/portfolio/PdCard.jsx:11`), `PageTabs`, `StyledTable`
(`src/components/ui/table-container.jsx:61`), the `STATUS_BADGES` vocabulary
(`src/components/settings/IntegrationTile.jsx:20`), `sonner` toasts.

Data fetching is hand-rolled `useState`/`useEffect`/`AbortController` over
`apiRequest` (`src/lib/api.js`), matching `src/lib/useClientGroups.js`. There is no
React Query in this app; SWR is admin-only. Polling idiom to copy:
`src/components/integrations/IntegrationsContent.jsx:140-208`.

## The seam with landing-page parity

Built in parallel: `services/ad_leads.py`, a resolver that yields ad-attributed
leads from two sources — `facebook_leads` (Meta instant forms) and `ghl_contacts`
with an `attributionSource.adId` — deduplicated on `match_keys`, with
ad/adset/campaign identity enriched from `facebook_ad_insights` rather than
trusted from GHL's free-text strings. It feeds the lead counts and CPL
(`services/meta_service.py:1560-1595`), the Leads tab
(`routers/meta.py:773`), the daily series (`:933`), and `/api/campaigns/opp-rollup`
(`routers/client_groups.py:2029`).

**`attribution_matches` is that resolver's third source.** It links a GHL contact
to an ad for the contacts GHL itself could not attribute. Wiring it in should be
one new source function plus one precedence entry in `ad_leads.py` — not a
rewrite. Precedence: instant-form row is canonical (it carries the form's question
answers), then the tracker match (it knows which touch actually converted), then
GHL's own `attributionSource` (first-touch only).

Also being built there: `services/pixel_health.py`, which diagnoses the client's
Meta Pixel from the API. It is deliberately free of any attribution import and its
frontend card is self-contained, so it can be moved into the Tracking tab by
changing one render site. Do that when the tab exists — one screen should answer
"is tracking working for this client?" with both halves on it.

## One bug to fix early

`attribution_service.leads_by_ad` resolves ad *names* by querying
`facebook_ad_insights`. **That collection is empty in production** — measured at
0 documents. Ad identity actually lives on the client group, under
`facebook_cache.entities.{ads,adsets,campaigns}` with the legacy flat
`facebook_cache.{ads,adsets,campaigns}` still written alongside it, and neither
is reliably populated on its own (one real group has 53 ads in the flat lists and
an empty `entities`).

`services/ad_leads._entity_index` / `_enrich_ad_identity` already read it
correctly, including that fallback. Reuse them rather than writing a second
lookup — and note they also fill the ad set id, which GoHighLevel never supplies
at all.

## Where to start

1. Read `docs/attribution.md`.
2. Run `./.venv/Scripts/python.exe -m pytest tests/test_attribution.py -q` to
   confirm the base is green (32 tests). Note that
   `tests/test_slack_suggestions.py::test_undo_requires_applied` fails on `main`
   for unrelated reasons — it is not yours.
3. Build the install surface before the Meta tagging. The tagging writes to live
   ad accounts and wants the most care; the install page is what makes the feature
   reachable at all.
