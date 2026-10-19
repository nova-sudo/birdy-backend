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

    [{"date": "2026-08-16", "leads": 4, "new_leads": 3, "new_contacts": 1,
      "open": 2, "won": 1, "lost": 0, "abandoned": 0}, ...]

sorted by date, one entry per day the location gained a contact or had an
opportunity change status (see the end of this docstring). `leads` is
the total (kept for back-compat with anything already reading it); the rest
split that same day's contacts by the Lead Hub's data model:

    new_leads / new_contacts  — lead_type == "lead" vs anything else, the
        same classification routers/client_groups.py's get_unified_leads
        already groups by (services/contact_classifier.py; trusts the
        stored field rather than recomputing, matching that endpoint's
        aggregation pipelines).
    open / won / lost / abandoned — among that day's *lead*-type contacts,
        one count per opportunity by current status, exactly like
        get_unified_leads' opp_pipeline (`$unwind` the opportunities array,
        group by status) — a contact with two open opportunities counts
        twice, same as it would there. Snapshotted as of whenever this cache
        last refreshed: a contact's status can move on from the day it was
        added, so this is a cohort snapshot, not a ledger of status-change
        events — but it's the only status data the Leads page's daily curve
        can plot without a real event log, and matching the live endpoint's
        counting rule keeps the chart and the KPI tiles it feeds in
        agreement with what get_unified_leads reports.

Those keys are the Leads Hub's (and report_blocks') and keep that meaning. The
dashboards query engine needs the other reading, "what happened on this day",
so every row also carries opportunity *events*, bucketed by the day the
opportunity's status last changed:

    opps_won / opps_lost / opps_abandoned / opps_open — one count per
        opportunity of any contact, on its lastStatusChangeAt day, falling back
        to updatedAt, then the opportunity's dateAdded, then its contact's
        dateAdded so an undated opportunity still lands somewhere.
    opps_total — the four above (the ghl_total_opps metric, and the
        denominator of ghl_conversion).
    revenue — the monetaryValue of the opportunities counted in opps_won, on
        that same day (the ghl_revenue metric).

A day can therefore have opportunity events and no new contacts: such a row has
`leads: 0`. Summing a window of these rows approximates the preset stats in
ghl_opp_cache, which are built from the opportunity search instead (and count
an open opportunity on its createdAt day, not its status-change day).
"""

import logging
from datetime import datetime

from core.database import DB_NAME
from integrations.gohighlevel import _opp_monetary_value

logger = logging.getLogger(__name__)

_OPP_STATUSES = ("open", "won", "lost", "abandoned")


_OPP_EVENT_KEYS = {"won": "opps_won", "lost": "opps_lost", "abandoned": "opps_abandoned", "open": "opps_open"}


def _blank_row() -> dict:
    return {
        "leads": 0, "new_leads": 0, "new_contacts": 0,
        "open": 0, "won": 0, "lost": 0, "abandoned": 0,
        "opps_won": 0, "opps_lost": 0, "opps_open": 0, "opps_abandoned": 0,
        "opps_total": 0, "revenue": 0.0,
    }


def _opp_event_day(opp: dict, contact: dict):
    """The day an opportunity's status last changed, falling back through
    updatedAt and dateAdded (its own, then its contact's). None if undated."""
    for value in (
        opp.get("lastStatusChangeAt"), opp.get("updatedAt"),
        opp.get("dateAdded"), contact.get("dateAdded"),
    ):
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


async def compute_daily_leads(user_id: str, ghl_location_id: str, mongo_client) -> list:
    """One row per day a contact was added or an opportunity changed status,
    counted from the already-synced ghl_contacts collection — a local read, no
    GHL API call."""
    db = mongo_client[DB_NAME]
    by_day = {}

    cursor = db["ghl_contacts"].find(
        {"user_id": user_id, "location_id": ghl_location_id},
        {"contact_data.dateAdded": 1, "lead_type": 1, "contact_data.opportunities": 1, "_id": 0},
    )
    async for doc in cursor:
        contact = doc.get("contact_data") or {}
        opps = contact.get("opportunities") or []

        added = contact.get("dateAdded")
        if added:
            row = by_day.setdefault(added[:10], _blank_row())
            row["leads"] += 1
            if doc.get("lead_type") == "lead":
                row["new_leads"] += 1
                for opp in opps:
                    status = (opp.get("status") or "").strip().lower()
                    if status in _OPP_STATUSES:
                        row[status] += 1
            else:
                row["new_contacts"] += 1

        # Opportunity events, on the day each one changed status. Unlike the
        # cohort counts above these include every contact, as ghl_opp_cache does.
        for opp in opps:
            status = (opp.get("status") or "open").strip().lower()
            key = _OPP_EVENT_KEYS.get(status)
            day = _opp_event_day(opp, contact)
            if not key or not day:
                continue
            row = by_day.setdefault(day, _blank_row())
            row[key] += 1
            row["opps_total"] += 1
            if status == "won":
                row["revenue"] += _opp_monetary_value(opp)

    return [
        {"date": day, **{k: (round(v, 2) if k == "revenue" else v) for k, v in row.items()}}
        for day, row in sorted(by_day.items())
    ]


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
