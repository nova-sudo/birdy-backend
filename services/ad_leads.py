"""
services/ad_leads.py
--------------------
The single source of ad-attributed leads.

Every per-ad lead surface in Birdy used to read `facebook_leads` — the Meta
**instant-form** collection. Clients who run their own landing pages have nothing
in it, so they showed spend against 0 leads and a CPL of 0, everywhere: Marketing
Hub, Client Hub, alerts, Ask Birdy, the suggestion engine.

Their leads were never missing. They were in `ghl_contacts` the whole time.
GoHighLevel records ad-level attribution itself on anything submitted through a
GHL-hosted funnel or form, under `contact_data.attributionSource`:

    {"utmSource": "facebook", "sessionSource": "Paid Social", "medium": "facebook",
     "campaign": "SOUP - Body Sculpting - 030925 - CBO", "campaignId": "1202327...",
     "utmMedium": "Celebrity Body Sculpt - Winning", "utmContent": "BS - Evergreen",
     "adId": "120246113041200118", "adSource": "facebook"}

Measured when this was written: 21,400 of 47,775 contacts carried an `adId`, and
21,397 of those had an email, a phone and a `lead_type` of "lead" — against 7,277
instant-form leads in the entire database. Three times more ad-attributed leads
sat in the CRM than in the collection everything read from.

So this module resolves leads from both, normalises them to one shape, and
deduplicates. Two rules worth knowing before changing anything here:

**Dedupe on match_keys.** A client running both instant forms and a GHL funnel
(Konfidence Clinic: 3,020 contacts, 924 instant-form leads) would otherwise count
the same person twice. Both collections already carry `match_keys` — the
normalised email/phone keys from `utils.phone_normalize` — so the overlap is
exact, not fuzzy.

**Trust Meta for identity, GHL for the person.** `attributionSource` has no ad
set *id* at all (`mediumId` is the page id, not the ad set), and its `campaign` /
`utmContent` are free-text names captured at submission time that drift as the
client renames things. So the ad/ad set/campaign identity is looked up from the client group's
`facebook_cache` by ad id, and GHL's strings are used only as a fallback for ads
the cache has no row for.

Adding a third source — the attribution tracker's `attribution_matches`, which
covers contacts GHL itself could not attribute — should be one more `_fetch_*`
function and one more entry in `SOURCE_PRECEDENCE`. See `docs/attribution-next.md`.
"""

from __future__ import annotations

import logging

from core.database import DB_NAME
from core.utils import iso_day_range

logger = logging.getLogger(__name__)


LEAD_SOURCE_INSTANT_FORM = "instant_form"
LEAD_SOURCE_GHL_ATTRIBUTION = "ghl_attribution"

# Which row wins when two sources describe the same person. The instant-form row
# is canonical because it is the only one carrying the form's question answers
# (`field_data`); the GHL contact still contributes opportunity status and
# revenue, which the merge folds in.
SOURCE_PRECEDENCE = (LEAD_SOURCE_INSTANT_FORM, LEAD_SOURCE_GHL_ATTRIBUTION)

# `lead_source` as reported to callers, describing a whole result set.
LEAD_SOURCE_MIXED = "mixed"
LEAD_SOURCE_NONE = "none"

# Meta's own conversion count, used only when no lead row can be resolved.
# Not a source this module can return rows for — a pixel conversion is a number
# Meta reports back, with no person attached. See `pixel_fallback`.
LEAD_SOURCE_PIXEL = "pixel"


def pixel_fallback(resolved_leads: int, meta_results: int) -> tuple[int, str | None]:
    """
    Decide whether Meta's own conversion count should stand in as the lead count.

    Returns `(count, source_override)`; `source_override` is None when the
    resolved count stands.

    A client can run a landing page that reports conversions to Meta through
    their pixel while nothing records the lead anywhere we can read — no Meta
    instant form, no GoHighLevel attribution. Measured on production before this
    existed: one client showed £347.56 of spend, 190 pixel conversions, and a
    lead count of 0 with a CPL of 0. The 190 was already in the cache, one field
    away from the column reading zero.

    Only ever a fallback, never a supplement. A pixel conversion and a lead row
    are different objects — the pixel can double-count a refresh and miss a
    blocked browser, and only one of them has a person behind it — so adding
    them would produce a number that is neither. When rows resolve, they win,
    and the difference between the two is the coverage gap rather than more
    leads.
    """
    if resolved_leads > 0:
        return resolved_leads, None
    if meta_results > 0:
        return meta_results, LEAD_SOURCE_PIXEL
    return 0, None

# A GHL contact counts as an ad-attributed lead only if GHL captured an ad id for
# it. Contacts without one are real contacts, but nothing can say which ad they
# came from, so they are not leads for a per-ad view.
#
# Spelled `$gt: ""` rather than the more obvious `$nin: [None, ""]` so that the
# partial index below can actually serve it. A partial index is only used when
# the planner can prove the query matches a subset of what was indexed, and it
# cannot prove that through a negation like `$nin`. `$gt: ""` matches exactly the
# non-empty strings — nulls, missing fields and other BSON types all sort outside
# it — and is a plain range predicate the planner handles.
_HAS_AD_ID = {"contact_data.attributionSource.adId": {"$gt": ""}}

# Priority order for picking the one opportunity that represents a contact.
# Matches the rule already used by /api/facebook-leads/filtered and
# /api/campaigns/opp-rollup — a contact with both a won and an open opportunity
# is a win.
_OPP_PRIORITY = ("won", "open", "lost", "abandoned")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def primary_opportunity(opportunities) -> tuple[str, float]:
    """Return (status, monetary_value) for the opportunity that represents a contact."""
    if not opportunities:
        return "", 0.0
    for status in _OPP_PRIORITY:
        match = next((o for o in opportunities if (o or {}).get("status") == status), None)
        if match:
            try:
                value = float(match.get("monetaryValue") or 0)
            except (TypeError, ValueError):
                value = 0.0
            return status, value
    return "", 0.0


def _group_filter(user_id: str, group_ids: list[str] | None) -> dict:
    query: dict = {"user_id": user_id}
    if group_ids:
        query["client_group_id"] = {"$in": group_ids}
    return query


def _instant_form_name(lead_data: dict) -> str:
    """
    A name for an instant-form lead, falling back to the form's own answers.

    Meta only fills `full_name` when the form asked for a full name. Forms that
    ask for a first name (and optionally a surname) separately leave it empty
    and put the answers in `field_data` under whatever the question was labelled
    — every one of one real account's 924 leads is like this. Left alone, those
    rows show a blank name column next to GHL-sourced rows that have one.

    Question labels are free text, so this matches on the label containing the
    word rather than equalling it.
    """
    name = (lead_data.get("full_name") or "").strip()
    if name:
        return name

    answers = lead_data.get("field_data") or {}
    if not isinstance(answers, dict):
        return ""

    def _find(*words: str) -> str:
        for label, value in answers.items():
            lowered = str(label).lower()
            if any(word in lowered for word in words) and value:
                return str(value).strip()
        return ""

    full = _find("full name", "full_name")
    if full:
        return full
    return f"{_find('first name', 'first_name', 'firstname')} {_find('last name', 'last_name', 'lastname', 'surname')}".strip()


def _contact_name(contact_data: dict) -> str:
    first = (contact_data.get("firstName") or "").strip()
    last = (contact_data.get("lastName") or "").strip()
    full = f"{first} {last}".strip()
    return full or (contact_data.get("contactName") or contact_data.get("name") or "").strip()


def describe_lead_source(rows: list[dict]) -> str:
    """Summarise which sources a result set actually came from."""
    sources = {row.get("lead_source") for row in rows}
    sources.discard(None)
    if not sources:
        return LEAD_SOURCE_NONE
    if len(sources) == 1:
        return sources.pop()
    return LEAD_SOURCE_MIXED


# ---------------------------------------------------------------------------
# Source: Meta instant forms (facebook_leads)
# ---------------------------------------------------------------------------

async def _fetch_instant_form_leads(
    db, user_id: str, group_ids: list[str] | None, date_range: dict, limit: int | None
) -> list[dict]:
    query = _group_filter(user_id, group_ids)
    if date_range:
        query["lead_data.created_time"] = date_range

    cursor = db["facebook_leads"].find(
        query,
        {
            "lead_data": 1, "match_keys": 1,
            "client_group_id": 1, "client_group_name": 1, "ad_account_id": 1,
        },
    ).sort("lead_data.created_time", -1)
    if limit:
        cursor = cursor.limit(limit)

    rows = []
    for doc in await cursor.to_list(length=limit):
        lead = doc.get("lead_data") or {}
        rows.append({
            "lead_source": LEAD_SOURCE_INSTANT_FORM,
            "lead_id": lead.get("id"),
            "full_name": _instant_form_name(lead),
            "email": lead.get("email") or "",
            "phone_number": lead.get("phone_number") or "",
            "created_time": lead.get("created_time") or "",
            "ad_id": lead.get("ad_id") or "",
            "ad_name": lead.get("ad_name") or "",
            "adset_id": lead.get("adset_id") or "",
            "adset_name": lead.get("adset_name") or "",
            "campaign_id": lead.get("campaign_id") or "",
            "campaign_name": lead.get("campaign_name") or "",
            "platform": lead.get("platform") or "",
            "field_data": lead.get("field_data") or {},
            "match_keys": doc.get("match_keys") or [],
            "client_group_id": doc.get("client_group_id"),
            "group_name": doc.get("client_group_name") or "Unknown Group",
            "ad_account_id": doc.get("ad_account_id"),
            # Filled in by the GHL enrichment pass.
            "ghl_matched": False,
            "ghl_contact_id": None,
            "ghl_tags": [],
            "ghl_opportunity_status": "",
            "ghl_opportunity_value": 0,
            "ghl_date_added": "",
        })
    return rows


# ---------------------------------------------------------------------------
# Source: GoHighLevel's own attribution (ghl_contacts)
# ---------------------------------------------------------------------------

async def _fetch_ghl_attributed_leads(
    db, user_id: str, group_ids: list[str] | None, date_range: dict, limit: int | None
) -> list[dict]:
    query = {**_group_filter(user_id, group_ids), **_HAS_AD_ID}
    if date_range:
        query["contact_data.dateAdded"] = date_range

    cursor = db["ghl_contacts"].find(
        query,
        {
            "contact_id": 1, "match_keys": 1,
            "client_group_id": 1, "client_group_name": 1,
            "contact_data.attributionSource": 1,
            "contact_data.firstName": 1, "contact_data.lastName": 1,
            "contact_data.contactName": 1, "contact_data.name": 1,
            "contact_data.email": 1, "contact_data.phone": 1,
            "contact_data.dateAdded": 1, "contact_data.tags": 1,
            "contact_data.opportunities": 1,
        },
    ).sort("contact_data.dateAdded", -1)
    if limit:
        cursor = cursor.limit(limit)

    rows = []
    for doc in await cursor.to_list(length=limit):
        contact = doc.get("contact_data") or {}
        attribution = contact.get("attributionSource") or {}
        status, value = primary_opportunity(contact.get("opportunities"))
        date_added = contact.get("dateAdded") or ""

        rows.append({
            "lead_source": LEAD_SOURCE_GHL_ATTRIBUTION,
            "lead_id": doc.get("contact_id"),
            "full_name": _contact_name(contact),
            "email": contact.get("email") or "",
            "phone_number": contact.get("phone") or "",
            "created_time": date_added,
            "ad_id": attribution.get("adId") or "",
            # GHL's captured names, kept only as a fallback — the Meta enrichment
            # pass overwrites these wherever it has a row for the ad.
            "ad_name": attribution.get("utmContent") or "",
            "adset_id": "",
            "adset_name": attribution.get("utmMedium") or "",
            "campaign_id": attribution.get("campaignId") or "",
            "campaign_name": attribution.get("campaign") or "",
            "platform": attribution.get("medium") or attribution.get("adSource") or "",
            # Instant-form leads carry the form's question answers here. A GHL
            # contact has no equivalent, and inventing one would make an empty
            # column look like an unanswered question.
            "field_data": {},
            "match_keys": doc.get("match_keys") or [],
            "client_group_id": doc.get("client_group_id"),
            "group_name": doc.get("client_group_name") or "Unknown Group",
            "ad_account_id": None,
            # This lead *is* a GHL contact, so the CRM columns are known already
            # rather than needing the match the instant-form rows go through.
            "ghl_matched": True,
            "ghl_contact_id": doc.get("contact_id"),
            "ghl_tags": contact.get("tags") or [],
            "ghl_opportunity_status": status,
            "ghl_opportunity_value": value,
            "ghl_date_added": date_added,
        })
    return rows


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

def _entity_index(facebook_cache: dict) -> dict[str, dict]:
    """
    Build `{kind: {id: entity}}` from a group's Meta cache.

    The cache is mid-migration to a split shape (see
    services/facebook_cache_shape.py): identity lives once under
    `facebook_cache.entities`, and the legacy flat `facebook_cache.ads` /
    `.adsets` / `.campaigns` are still written alongside it. Neither is reliably
    populated on its own — production has groups with 53 ads in the flat lists
    and an empty `entities` — so take whichever has rows.
    """
    entities = facebook_cache.get("entities") or {}
    index: dict[str, dict] = {}
    for kind in ("ads", "adsets", "campaigns"):
        rows = entities.get(kind) or facebook_cache.get(kind) or []
        index[kind] = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}
    return index


async def _enrich_ad_identity(db, user_id: str, rows: list[dict]) -> None:
    """
    Fill ad set / campaign ids and current names from Meta's own view, in place.

    GoHighLevel captures the ad id reliably but has no ad set id at all, and the
    campaign and ad *names* it stores are whatever they were called at the
    moment the lead came in. Meta is the authority for both.

    That view lives on the client group's `facebook_cache`, not in
    `facebook_ad_insights` — that collection is empty in production, and the
    per-ad rows the Marketing Hub renders come from the cache. Reading the wrong
    one silently left every ad set id blank, which is a per-ad-set rollup that
    quietly reports nothing.
    """
    group_ids = {row["client_group_id"] for row in rows if row.get("client_group_id")}
    if not group_ids:
        return

    index: dict[str, dict[str, dict]] = {}
    async for group in db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": list(group_ids)}},
        {"id": 1, "facebook_cache": 1, "_id": 0},
    ):
        index[group["id"]] = _entity_index(group.get("facebook_cache") or {})

    for row in rows:
        entities = index.get(row.get("client_group_id"))
        if not entities:
            continue

        ad = entities["ads"].get(row.get("ad_id"))
        if ad:
            if ad.get("name"):
                row["ad_name"] = ad["name"]
            if ad.get("adset_id"):
                row["adset_id"] = ad["adset_id"]
            if ad.get("campaign_id"):
                row["campaign_id"] = ad["campaign_id"]

        adset = entities["adsets"].get(row.get("adset_id"))
        if adset and adset.get("name"):
            row["adset_name"] = adset["name"]

        campaign = entities["campaigns"].get(row.get("campaign_id"))
        if campaign and campaign.get("name"):
            row["campaign_name"] = campaign["name"]


async def _enrich_ghl_contact(db, user_id: str, rows: list[dict]) -> None:
    """
    Attach CRM state to instant-form leads, in place.

    Only instant-form rows need this — a GHL-attributed lead already is the
    contact. Same match_keys join /api/facebook-leads/filtered has always done,
    lifted here so both sources come out of the resolver in one shape.
    """
    pending = [r for r in rows if r["lead_source"] == LEAD_SOURCE_INSTANT_FORM]
    if not pending:
        return

    keys = {k for row in pending for k in row["match_keys"]}
    if not keys:
        return

    by_key: dict[str, dict] = {}
    cursor = db["ghl_contacts"].find(
        {"user_id": user_id, "match_keys": {"$in": list(keys)}},
        {
            "contact_id": 1, "match_keys": 1,
            "contact_data.tags": 1, "contact_data.opportunities": 1,
            "contact_data.dateAdded": 1,
        },
    )
    async for doc in cursor:
        contact = doc.get("contact_data") or {}
        status, value = primary_opportunity(contact.get("opportunities"))
        enrichment = {
            "ghl_matched": True,
            "ghl_contact_id": doc.get("contact_id"),
            "ghl_tags": contact.get("tags") or [],
            "ghl_opportunity_status": status,
            "ghl_opportunity_value": value,
            "ghl_date_added": contact.get("dateAdded") or "",
        }
        for key in (doc.get("match_keys") or []):
            # First contact wins, matching the primary-match rule the leads tab
            # has always used.
            by_key.setdefault(key, enrichment)

    for row in pending:
        for key in row["match_keys"]:
            found = by_key.get(key)
            if found:
                row.update(found)
                break


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------

def _dedupe(rows: list[dict]) -> list[dict]:
    """
    Collapse rows describing the same person, keeping the higher-precedence one.

    A client with both an instant form and a GHL funnel produces two rows for one
    lead. `match_keys` makes the overlap exact — same normalised email or phone —
    so this is a set intersection, not a guess.

    A row with no match keys at all (no usable email or phone) cannot be
    compared with anything, so it is always kept: dropping it would silently
    lose a real lead.
    """
    rank = {source: i for i, source in enumerate(SOURCE_PRECEDENCE)}
    claimed: dict[str, int] = {}   # match key → index of the row holding it
    kept: list[dict | None] = []

    for row in sorted(rows, key=lambda r: rank.get(r["lead_source"], len(rank))):
        keys = row.get("match_keys") or []
        existing = next((claimed[k] for k in keys if k in claimed), None)

        if existing is None:
            kept.append(row)
            index = len(kept) - 1
            for key in keys:
                claimed.setdefault(key, index)
            continue

        # Same person, lower-precedence source. Keep the winning row but take
        # any CRM state it is missing — an instant-form lead has no opportunity
        # status of its own until the GHL join fills it in.
        winner = kept[existing]
        if winner and not winner.get("ghl_matched") and row.get("ghl_matched"):
            for field in (
                "ghl_matched", "ghl_contact_id", "ghl_tags",
                "ghl_opportunity_status", "ghl_opportunity_value", "ghl_date_added",
            ):
                winner[field] = row[field]

    return [row for row in kept if row]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def fetch_ad_leads(
    user_id: str,
    group_ids: list[str] | None,
    start_date: str | None,
    end_date: str | None,
    mongo_client,
    limit: int | None = None,
) -> list[dict]:
    """
    Every ad-attributed lead in the window, newest first, deduplicated.

    Raises ValueError on a malformed date (via `iso_day_range`) — callers turn
    that into a 400 rather than silently widening the window.
    """
    db = mongo_client[DB_NAME]
    date_range = iso_day_range(start_date, end_date)

    # Each source is limited independently, then the merged set is trimmed. Both
    # sources sort newest-first, so the rows dropped here are older than every
    # row kept.
    instant = await _fetch_instant_form_leads(db, user_id, group_ids, date_range, limit)
    ghl = await _fetch_ghl_attributed_leads(db, user_id, group_ids, date_range, limit)

    rows = _dedupe(instant + ghl)
    await _enrich_ghl_contact(db, user_id, rows)
    await _enrich_ad_identity(db, user_id, rows)

    rows.sort(key=lambda r: r.get("created_time") or "", reverse=True)
    return rows[:limit] if limit else rows


async def _fetch_count_rows(
    db, user_id: str, group_ids: list[str] | None, date_range: dict
) -> list[dict]:
    """
    The minimum needed to count: a date and the keys that identify a person.

    Deliberately not `fetch_ad_leads` — a chart over "all time" would otherwise
    pull every lead body, its tags and its opportunities into memory to throw
    all of it away. This stays a projection scan.
    """
    rows: list[dict] = []

    fb_query = _group_filter(user_id, group_ids)
    if date_range:
        fb_query["lead_data.created_time"] = date_range
    async for doc in db["facebook_leads"].find(
        fb_query, {"lead_data.created_time": 1, "match_keys": 1, "_id": 0}
    ):
        rows.append({
            "lead_source": LEAD_SOURCE_INSTANT_FORM,
            "match_keys": doc.get("match_keys") or [],
            "created_time": (doc.get("lead_data") or {}).get("created_time") or "",
        })

    ghl_query = {**_group_filter(user_id, group_ids), **_HAS_AD_ID}
    if date_range:
        ghl_query["contact_data.dateAdded"] = date_range
    async for doc in db["ghl_contacts"].find(
        ghl_query, {"contact_data.dateAdded": 1, "match_keys": 1, "_id": 0}
    ):
        rows.append({
            "lead_source": LEAD_SOURCE_GHL_ATTRIBUTION,
            "match_keys": doc.get("match_keys") or [],
            "created_time": (doc.get("contact_data") or {}).get("dateAdded") or "",
        })

    return _dedupe(rows)


async def fetch_lead_timeline(
    user_id: str,
    group_ids: list[str] | None,
    mongo_client,
) -> list[dict]:
    """
    Every ad-attributed lead as `{created_time, lead_source}`, deduplicated.

    For callers that need to count the same set against several date windows —
    the preset cache writes thirteen of them. Reading once and bucketing in
    memory keeps that to two queries instead of twenty-six.
    """
    db = mongo_client[DB_NAME]
    rows = await _fetch_count_rows(db, user_id, group_ids, {})
    return [
        {"created_time": row["created_time"], "lead_source": row["lead_source"]}
        for row in rows
    ]


def count_in_window(timeline: list[dict], start_iso: str | None, end_iso: str | None) -> int:
    """
    Leads from a `fetch_lead_timeline` result that fall inside a date window.

    Compares the first ten characters — the calendar date — against bounds that
    are already `YYYY-MM-DD`. Both sources timestamp in UTC (Meta's
    `created_time` ends `+0000`, GHL's `dateAdded` ends `Z`), so the date slice
    is the UTC day, which is what the preset bounds mean.
    """
    if start_iso is None and end_iso is None:
        return len(timeline)
    total = 0
    for row in timeline:
        day = (row.get("created_time") or "")[:10]
        if not day:
            continue
        if start_iso and day < start_iso:
            continue
        if end_iso and day > end_iso:
            continue
        total += 1
    return total


async def count_ad_leads_by_day(
    user_id: str,
    group_ids: list[str] | None,
    start_date: str | None,
    end_date: str | None,
    mongo_client,
) -> dict[str, int]:
    """Leads per calendar day, deduplicated. `{"2026-09-01": 12, ...}`"""
    db = mongo_client[DB_NAME]
    rows = await _fetch_count_rows(
        db, user_id, group_ids, iso_day_range(start_date, end_date)
    )

    by_day: dict[str, int] = {}
    for row in rows:
        day = (row.get("created_time") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    return by_day


async def ad_leads_series(
    user_id: str,
    group_ids: list[str] | None,
    start_date: str | None,
    end_date: str | None,
    mongo_client,
) -> list[dict]:
    """
    One bucket per day: leads that arrived, and how many of them closed.

    `closes` counts leads whose person holds a won opportunity in the CRM. The
    won-key set is gathered once for the whole account rather than joined per
    lead — an instant-form lead's contact need not itself be ad-attributed, so
    the lookup has to span every contact, not just the ones this resolver
    treats as leads.
    """
    db = mongo_client[DB_NAME]
    rows = await _fetch_count_rows(
        db, user_id, group_ids, iso_day_range(start_date, end_date)
    )

    won_keys: set[str] = set()
    async for doc in db["ghl_contacts"].find(
        {"user_id": user_id, "contact_data.opportunities.status": "won"},
        {"match_keys": 1, "_id": 0},
    ):
        won_keys.update(doc.get("match_keys") or [])

    buckets: dict[str, dict] = {}
    for row in rows:
        day = (row.get("created_time") or "")[:10]
        if not day:
            continue
        bucket = buckets.setdefault(day, {"date": day, "leads": 0, "closes": 0})
        bucket["leads"] += 1
        if any(key in won_keys for key in row.get("match_keys") or []):
            bucket["closes"] += 1

    return [buckets[day] for day in sorted(buckets)]


async def count_ad_leads(
    user_id: str,
    group_ids: list[str] | None,
    start_date: str | None,
    end_date: str | None,
    mongo_client,
) -> int:
    """Total ad-attributed leads in the window, deduplicated."""
    by_day = await count_ad_leads_by_day(
        user_id, group_ids, start_date, end_date, mongo_client
    )
    return sum(by_day.values())


# ---------------------------------------------------------------------------
# Indexes (registered in core/indexes.py)
# ---------------------------------------------------------------------------

async def create_ad_leads_indexes(mongo_client):
    """
    The index behind every GHL-attributed lead query.

    Partial on the ad id, because only about 45% of contacts carry one — that
    keeps the index to the rows a per-ad view can ever return rather than all of
    `ghl_contacts`, which is the largest collection in the system and already
    its biggest source of scanned documents.

    The partial expression and `_HAS_AD_ID` must stay spelled the same way; see
    the comment there for why it is `$gt: ""`.
    """
    await mongo_client[DB_NAME]["ghl_contacts"].create_index(
        [("client_group_id", 1), ("contact_data.dateAdded", -1)],
        name="group_ad_attributed_leads",
        partialFilterExpression={"contact_data.attributionSource.adId": {"$gt": ""}},
        background=True,
    )
    logger.info("✅ Created ad-attributed lead index on ghl_contacts")
