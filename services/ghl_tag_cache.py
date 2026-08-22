"""
services/ghl_tag_cache.py
-------------------------
Windowed tag counts per client group, precomputed per preset.

The Clients page renders one column per GHL tag, counted over the selected
date window. That count was computed live on every page load, by the tags
branch of the `$facet` in routers/client_groups.py: `$unwind` every tag on
every contact in the window, then group. Measured on production:

    ms=1530  nReturned=16723  keysExamined=180106  docsExamined=180106

180,106 documents examined is the entire ghl_contacts collection — 478 MB —
per page load. No index can fix it: `$unwind` on an array field forces a FETCH
of every matching document, and multikey indexes cannot cover a query over the
array they index.

`gohighlevel_cache.metrics.tag_breakdown` looks like it already solves this, and
does not. That field is a LIFETIME counter, accumulated incrementally as
contacts sync (services/ghl_service.py merges the previous value into each
run). Verified against production: for one group it matched a live lifetime
aggregation exactly (96 tags, 28,726) while the windowed value for the same
group was 47 tags and 2,654. They are different metrics that happen to share a
shape.

Stored in its own collection rather than on the client group, keyed
{group_id, preset}:

    {"group_id": "...", "user_id": "...", "preset": "last_30d",
     "tags": {"fb lead form submitted": 236, ...},
     "updated_at": "2026-08-22T15:00:00"}

client_groups is already 784 KB per document on average and 7.45 MB at worst —
47% of MongoDB's hard 16 MB limit — so thirteen more preset buckets belong
somewhere else. Sibling to ghl_daily_leads, which buckets the same collection
by day; this one buckets it by tag.
"""

import logging
from collections import defaultdict
from datetime import datetime

from core.constants import META_CACHE_PRESETS, ghl_date_bounds
from core.database import DB_NAME

logger = logging.getLogger(__name__)

COLLECTION = "client_group_tag_cache"


async def _tags_by_day(user_id: str, group_id: str, mongo_client) -> dict:
    """{"2026-08-16": {"tag": count}} for one group — one pass over its contacts.

    Bucketing by day once and rolling the days up per preset in Python beats
    running one aggregation per preset: thirteen passes over the same contacts
    to answer thirteen overlapping questions about them.
    """
    db = mongo_client[DB_NAME]
    pipeline = [
        {"$match": {"user_id": user_id, "client_group_id": group_id}},
        {"$unwind": {"path": "$contact_data.tags", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": {
                # dateAdded is a string-ISO timestamp, so the day is its first
                # ten characters — the same slice the date filters compare on.
                "day": {"$substrBytes": ["$contact_data.dateAdded", 0, 10]},
                "tag": "$contact_data.tags",
            },
            "n": {"$sum": 1},
        }},
    ]
    by_day: dict = defaultdict(dict)
    async for row in db["ghl_contacts"].aggregate(pipeline, allowDiskUse=True):
        day = row["_id"].get("day")
        tag = row["_id"].get("tag")
        if day and tag:
            by_day[day][tag] = row["n"]
    return by_day


def _rollup(by_day: dict, start: str, end: str) -> dict:
    """Sum per-day tag counts across [start, end]. None bounds mean all-time."""
    totals: dict = defaultdict(int)
    for day, tags in by_day.items():
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        for tag, n in tags.items():
            totals[tag] += n
    # Sorted so the heaviest tags read first wherever this is dumped.
    return dict(sorted(totals.items(), key=lambda kv: -kv[1]))


async def cache_group_tag_breakdown(
        user_id: str,
        group_id: str,
        mongo_client,
        presets=None,
) -> int:
    """Recompute every preset's tag counts for one group.

    Returns the number of presets written. A group with no tagged contacts
    writes empty buckets rather than nothing, so the read path can tell
    "computed, and there are none" from "never computed".
    """
    presets = presets or META_CACHE_PRESETS
    by_day = await _tags_by_day(user_id, group_id, mongo_client)

    db = mongo_client[DB_NAME]
    now = datetime.utcnow().isoformat()
    written = 0
    for preset in presets:
        start, end = ghl_date_bounds(preset)
        await db[COLLECTION].update_one(
            {"group_id": group_id, "preset": preset},
            {"$set": {
                "user_id": user_id,
                "tags": _rollup(by_day, start, end),
                "updated_at": now,
            }},
            upsert=True,
        )
        written += 1

    logger.info(
        "Cached tag breakdown for %s: %d presets, %d days of tag data",
        group_id, written, len(by_day),
    )
    return written


async def get_tag_breakdowns(user_id: str, preset: str, mongo_client) -> dict:
    """{group_id: {tag: count}} for one user and preset.

    Returns {} for groups with no cached row rather than raising — a group
    added since the last refresh simply has no tags yet, which the Clients
    page renders as empty columns.
    """
    db = mongo_client[DB_NAME]
    out: dict = {}
    async for doc in db[COLLECTION].find(
        {"user_id": user_id, "preset": preset},
        {"group_id": 1, "tags": 1, "_id": 0},
    ):
        out[doc["group_id"]] = doc.get("tags") or {}
    return out
