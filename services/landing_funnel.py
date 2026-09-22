"""
services/landing_funnel.py
--------------------------
What happens on a landing page *before* anyone becomes a lead.

Attribution answers "which ad produced this lead". It cannot answer the
question a media buyer asks next, which is the one that actually moves money:
of everyone the ad sent to the page, how many opted in — and where did the rest
go? A client on Meta Instant Forms never needs this; Meta's own funnel ends at
the form. A client on their own landing page has a step in the middle that
nobody has ever been able to see, and it is usually the leaking one.

So for clients running their own page with our script on it, this counts the
whole way down:

    ad clicks   →   landing views   →   form started   →   opted in

and reports the drop-off between the last two as two different failures, which
is the point of the whole exercise:

    left before touching the form    the page did not sell it
    started the form, abandoned it   the form did

Those need opposite fixes — one is a copy/offer problem, the other is a form
problem — and a single "conversion rate" hides which one you have.

── Why a rollup collection and not an aggregation over the visitors ──────────
`attribution_visitors` looks like it already holds all of this, and for a short
window it does. But anonymous visitors carry a TTL (see VISITOR_TTL_DAYS) while
identified ones have theirs unset the moment they hand over an email. Counting
a funnel off that collection therefore reads *better and better as it ages*:
six months on, every visitor who bounced has been deleted and every visitor who
converted is still there, so an old window reports a 100% opt-in rate. Numbers
that improve because the evidence expired are worse than no numbers.

One row per (client group, day), incremented on the way past, is immune to
that. It also costs one write per hit on a public firehose, which is why every
counter below is deduplicated at the source rather than being a raw event log
we would have to aggregate later.

── What each counter counts ─────────────────────────────────────────────────
    pageviews    every /t/collect hit. The only raw one.
    visitors     browsers seen for the first time ever.
    views        visitor-*days*: one per browser per day, however many pages
                 they read. This is the funnel's cohort — a "visit".
    ad_views     of those visits, the ones that arrived carrying ad
                 identifiers. Deliberately NOT Meta's link-click number: a
                 person who clicks the same ad twice in a day is one, and
                 anyone whose browser dropped the parameters is none.
    form_starts  visitor-days where someone put a cursor in a form field, or
                 focused/clicked through to an embedded form. Once per day.
    opt_ins      visitors who became a lead, counted once ever — the same
                 person filling the form again in March is not a second
                 opt-in, exactly as `tracked_leads` dedupes them.

A webhook lead with no visitor id (an external form on a page with no script)
is not counted here at all. It has no view either, and a stage that counts
people the stage above it never saw is not a funnel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from core.database import DB_NAME

logger = logging.getLogger(__name__)


LANDING_DAILY = "landing_page_daily"

# Every counter a day row carries, in funnel order where that applies. Listed
# once so the reader, the backfill and the zero-filled empty window can't drift
# apart.
COUNTERS = ("pageviews", "visitors", "views", "ad_views", "form_starts", "opt_ins")


def day_of(ts: datetime | None = None) -> str:
    """The rollup's day key: an ISO date string, as ghl_daily_leads uses."""
    return (ts or datetime.utcnow()).strftime("%Y-%m-%d")


def _same_day(ts: datetime | None, day: str) -> bool:
    return bool(ts) and day_of(ts) == day


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

async def bump(site: dict, mongo_client, day: str | None = None, **counters: int) -> None:
    """
    Add to one day's counters for one client.

    Called from the public ingest path, so it never raises: a rollup that fails
    must not turn into an error inside a stranger's browser on a landing page we
    don't own. A lost increment costs a number; a thrown exception costs the
    lead behind it.
    """
    increments = {key: value for key, value in counters.items() if value and key in COUNTERS}
    if not increments or not site.get("client_group_id"):
        return

    day = day or day_of()
    now = datetime.utcnow()
    try:
        await mongo_client[DB_NAME][LANDING_DAILY].update_one(
            {"client_group_id": site["client_group_id"], "day": day},
            {
                "$inc": increments,
                "$setOnInsert": {
                    "user_id": site.get("user_id"),
                    "site_id": site.get("site_id"),
                    "created_at": now,
                },
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
    except Exception as e:
        logger.error(
            "landing rollup failed for group %s on %s: %s",
            site.get("client_group_id"), day, e,
        )


async def record_view(
    site: dict,
    visitor_before: dict | None,
    mongo_client,
    now: datetime | None = None,
    paid: bool = False,
) -> None:
    """
    Fold one landing into the day's counters.

    `visitor_before` is the visitor document **as it was before this hit** —
    None when the browser had never been seen. That pre-image is the whole
    trick: it is what separates a new visit from the fourth page of one already
    in progress, without a second query or a per-hit event log.
    """
    now = now or datetime.utcnow()
    day = day_of(now)

    new_visitor = visitor_before is None
    # A visit is a browser-day. Their last hit landing on an earlier day means
    # they came back, which is a second visit; a hit ten minutes later is the
    # same one.
    new_visit = new_visitor or not _same_day(visitor_before.get("last_seen_at"), day)

    await bump(
        site,
        mongo_client,
        day=day,
        pageviews=1,
        visitors=1 if new_visitor else 0,
        views=1 if new_visit else 0,
        ad_views=1 if (new_visit and paid) else 0,
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

# The stages, in order, with the sentence each one needs when it reads oddly.
# `of` names the stage a share is taken against — every share here is of the
# visits cohort, except the first, which IS a slice of the cohort rather than a
# funnel step below it.
STAGES = (
    {
        "key": "ad_views",
        "label": "Ad clicks",
        "hint": (
            "Visits that arrived carrying Meta's ad identifiers. Expect this to "
            "sit below Meta's own link clicks — a person who clicks twice in a "
            "day is one visit here, and a browser that strips the parameters is "
            "none."
        ),
    },
    {
        "key": "views",
        "label": "Landing views",
        "hint": "Everyone who reached the page, counted once per day.",
    },
    {
        "key": "form_starts",
        "label": "Form started",
        "hint": "Visits where someone put a cursor in the form.",
    },
    {
        "key": "opt_ins",
        "label": "Opted in",
        "hint": "Visits that became a lead.",
    },
)

# What an embedded form can and cannot tell us, said once.
EMBEDDED_FORM_HINT = (
    "This client's form is embedded from another domain, so a start is only "
    "seen when the visitor clicks or focuses the embed — treat it as a floor, "
    "not an exact count."
)


def _window_days(start: str, end: str) -> int:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days + 1


def previous_window(start: str, end: str) -> tuple[str, str]:
    """The equally long window ending the day before this one starts."""
    length = _window_days(start, end)
    previous_end = datetime.fromisoformat(start) - timedelta(days=1)
    previous_start = previous_end - timedelta(days=length - 1)
    return day_of(previous_start), day_of(previous_end)


async def read_days(client_group_id: str, start: str, end: str, mongo_client) -> list[dict]:
    """The day rows in a window, oldest first, with every counter present."""
    rows = await mongo_client[DB_NAME][LANDING_DAILY].find(
        {"client_group_id": client_group_id, "day": {"$gte": start, "$lte": end}},
        projection={"_id": 0, "day": 1, **{key: 1 for key in COUNTERS}},
        sort=[("day", 1)],
    ).to_list(length=None)

    return [
        {"date": row["day"], **{key: int(row.get(key) or 0) for key in COUNTERS}}
        for row in rows
    ]


def sum_days(days: list[dict]) -> dict:
    """Total each counter across the window. Zeroes, not blanks, on an empty one."""
    return {key: sum(day.get(key, 0) for day in days) for key in COUNTERS}


def _share(count: int, cohort: int) -> float | None:
    """A stage as a fraction of the visits cohort, or None when there is none."""
    return count / cohort if cohort else None


def _delta(count: int, previous: int) -> dict | None:
    """
    Movement against the previous window, as a direction and a percentage.

    None when the previous window had nothing: "up 100%" from zero is a
    division by nothing dressed up as a result.
    """
    if not previous:
        return None
    change = (count - previous) / previous * 100
    return {"direction": "up" if change >= 0 else "down", "delta": round(abs(change), 1)}


def build_stages(totals: dict, previous: dict | None, embedded_form: bool = False) -> list[dict]:
    """
    The four stages as the hubs draw them.

    Shares are of `views`, never of the stage above — the same framing the
    client funnel uses, and for the same reason: it survives a stage we cannot
    measure. Ad clicks are the exception, being a slice of the visits rather
    than a step below them, so they carry no share at all.
    """
    cohort = totals.get("views", 0)
    stages = []
    for stage in STAGES:
        count = totals.get(stage["key"], 0)
        hint = stage["hint"]
        if stage["key"] == "form_starts" and embedded_form:
            hint = EMBEDDED_FORM_HINT
        stages.append({
            "key": stage["key"],
            "label": stage["label"],
            "count": count,
            "share": None if stage["key"] in ("ad_views", "views") else _share(count, cohort),
            "hint": hint,
            **(_delta(count, (previous or {}).get(stage["key"], 0)) or {}),
        })
    return stages


def build_exits(totals: dict) -> dict:
    """
    The visits that ended in nothing, split by how far they got.

    `opt_ins` can legitimately exceed `form_starts` — a browser autofilling and
    submitting in one gesture never focuses a field, and an embedded form only
    reports a start when the visitor happens to click it. So both splits are
    clamped at zero rather than being allowed to go negative and silently make
    the two halves stop summing to the whole.
    """
    views = totals.get("views", 0)
    starts = min(totals.get("form_starts", 0), views)
    opt_ins = min(totals.get("opt_ins", 0), views)

    total = max(views - opt_ins, 0)
    abandoned = max(min(starts, views) - opt_ins, 0)
    return {
        "total": total,
        # Never touched the form: everyone who did not start it. Derived from
        # the remainder rather than from starts directly, so the two halves
        # always add up to `total` whatever the counters did.
        "before_form": max(total - abandoned, 0),
        "abandoned_form": abandoned,
        "rate": _share(total, views),
    }


async def funnel(
    client_group_id: str,
    start: str,
    end: str,
    mongo_client,
    embedded_form: bool = False,
    compare: bool = True,
) -> dict:
    """
    The whole landing-page funnel for one client over one window.

    Returns the per-day rows too: they are already in hand, the caller is the
    only thing that knows whether it wants to plot them, and fetching them
    again later would cost a second round trip for data this query has read.
    """
    days = await read_days(client_group_id, start, end, mongo_client)
    totals = sum_days(days)

    previous = None
    if compare:
        previous_start, previous_end = previous_window(start, end)
        previous = sum_days(await read_days(client_group_id, previous_start, previous_end, mongo_client))

    return {
        "start": start,
        "end": end,
        "totals": totals,
        "previous": previous,
        "stages": build_stages(totals, previous, embedded_form=embedded_form),
        "exits": build_exits(totals),
        "days": days,
    }


async def has_any_data(client_group_id: str, mongo_client) -> bool:
    """Whether this client has ever reported a landing hit."""
    return bool(
        await mongo_client[DB_NAME][LANDING_DAILY].count_documents(
            {"client_group_id": client_group_id}, limit=1
        )
    )


# ---------------------------------------------------------------------------
# Indexes (registered in core/indexes.py)
# ---------------------------------------------------------------------------

async def create_landing_funnel_indexes(mongo_client):
    """Idempotent index creation for the landing-funnel rollup."""
    coll = mongo_client[DB_NAME][LANDING_DAILY]

    # The upsert's own filter, and the only read the reports do. Unique so a
    # racing pair of hits on the same day can't fork the row in two.
    await coll.create_index(
        [("client_group_id", 1), ("day", 1)], unique=True, name="group_day_unique"
    )

    logger.info("✅ Created landing funnel indexes")
