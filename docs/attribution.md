# Attribution — ad → click → lead

Birdy already knew what happened on Meta and what happened in GoHighLevel. It
never knew which ad produced which contact. This is that join.

The design principle: **the form is not responsible for attribution.** A client
can run ten ads into one landing page and one Typeform, swap form providers, or
use a form we've never heard of, and the numbers still work. Attribution lives
at the visitor level, and the form is just one of several ways a visitor tells
us who they are.

```
Meta ad  →  landing page  →  any form  →  GoHighLevel
   │            │                            │
 ad_id      visitor_id                  contact_id
   └──────────  attribution_matches  ─────────┘
```

## The four layers

| Layer | Who owns it | Where it lives |
|---|---|---|
| Which ad was clicked | Meta URL parameters | `attribution_visitors.first_touch` / `last_paid_touch` |
| Who visited | our tracking script | `attribution_visitors._id` (the `visitor_id`) |
| Who became a lead | the form, any form | `attribution_visitors.match_keys` (email/phone) |
| What that lead became | GoHighLevel | `ghl_contacts`, opportunities, `call_logs` |

`attribution_matches` is the row that joins them, and it is the only permanent
artifact — anonymous visitor rows age out after 180 days.

## Setup, per client

Three steps, and the third is optional.

1. **Install the snippet.** `GET /attribution/setup/{group_id}` returns it,
   minting the site key on first call:

   ```html
   <script async src="https://api.birdy.ai/t/<site_id>.js"></script>
   ```

   The `site_id` is a public key, like a Meta pixel id. It grants nothing.

2. **Paste the URL parameters into Meta** (ad level → *URL parameters*). The
   same endpoint returns the string:

   ```
   utm_source=facebook&utm_medium=paid&utm_campaign={{campaign.name}}
   &utm_content={{ad.name}}&campaign_id={{campaign.id}}
   &adset_id={{adset.id}}&ad_id={{ad.id}}
   ```

   `ad_id` is the only one that matters. Birdy already knows what ad
   `12021988` is called, so a client mistyping an ad name costs them nothing —
   names are resolved from `facebook_ad_insights` at read time.

3. **Optionally carry `birdy_visitor_id` through the form.** The tracker does
   this by itself where it can (hidden input on same-page forms, appended query
   parameter on Typeform / ROASForm / GHL iframes and links). If the provider
   also writes it into a GHL custom field, the match stops being an inference
   and scores 100.

`GET /attribution/status/{group_id}` backs the onboarding ticks: *tracking
detected*, *first ad click seen*.

## How a lead gets attributed

1. The tracker mints a `visitor_id` (cookie + localStorage, adopted from the
   URL when one is handed along) and POSTs each landing to `/t/collect`.
2. On form submit it scrapes the email/phone and POSTs `/t/identify`. Those
   become `match_keys` — the same normalized `email:` / `phone:` keys already
   stamped on every `ghl_contacts` row.
3. We try to match immediately. Usually there is nothing to match against yet:
   the tracker reports an email in milliseconds and the GHL sync runs hourly.
   The visitor sits in a `pending` queue.
4. `/api/cron/attribution-tick` (every 5 min) drains that queue. It is driven
   from the visitor side, so the cost is proportional to leads waiting, not to
   how many contacts the account has.
5. On a hit it writes `attribution_matches` — first match wins, one row per
   contact.

After ~12 hours of retries a visitor is marked `unmatched`. That is normal, not
a fault: plenty of people fill in a form and never reach the CRM.

## Confidence

| Score | Method | Meaning |
|---|---|---|
| 100 | `visitor_id` | the CRM record carries our own id — nothing inferred |
| 99 | `email+phone` | both deterministic identifiers agree |
| 95 | `email` | |
| 90 | `phone` | shared/typo'd numbers are commoner than shared inboxes |
| — | unattributed | not enough evidence |

There is **no probabilistic or fingerprint matching**, on purpose. These
numbers get shown to the agency's own client; an attribution product that
quietly guesses is worse than one that says "unattributed".

Two other honesty guards:

- `contact_predates_click` flags a contact that already existed before the
  click. The match is kept (same person, real touch) but excluded from
  "leads from this ad" by default.
- Credit goes to the **last paid touch**, falling back to first touch, so an
  organic revisit can't take credit from the ad that paid for it. `first_touch`
  is kept on every row, so a first-click view stays available later.

## The landing-page funnel

Attribution answers *which ad produced this lead*. For a client whose ads point
at a page of their own, the question after that one is where everybody else
went — and that is a step no other report in Birdy can see.

```
ad clicks  →  landing views  →  form started  →  opted in
                    └──────── left without opting in ────────┘
                         before the form  │  inside the form
```

Counted in `landing_page_daily`, one row per (client group, day), incremented
on the way past by the same `/t/*` ingest that records a touch. Each counter is
deduplicated at the source, so the stages can be compared with each other:

| counter | counted | once per |
|---|---|---|
| `pageviews` | every `/t/collect` hit | — |
| `visitors` | browsers never seen before | ever |
| `views` | a visit — the funnel's cohort | browser-day |
| `ad_views` | visits carrying ad identifiers | browser-day |
| `form_starts` | a cursor in a field, or focus into an embedded form | browser-day |
| `opt_ins` | the visit became a lead | browser, ever |

**Why a rollup and not an aggregation over `attribution_visitors`.** Anonymous
visitors carry a TTL; identified ones have theirs unset. Counting a funnel off
that collection therefore *improves as it ages* — six months on, everyone who
bounced has been deleted and everyone who converted is still there, so an old
window reports a 100% opt-in rate. A rollup row is immune to that.

**Why the drop-off is two numbers.** "Left before touching the form" is a page
problem — offer, headline, load speed, an ad promising something the page
doesn't deliver. "Started the form and abandoned it" is a form problem — too
long, asking too much, broken on a phone. They need opposite fixes, and a
single conversion rate hides which one you have. That split is the only reason
the tracker reports a form start at all.

`GET /attribution/funnel/{group_id}` returns `applicable: false` for clients on
Meta Instant Forms (no page of ours to measure) and for clients who have
declared nothing and reported nothing. The Marketing Hub and the client
workspace's Overview both draw the card, and both draw nothing when it says
false — four zeroes under "Landing page" read as a page that is failing rather
than one that was never there.

Existing clients start with an empty funnel; `python -m scripts.backfill_landing_funnel`
reconstructs what it honestly can from the visitors already on file, marks the
rows `backfilled: true`, and under-reports return visits and over-reports the
opt-in rate for exactly the reason above.

## Endpoints

| | |
|---|---|
| `GET /t/{site_id}.js` | the snippet, account baked in, no DB lookup |
| `POST /t/collect` | a landing |
| `POST /t/identify` | an email/phone |
| `POST /t/event` | an engagement signal (today: `form_start`) |
| `GET /attribution/setup/{group_id}` | snippet + Meta parameters + status |
| `GET /attribution/status/{group_id}` | onboarding ticks |
| `GET /attribution/leads-by-ad/{group_id}` | attributed leads per ad |
| `GET /attribution/funnel/{group_id}` | the landing-page funnel for a window |
| `GET /api/cron/attribution-tick` | the match queue |

The `/t/*` endpoints are public and unauthenticated by design. They answer 204
to everything — bad site id, malformed body, dead account — because a public
endpoint that answers differently for real and fake ids is an account oracle,
and because nothing we do may put a red error in a customer's own console.
They also never trigger a CORS preflight: bodies go over as `text/plain` via
`sendBeacon`, which keeps them CORS *simple requests*, so the app's strict
`CORS_ORIGINS` allowlist stays untouched.

## Not built yet

- **Downstream lifecycle.** `attribution_matches` reaches the lead. Bookings,
  shows, opportunities and revenue per ad are a join away (`ghl_contacts` and
  `call_logs` already hold them) but aren't wired into a report yet.
- **Frontend.** No onboarding step or dashboard reads these endpoints yet.
- **Meta CAPI feedback.** Sending qualified leads and real revenue back to Meta
  is the natural next step, and the reason to hold `fbclid` on every match.
- **ROASForm webhooks.** The deep integration wants exactly one thing from
  them: accept `birdy_visitor_id` into a form session and return it unchanged
  on every submission/booking webhook.
