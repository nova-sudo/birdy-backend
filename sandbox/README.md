# Sandbox accounts: the core (phase 1)

A sandbox account is any account whose email is on a domain in `SANDBOX_EMAIL_DOMAINS`
(env, comma-separated, default `sandbox.birdy.test`). Every credential it holds starts with
`sbx_`. A request to a provider host carrying one is answered in-process by a fake; nothing
leaves the process. This file is the contract the three fakes (Meta, GoHighLevel, Hot
Prospector) are built on.

```
sandbox/__init__.py     is_sandbox_user, install, credential helpers (re-exported)
sandbox/credentials.py  make / parse sbx_ credentials
sandbox/routing.py      the httpx hook, host -> app registry
sandbox/fakes.py        shared helpers for the fake apps (build_app, require_credential, errors)
sandbox/world.py        the generated data set (pure, deterministic)
sandbox/state.py        remembered writes (Mongo `sandbox_state`)
sandbox/connect.py      the connect-flow branches used by routers/meta.py, ghl.py, hotprospector.py
sandbox/meta_app.py     fake Graph API        (phase 2 extends)
sandbox/ghl_app.py      fake LeadConnector    (phase 2 extends)
sandbox/hp_app.py       fake Hot Prospector   (phase 2 extends)
```

## 1. Who is a sandbox account

```python
from sandbox import is_sandbox_user
is_sandbox_user("tester@sandbox.birdy.test")   # True (case-insensitive, no DB read)
is_sandbox_user("alice@example.com")           # False
```

## 2. Credentials

`sbx_<base64url("kind~nonce~user_id")>.<12-char HMAC>`: URL-safe, about 90-130 characters, signed
(key: `SANDBOX_CREDENTIAL_SECRET`, else `JWT_SECRET`), so one sandbox account cannot edit a
credential into another's. A credential that encodes a non-sandbox account never parses.

```python
from sandbox import make_credential, parse_credential, credential_error

tok = make_credential("tester@sandbox.birdy.test", "ghl_location:AbC123")   # random nonce
uid = make_credential("tester@sandbox.birdy.test", "hp_uid", nonce="")      # stable
cred = parse_credential(tok)          # also accepts "Bearer sbx_..."; None when invalid
cred.user_id, cred.kind               # "tester@sandbox.birdy.test", "ghl_location:AbC123"
cred.base_kind, cred.arg              # "ghl_location", "AbC123"
credential_error(user_id, value)      # None, or why value may not be stored for user_id
```

Kinds in use: `meta_code`, `meta`; `ghl_code`, `ghl`, `ghl_refresh`, `ghl_location:<id>`,
`ghl_location_refresh:<id>`; `hp_uid`, `hp_key`, `hp`, `hp_refresh`; `media`. Add new kinds freely
(no `~`). A fake mints the credentials it hands out with `make_credential(cred.user_id, kind)`.

## 3. Routing

`sandbox.install()` (called at the top of `main.py`, idempotent) wraps `httpx.AsyncClient.send`,
which `get`, `post`, `request` and `stream` all use, whatever `transport=` / `mounts=` the client
has. For a request to a provider host (`routing.PROVIDER_HOSTS`: `*.facebook.com`, `*.fbcdn.net`,
`*.leadconnectorhq.com`, `*.gohighlevel.com`, `*.msgsndr.com`, `*.hookscall.com`,
`*.hotprospector.com`) that has `sbx_` at a token boundary in its URL, a header or its body, the
request goes to the app registered for the host via `httpx.ASGITransport`. Nothing else is touched.

```python
from sandbox.routing import register_app
register_app([".example-provider.com", "api.other.com"], asgi_app)   # exact host or ".suffix"
```

- A provider host with no registered app answers `501 {"error": {"message": "sandbox: GET /x is
  not implemented", "type": "sandbox_not_implemented"}}` and logs `sandbox_unhandled`. Never sent.
- A forged or unparsable `sbx_` marker is still answered in-process (with a 401 in the
  provider's shape).
- The sync `httpx.Client` raises `routing.SandboxLeakBlocked` for a sandbox request (the product
  never calls a provider with it).
- `event_hooks` and `auth=` of the calling client are not applied to sandboxed requests.

## 4. Writing a fake endpoint

Each fake is one `APIRouter` turned into an app by `fakes.build_app` at the bottom of its file.
**Add endpoints above the `app = build_app(...)` line** (routes are copied when the app is built);
look for the `ADD ENDPOINTS HERE` marker. Anything not matched answers "not implemented" in the
provider's error shape and logs `sandbox_unhandled provider=... method=... path=...`.

```python
from sandbox.fakes import ProviderError, request_fields, require_credential, world_of

@router.get("/me/adaccounts")                         # meta_app: no /v25.0 prefix in routes
async def adaccounts(request: Request):
    cred = await require_credential(request, "meta")  # 401 in Graph's shape when missing
    w = await world_of(cred)                          # world_for(user, overrides=state.overrides(user))
    fields = await request_fields(request)            # query + form/JSON body, merged
    if "x" in fields: raise ProviderError(400, "bad x", code=100)
    return {"data": [{"id": c.meta_ad_account_id, "name": c.name, "currency": c.currency} for c in w.clients]}
```

- `require_credential(request, *kinds)` matches a kind or a base kind (`"ghl_location"` matches
  `ghl_location:abc`; `cred.arg` is the location id). It looks at the Authorization header, then
  the URL, then the body.
- Errors: `meta_app.graph_error` (`{"error": {message, type, code, fbtrace_id}}`, a 401 becomes
  Graph's code 190 OAuthException), `ghl_app.ghl_error` (`{statusCode, message, error}`),
  `hp_app.hp_error` (`{"response": "false", "message"}`).
- Hot Prospector operations are keyed by the body's `Method`: register with
  `@hp_app.method("SearchByUserInput")` -> `async def h(cred, body, world) -> dict`.
- Unexpected exceptions answer 500 in the provider's shape and log `sandbox_error`.

## 5. Connect flows (already done in phase 1)

| Flow | Sandbox account | Fake endpoints |
|---|---|---|
| `GET /api/connect/facebook` | `auth_url` = `FACEBOOK_REDIRECT_URI?code=sbx_(meta_code)`, no dialog | `POST/GET /oauth/access_token` (code and `fb_exchange_token`), `/me`, `/me/permissions`, `/debug_token` |
| `GET /api/connect` (GHL) | `auth_url` = `REDIRECT_URI?code=sbx_(ghl_code)` | `POST /oauth/token` (authorization_code, refresh_token agency + location), `POST /oauth/locationToken` (201) |
| `POST /api/hotprospector/connect` | typed values replaced by `hp_app.sandbox_hp_credentials(user)` | `POST /glu/api/v2/auth/token`, `/auth/refresh`, `/request` + v1 `/glu/custom_api`, Method `getMemberUsers` |

Callbacks refuse a non-`sbx_` code or token for a sandbox account and an `sbx_` one for a normal
account (`sandbox.connect.code_error` / `token_error`), before exchanging anything.

## 5b. Hot Prospector fake (phase 2)

`sandbox/hp_methods.py` (operations) and `sandbox/hp_rows.py` (HP's row shapes, pure), on v2
(`/glu/api/v2/request`, Bearer) and v1 (`/glu/custom_api`, uid + key in the body, same account):
`getMemberUsers`, `SearchByUserInput` (`locationId`, `searchField`/`searchText`, `limit` <= 100,
`offset`, oldest first), `FetchUserCallLog` (`locationId`?, `from_date`/`to_date` <= 30 days,
default last 7; `type`, `limit` <= 500, `offset`, `sort_order`), `FetchLeadCallLogs` (list-wrapped),
`getMemberDashboardData` (`date`), plus `getGroups`, `getLists`, `getLeadDetails` (not called by
the product). Only call-centre clients (`hp_group_id`) have HP leads and calls; a location without
one answers an empty page. A "No Answer" call reports 0 s; a member's dashboard day is counted from
that day's call log rows. **No recordings**: `recording` / `recordingId` are empty, and a world
recording URL answers 404, so call analysis finds nothing to transcribe. Hosts that
`HOTPROSPECTOR_*_URL` point at are HP hosts too (`hp_app.configured_hosts`, read per request).

## 5c. Meta fake (phase 2)

`sandbox/meta_app.py` (the app, *.facebook.com and *.fbcdn.net) and `sandbox/meta_graph.py` (the
engine), with `meta_fields.py` (the `fields` grammar) and `meta_media.py` (placeholder media).
Paths match without the version prefix (`/v25.0/me/adaccounts` is `@router.get("/me/adaccounts")`).

- Graph's object model over the world: any node (`GET /{id}` with field expansion), any edge
  (`GET /{id}/{edge}`, paged with `cursors` and a `next` that carries the token), insights (any
  `date_preset`, `time_range`, `time_increment`, `level`), the `leads` edge, the batch API
  (`POST /` with `batch`), and writes (`POST /{id}` status / budget / name, `POST /act_x/{edge}`
  to create, `DELETE /{id}`). Errors are Graph's `{"error": {message, type, code, error_subcode,
  fbtrace_id}}`.
- Numbers add up at every level: an account's insights for a range are the sum of its
  campaigns', which are the sum of their ads', which are what the daily rows add to; the `lead`
  action count equals the leads the `leads` edge lists. Insight numbers are strings, as Graph's.
  Dates are the account's (Europe/London); a lead's `created_time` is UTC.
- Ad account ids are `act_<n>`; `w.client()` takes them with or without `act_`.
- Objects created through the fake (creative tests: ads, creatives, images, videos, campaigns,
  ad sets) are kept in `sandbox_state` (kind `meta_object`) and merged into every listing; they
  never deliver (no insights, no leads).
- Media a browser loads (creative `image_url` / thumbnails, video pictures, ad previews) points at
  the backend's own `/api/sandbox/media/...` (generated SVGs, `routers/sandbox.py`); media the
  backend downloads is on `scontent.xx.fbcdn.net` with an `sbx_` segment and is answered in-process.
- A field the fake does not know is left out (logged `sandbox_unhandled_field`), so a new product
  request still works.

## 5d. GoHighLevel fake (phase 2)

`sandbox/ghl_app.py` (the app and the token endpoints), `ghl_endpoints.py` (every other endpoint
Birdy calls), `ghl_data.py` (the world as GHL sees it) and `ghl_search.py` (`POST /contacts/search`
filters, sort, paging).

- Tokens as GHL enforces them: the agency token (`ghl`) lists locations (`/locations/search`,
  `/oauth/installedLocations`, `/locations`, `/locations/{id}`, `/users/`) and mints location tokens
  (`POST /oauth/locationToken`); a location token (`ghl_location:<id>`) works only inside its own
  location (another location's contact / opportunity / conversation is a 403). Every endpoint but
  OAuth wants a `Version` header. Refresh (`grant_type=refresh_token`, agency and location) works.
- A contact is a Meta lead (same name, email, phone, ad ids, `attributionSource`), created a little
  after the lead; its opportunity is `World.opportunity_for(lead)` (pipeline New / Contacted /
  Booked / Won / Lost); conversations, appointments, notes and custom fields are generated from the
  same deterministic primitives. Nothing is dated after `now`.
- Every paging style reaches the end (page / pageLimit, startAfter / startAfterId, searchAfter).
- Writes (create / update contact, tags, opportunity status, notes, tasks, send SMS or email) answer
  in GHL's shape and are remembered (`sandbox_state` kinds `ghl_*`), so later reads show them.
  Nothing is sent to anyone. Calls have no recordings (recording / transcription answer 404).

## 6. The world

```python
from sandbox.world import world_for
w = world_for(user_id, now=None, overrides=None)
```

Pure and deterministic, from the account id and the calendar (Europe/London). Cached per
(user, date) in-process; a later `now` on the same day only moves "today" forward. `now`
defaults to the current time (a naive datetime is read as UTC). A full 120-day, 8-client pass
takes ~0.3 s cold, ~0.05 s warm. `overrides` = `await sandbox.state.overrides(user_id)`
(`fakes.world_of` does this for you).

### Structure

| | |
|---|---|
| `w.user_id`, `w.seed`, `w.now`, `w.today`, `w.history_start` | `history_start` = today - 119 days |
| `w.agency` | `Agency(name, ghl_company_id, ghl_user_id, meta_user_id, meta_user_name, meta_business_id, hp_account_name)` |
| `w.clients` | 8 `Client`s |
| `Client` | `index, key, name, vertical ("aesthetics"/"dental"/"gym"), city, health, target_cpl, deal_value, currency "GBP", timezone, email, phone, website, address, postcode, ghl_location_id, meta_ad_account_id ("act_..."), meta_account_number, meta_page_id, lead_form_id, hp_group_id (None for 3 clients), call_centre, pipeline, members, campaigns` |
| `health` | 2 `healthy` (CPL ~0.7x target), 3 `middling` (~1x), 2 `over_cpl` (~1.8x), 1 `no_leads` (spends, 0 leads for the last 4 days) |
| `Campaign` | `id, name, status, objective "OUTCOME_LEADS", account_id, client_index, created_on, adsets, daily_budget None` (3-5 per client) |
| `AdSet` | `id, name, status, campaign_id, account_id, client_index, created_on, daily_budget (pence), optimization_goal, billing_event, audience, ads` (1-3 per campaign; budgets live here) |
| `Ad` | `id, name, status, created_on, paused_on, adset_id, campaign_id, account_id, client_index, slot, generation, creative, weight, quality` |
| `Creative` | `id, kind ("image"/"video"), title, body, call_to_action, image_url, thumbnail_url, video_id` |
| `Pipeline` | `id, name, stages` (`Stage(id, name, position)`: New, Contacted, Booked, Won, Lost); `pipeline.stage("Won")` |
| `Member` | `id, first_name, last_name, name, email, phone, role, group_id, client_index` (3-5 per call centre) |

Ads rotate: each ad set has 2-4 ad slots, each running a sequence of ad generations (21-70 days).
An ended generation is a PAUSED ad (`paused_on` = its end), the running one is ACTIVE, so every
ad set has 2-4 live ads plus older paused ones. All dates are absolute: a past day never changes.
Statuses, names and budgets on these objects already include remembered writes.

### Lookups

```python
w.client(0) / w.client("act_123...") / w.client("123...") / w.client(location_id) / w.client(hp_group_id) / w.client(key)
w.find(object_id)            # Campaign | AdSet | Ad | None
w.client_of(obj); w.campaign_of(adset_or_ad); w.adset_of(ad)
w.adsets(client_or_campaign); w.ads(scope=None)          # scope: Client | Campaign | AdSet | Ad
w.effective_status(obj)      # ACTIVE | PAUSED | ADSET_PAUSED | CAMPAIGN_PAUSED
w.members(client=None); w.member(member_id)
```

### Delivery

```python
w.days(since, until)               # [date], clamped to [history_start, today]: never a future day
w.day_bounds(day)                  # (start, end) aware datetimes of the London day
w.ad_day(ad, day)                  # DayMetrics | None (future, or before history_start)
w.daily(scope, since, until)       # [DayMetrics] one per day, summed over the scope's ads
w.metrics(scope, since, until)     # DayMetrics total; .days = days with data
```

`DayMetrics(date, spend (GBP, 2dp), impressions, reach, clicks, link_clicks, leads, days)` with
`.ctr` (%), `.cpm`, `.cpc`, `.cpl` (None without leads), `.frequency`, `+`, and
`.meta_actions()` -> `[{"action_type": "link_click"|"lead"|"onsite_conversion.lead_grouped"|"leadgen_grouped", "value": "3"}]`.

Guarantees: a campaign's / ad set's / client's day is exactly the sum of its ads' days;
`ad_day(ad, d).leads == len(ad_leads(ad, d))` and `metrics(scope, a, b).leads == len(leads(scope, a, b))`;
today is partial (spend etc. scale with the elapsed share of the London day, leads are the people
created before `now`). Reach is summed, not deduplicated.

### People

```python
w.ad_leads(ad, day)                # [Lead] behind ad_day(ad, day).leads, time order
w.leads(scope, since, until)       # [Lead] created on days in [since, until], time order
w.lead(lead_id)                    # Lead | None, without listing (ids decode)
w.contact_for(lead); w.contact(contact_id); w.contacts(client, since, until)
w.opportunity_for(lead)            # Opportunity | None (~85% have one); w.opportunity(opp_id); w.opportunities(client, since, until)
w.calls_for(lead)                  # [Call] so far (call centres only); w.calls(client, since, until)
w.hp_lead_id(lead_id); w.hp_lead(hp_lead_id)
w.contact_id(lead_id); w.opportunity_id(lead_id)
```

- `Lead(id (17 digits), client_index, ad_id, adset_id, campaign_id, account_id, form_id, created_time (aware London), first_name, last_name, full_name, email (@example.com), phone (+447700xxxxxx), answers {question: answer}, platform "fb"/"ig")`.
  Email and phone are unique per world.
- `Contact(id (20 chars), lead_id, location_id, client_index, first_name, last_name, full_name, email, phone, date_added, date_updated, source "Facebook Lead Form", tags, ad_id, adset_id, campaign_id, form_id, hp_lead_id)`:
  same email / phone / ad ids as the lead.
- `Opportunity(id, contact_id, lead_id, location_id, client_index, name, pipeline_id, stage, status "open"/"won"/"lost", monetary_value (only when won), created_at, updated_at, last_status_change_at, last_stage_change_at, history ((stage, at), ...))`:
  stage as of `now`; every timestamp <= now.
- `Call(id, lead_id, hp_lead_id, contact_id, client_index, group_id, member_id, member_name, direction "outbound"/"inbound", started_at, duration (s), disposition, recording_url | None, from_number, to_number)`, `.answered` (duration >= 45 s).
  In call centres the opportunity's Contacted time is the first answered call.
  `recording_url` is on `service.hookscall.com` with an `sbx_` path segment, so a download is
  routed to the HP fake (phase 2 serves it).
- Creative media URLs are on `scontent.xx.fbcdn.net` with an `sbx_` path segment (`world.media_url`):
  routed in-process if the backend downloads them; a browser cannot load them (open question).

### Writes

```python
from sandbox import state
await state.set(user_id, "status", ad_id, "PAUSED")          # also campaigns / ad sets
await state.set(user_id, "budget", adset_id, 4500)           # pence
await state.set(user_id, "name", object_id, "New name")
await state.set(user_id, "ghl_note", note_id, {...})         # any other kind: free for a fake
await state.get(user_id, kind, key, default=None)
await state.list(user_id, kind=None)                         # [{kind, key, value, at}] oldest first
await state.delete(user_id, kind, key)
await state.overrides(user_id)                               # -> world.Overrides
```

Collection `sandbox_state`, `{user_id, kind, key, value, at}` (+ `history` for status and budget),
unique on (user_id, kind, key) (index in `core/indexes.py`). Pass `db=` to use another database;
the default is the app's (the `mock_db` fixture in tests).

How the world applies them, from the moment `at` they were made: a status change stops or
restarts delivery (spend scaled to the active part of the day, leads only while active); every
change is kept, so a pause followed by a re-enable leaves the paused stretch at zero. A budget
change scales spend from `at`; the lead count follows the budget in effect at the start of each
day (so today's earlier leads never vanish). Names just replace.

### Known limits

- The `no_leads` client's dead stretch is relative to today: when a day leaves the last-4-days
  window it gets its leads. The only place where a past day changes (spend never does).
- Budgets are per ad set; campaigns have no budget.
- Created objects (a new ad from a creative test) are not in the world yet: phase 2 can keep them
  in `state` under its own kind and merge them into its responses.

## Trying it

A tester's whole account runs on the generated world; the rest of the product is the real one.
`tests/test_sandbox_onboarding_e2e.py` walks exactly this path through the HTTP routes.

1. **Register** at `/register` with any address on `@sandbox.birdy.test` (or another domain in
   `SANDBOX_EMAIL_DOMAINS`), for example `tester@sandbox.birdy.test`. Each address gets its own
   world: eight UK clients (aesthetics, dental, gyms) with 120 days of history, the same every
   time for that address. Fill in the basics as usual.
2. **Connect Meta**: the button skips Facebook's dialog and lands straight back on the callback
   with a sandbox code. Eight ad accounts appear, named like the clients.
3. **Connect GoHighLevel**: the same, no GoHighLevel dialog. Eight sub-accounts appear.
4. **Calling**: pick Hot Prospector and type anything into the API UID and key. What is typed is
   ignored; the account's own sandbox keys are stored. Five of the eight clients run a call centre
   (they have calls, agents and member dashboards); the other three have no Hot Prospector data.
5. **Import** ("bring in the rest"): the review reads every sub-account's recent leads and pairs
   each with its own ad account (the names match exactly; no model call is made for a sandbox
   account). Import them all.
6. **Wait for the first sync**: the cron ticks fill each client in the next few minutes, as for a
   real account (GoHighLevel two clients a minute, Meta five, Hot Prospector one client a minute
   per account). Until then a client shows as syncing. The Hot Prospector member dashboards
   backfill the current year 20 days a tick; days before the world's history read zero.
7. Everything after that reads the world: dashboards, hubs, leads, a lead's conversation, the
   call centre, the agents' Meta and GHL tools. Writes are remembered per account: pausing an ad,
   changing a budget, renaming, moving an opportunity, sending a lead an SMS or email. They show
   up in later reads (a paused ad stops spending from that moment) and are never sent anywhere.

What is fake and what stays real:

| Fake (answered in-process, never sent) | Real |
|---|---|
| Meta Graph API, GoHighLevel (LeadConnector), Hot Prospector | Billing: the first client is free; importing more needs a plan (or an admin's client-limit override) |
| The OAuth dialogs (skipped) | The AI model: chat, agents and summaries call the real model and use credits |
| Lead messages (SMS / email are remembered, not delivered) | Birdy's own database, crons, alerts, email from Birdy |
| Creative media (placeholder SVGs from `/api/sandbox/media/...`) | |
| Slack: a made-up workspace with five channels; posts are accepted and go nowhere (`sandbox/slack_fake.py`). A real workspace can be linked to one Birdy account only, so a tester's own would be refused | |

Odd but expected: calls have no recordings (call analysis finds nothing to transcribe); the
`no_leads` client stops getting leads for the last four days on purpose; today's numbers grow
through the day.
