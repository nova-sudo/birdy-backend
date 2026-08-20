"""
services/ghl_daily_leads.py
----------------------------
Account-level daily new-lead counts.

Sibling to integrations/facebook_utils/meta_daily_spend.py: the Leads page
needs a daily curve, and Sales-Hub's call trend chart already hit this
problem the wrong way — it fetches every page of the call-log history on
every page load just to bucket it into days client-side.

This one is cheaper than the Meta case, though: fetch_and_cache_ghl_data_optimized
already syncs every contact for a location into `ghl_contacts` on every refresh,
so there is no extra API call to make. This just buckets what is already on
disk by day, the same way _compute_call_stats_all_presets buckets HP calls.

Stored on the client group as `ghl_daily_leads`:

    [{"date": "2026-08-16", "leads": 4}, ...]

sorted by date, one entry per day the location gained a contact.
"""

import logging
from collections import Counter
from datetime import datetime

from core.database import DB_NAME

logger = logging.getLogger(__name__)


async def compute_daily_leads(user_id: str, ghl_location_id: str, mongo_client) -> list:
    """One row per day a contact was added, counted from the already-synced
    ghl_contacts collection — a local read, no GHL API call."""
    db = mongo_client[DB_NAME]
    counts = Counter()

    cursor = db["ghl_contacts"].find(
        {"user_id": user_id, "location_id": ghl_location_id},
        {"contact_data.dateAdded": 1, "_id": 0},
    )
    async for doc in cursor:
        added = (doc.get("contact_data") or {}).get("dateAdded")
        if added:
            counts[added[:10]] += 1

    return [{"date": day, "leads": n} for day, n in sorted(counts.items())]


async def cache_ghl_daily_leads(
        group_id: str,
        user_id: str,
        ghl_location_id: str,
        mongo_client,
) -> int:
    """
    Refresh one group's daily lead count and store it on the group.

    Rewrites the whole series every run — cheap, since it's a local count
    rather than a fetch. If the count comes back empty while a non-empty
    cache already exists, skip the write rather than blanking it — the same
    transient-failure guard cache_ghl_opp_stats_all_presets uses.
    """
    db = mongo_client[DB_NAME]

    rows = await compute_daily_leads(user_id, ghl_location_id, mongo_client)

    if not rows:
        existing = await db["client_groups"].find_one({"id": group_id}, {"ghl_daily_leads": 1})
        if (existing or {}).get("ghl_daily_leads"):
            logger.warning(
                "Daily leads count came back empty for %s but cache is non-empty — skipping",
                ghl_location_id,
            )
            return 0

    await db["client_groups"].update_one(
        {"id": group_id},
        {"$set": {
            "ghl_daily_leads": rows,
            "ghl_daily_leads_updated_at": datetime.utcnow().isoformat(),
        }},
    )

    logger.info(
        "Cached daily leads for %s: %d days, %d total leads",
        group_id, len(rows), sum(r["leads"] for r in rows),
    )
    return len(rows)
