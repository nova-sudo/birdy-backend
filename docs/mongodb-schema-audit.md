# MongoDB Schema & Performance Audit — birdy-backend

**Date:** 2026-08-22 · **Cluster:** MongoDB 8.0.29 · **DB:** `birdyaidev`
**Method:** live `collStats`, `$bsonSize`, `$indexStats`, and `explain("executionStats")` against the running cluster, cross-read against `routers/` + `services/`.

> ⚠️ All measurements are from `birdyaidev`. Prod volumes are likely larger, so every number below is a **floor**, not a ceiling.

---

## TL;DR

The dashboard is slow for **two** reasons, and they compound:

1. **`client_groups` documents are enormous** — 784 KB average, 7.45 MB worst case. The Clients page fetches 67 of them in one query: **13.5 MB and 15.3 seconds of wire time**, even *with* the projection already in place.
2. **Every Clients page load full-scans `ghl_contacts`** (180,105 docs / 478 MB) to build the tag breakdown: **1,530 ms, 180,106 documents examined**.

Neither is fixable with indexes. Both are schema problems. The single highest-leverage change is **de-duplicating `facebook_cache`**, which alone should cut `client_groups` by ~90%.

There is also a **latent outage**: the largest `client_groups` document is at **47% of MongoDB's hard 16 MB limit** and grows every day. When it crosses 16 MB, writes to that client fail permanently.

---

## Measured baseline

| Collection | Docs | Data | Index | Avg doc |
|---|---:|---:|---:|---:|
| `ghl_contacts` | 180,105 | **477.8 MB** | 63.0 MB | 2.6 KB |
| `hotprospector_leads` | 54,792 | 127.6 MB | 5.5 MB | 2.3 KB |
| `call_logs` | 103,260 | 109.5 MB | 7.8 MB | 1.0 KB |
| **`client_groups`** | **76** | **59.6 MB** | 0.1 MB | **783,883 B** |
| `meta_refresh_jobs` | 27,791 | 35.6 MB | 2.5 MB | 1.3 KB |
| `facebook_leads` | 36,813 | 35.2 MB | 9.3 MB | 1.0 KB |

76 documents occupying 59.6 MB is the outlier that explains almost everything.

---

## Findings, ranked

### F1 — `facebook_cache` duplicates the entire ad inventory across 13 date presets — CRITICAL

`facebook_cache` has 22 keys, 13 of which are date-preset buckets: `today`, `yesterday`, `this_week_mon_today`, `last_7d`, `last_14d`, `last_30d`, `this_month`, `this_quarter`, `this_year`, `last_month`, `last_quarter`, `last_year`, `maximum`.

**Each bucket is ~665 KB and contains a full copy of the same entity lists:**

```
per-preset bucket "last_30d":
   0.515 MB  ads         list len=489
   0.075 MB  campaigns   list len=338
   0.074 MB  adsets      list len=349
   0.000 MB  metrics     dict len=4
   0.000 MB  date_preset str
```

Verified identical across buckets — same 489 ad IDs, same hash, in every preset:

```
last_7d      ads= 489 idhash=afde97a92956
last_30d     ads= 489 idhash=afde97a92956
this_year    ads= 489 idhash=afde97a92956
maximum      ads= 489 idhash=afde97a92956
```

Only the per-entity `metrics` numbers differ. Every ad's name, id, campaign_id, adset_id and creative fields are stored **13 times**.

**Impact:** 13 × 0.664 MB ≈ 8.6 MB of near-pure redundancy in the worst document. Largest doc = **7.45 MB / 16 MB hard limit (47%)**; 13 documents already exceed 1 MB.

**Fix:** split identity from metrics.

```jsonc
// before — entity identity repeated 13x
facebook_cache: {
  last_30d:  { ads: [ {ad_id, name, ..., metrics} x489 ] },
  this_year: { ads: [ ...the same 489...              ] },
  ...
}

// after — identity once, metrics keyed by id per preset
facebook_cache: {
  entities: { ads: [ {ad_id, name, campaign_id, adset_id} x489 ] },        // ~0.1 MB, once
  presets:  { last_30d: { ads: { "<ad_id>": {spend, leads, impressions} } } }  // small
}
```

Expected: **7.45 MB → ~0.7 MB (≈90% reduction)**, and the 16 MB cliff disappears.

---

### F2 — Clients page fetches 13.5 MB / 15.3 s in one query — CRITICAL

`routers/client_groups.py:186-210` — the list endpoint fetches every group for the user. Measured on one real user (67 groups):

```
Clients-page group fetch: 67 groups, 13.51 MB projected payload, 15317 ms wire time
  (unprojected would be:                59.56 MB,               24812 ms)
```

The projection at `routers/client_groups.py:178` is **already doing its job** — it is why this is 13.5 MB and not 59.6 MB. The problem is that even the projected fields are huge, because it requests `facebook_cache.campaigns` + `.adsets` + `.ads` + a full preset bucket + `ghl_daily_leads` + `meta_daily_spend` + `hp_daily_calls` for **all 67 groups at once**.

Also note: a projection reduces *network* bytes, not WiredTiger read cost — Mongo still reads and parses the full 7.45 MB document before projecting.

**Fix:** F1 removes most of this mechanically. Beyond that, campaigns/adsets/ads are **per-client drill-down data** and do not belong on a list endpoint — move them behind `GET /client-groups/{id}`.

---

### F3 — Every Clients page load full-scans `ghl_contacts` — CRITICAL

`routers/client_groups.py:132-153` — the `$facet` tag-breakdown branch. Measured:

```
$match + $unwind tags + $group:
  ms=1530  returned=16723  keysExamined=180106  docsExamined=180106
```

**180,106 documents examined — that is 100% of the collection**, 478 MB, on every page load. The `$unwind` on `contact_data.tags` forces a FETCH of every document because no index covers the tags array.

The counts branch is milder but still wrong-shaped:

```
$match + $group by client_group_id:
  ms=18  returned=62  keysExamined=16791  docsExamined=0
```

16,791 keys for 62 results. `idx_ghl_date` is `(user_id, client_group_id, contact_data.dateAdded)` — the query filters `user_id` + `dateAdded` but **not** `client_group_id`, which sits in the middle. That violates ESR (Equality → Sort → Range) and degrades to a generic multi-interval scan.

**Fix:**
- Add `{user_id: 1, "contact_data.dateAdded": 1, client_group_id: 1}` — makes the counts branch a single contiguous range scan.
- The tag breakdown should **not** be computed live. It is already cached elsewhere (`gohighlevel_cache.metrics.tag_breakdown`) — precompute it per preset on the refresh job, exactly the way `ghl_opp_cache` and `ghl_funnel_cache` already work.

---

### F4 — `contact_data.dateAdded` is stored as a string, not a Date — HIGH

100% of a 3,000-doc sample: `dateAdded BSON types: [{'_id': 'string', 'n': 3000}]` — e.g. `'2025-12-14T21:30:35.429Z'`.

Queried by string range at `routers/client_groups.py:126-129`:

```python
"contact_data.dateAdded": {
    "$gte": f"{ghl_start}T00:00:00.000Z",
    "$lte": f"{ghl_end}T23:59:59.999Z",
}
```

This only works while **every** value is UTC with identical formatting. Any record written with a `+02:00` offset, without milliseconds, or with a space separator sorts wrong and silently drops out of the range. It also costs roughly 2x the bytes of a BSON date and blocks `$dateTrunc` / timezone-aware bucketing.

**This is also a prime suspect for the cross-page number mismatches** — flagged to the data-accuracy investigation.

**Fix:** migrate to BSON `Date` via dual-write + backfill, then swap the query to real date objects.

---

### F5 — Unbounded daily arrays inside `client_groups` — HIGH

| Array | Max len | Avg len | Total elements |
|---|---:|---:|---:|
| `ghl_daily_leads` | **1,118** | 419 | 31,822 |
| `meta_daily_spend` | 401 | 237 | 18,010 |
| `hp_daily_calls` | 71 | 32 | 2,415 |

These grow one element per day forever, with no cap and no TTL. `ghl_daily_leads` at 1,118 entries is already 3 years of daily history embedded in a document that gets re-serialized on every write (`services/ghl_daily_leads.py:110`).

Textbook unbounded-array anti-pattern.

**Fix:** either cap to the retained window (e.g. last 400 days via `$slice` on push) or move to a `client_group_daily` time-series collection keyed `{group_id, date}`.

---

### F6 — `call_logs` is 109 MB of never-read data — MEDIUM

`$indexStats` since 2026-08-10:

```
call_logs:
   source_event_unique   ops=0
   received_at_desc      ops=0
   location_started_at   ops=0
   user_started_at       ops=0
   _id_                  ops=2
```

**Every index has zero reads**, and `_id_` has 2. The Sales-Hub was moved to read from the daily cache (`hotprospector_member_daily`), so this collection is now effectively write-only.

Meanwhile each document stores `raw_payload` (the full webhook body) **and** `headers` forever — `services/call_logs_service.py:165-172`. That is the bulk of the 109 MB. `_serialize` (`routers/call_logs.py:47-65`) correctly strips them on read, so this is storage and backup cost, not response bloat.

**Fix:** TTL index on `received_at` (e.g. 90 days) for the raw fields, or move `raw_payload`/`headers` to a cold `call_logs_raw` collection. Keep `source_event_unique` — webhook idempotency needs it even though it serves no reads. Confirm the Sales-Hub genuinely no longer reads this before dropping the read indexes.

---

### F7 — `meta_refresh_jobs` has no retention — MEDIUM

27,791 docs / 35.6 MB of job records, no TTL. `idx_mrj_group_created` has **0 ops**.

**Fix:** TTL on `created_at` (30 days); drop `idx_mrj_group_created`.

---

### F8 — `hotprospector_leads` hot index has no date component — MEDIUM

```
user_id_1_ghl_location_id_1   ops=889,856   <-- by far the hottest index in the DB
idx_hpl_user_loc_created      ops=8,133
```

889 K ops on a two-field equality index with no sort or range component, against 54,792 docs. This is almost certainly "fetch all leads for a location, then filter in Python" (`services/ghl_service.py:158`, `services/hp_service.py:294`).

**Fix:** push the date filter into the query so it uses the existing `idx_hpl_user_loc_created` — the right index already exists and is barely used.

---

### F9 — `users.integrations` is 273 KB in a single document — LOW

7 users, 40 KB average, one `integrations` subdocument at 272.7 KB. There is also a multikey index on `integrations.facebook.accounts`. Fine at 7 users; revisit before scaling.

---

### F10 — Unused indexes — LOW

| Index | Ops |
|---|---:|
| `call_logs.*` (4 read indexes) | 0 |
| `meta_refresh_jobs.idx_mrj_group_created` | 0 |
| `ghl_contacts.idx_lead_type` | 2 |

Small win, but they cost write amplification on two of the highest-write collections.

---

## Recommended order of work

| # | Change | Fixes | Effort | Payoff |
|---|---|---|---|---|
| 1 | Split `facebook_cache` identity from per-preset metrics | F1, F2 | M | **~90% smaller `client_groups`**; removes 16 MB cliff |
| 2 | Precompute tag breakdown into `gohighlevel_cache` per preset | F3 | M | −1,530 ms per page load |
| 3 | Add `{user_id, contact_data.dateAdded, client_group_id}` index | F3 | **S** | 16,791 → ~62 keys examined |
| 4 | Move campaigns/adsets/ads off the list endpoint to `/{id}` | F2 | S | Large payload cut |
| 5 | Cap or externalize the daily arrays | F5 | M | Stops unbounded growth |
| 6 | TTL `call_logs` raw fields + `meta_refresh_jobs` | F6, F7 | S | ~120 MB reclaimed |
| 7 | Migrate `dateAdded` to BSON Date | F4 | **L** | Correctness + timezone bucketing |
| 8 | Push date filter into `hotprospector_leads` queries | F8 | S | Uses the index that already exists |
| 9 | Drop unused indexes | F10 | S | Less write amplification |

**Item 3 is a one-line index and should ship first** — smallest change, immediately measurable.

Items 1 and 2 are where the real wins are, and both are refresh-job changes rather than read-path rewrites, so they can ship without touching the frontend.

---

## Not done here

- **No writes were performed.** No indexes created, no documents modified. Every recommendation above needs approval before execution.
- Atlas Performance Advisor and slow-query logs were not consulted (requires Atlas M10+ API credentials). Configuring the MongoDB Atlas MCP would add real slow-query evidence on top of this static + explain-based analysis.
- Measurements are from `birdyaidev`. Re-run against prod before sizing the migration.
