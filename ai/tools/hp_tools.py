"""
ai/tools/hp_tools.py
--------------------
HotProspector (call center) chat tool. Reads the per-preset call stats that
services/hp_service.py caches onto each client_group under
`hotprospector_call_cache.{preset}` (same cache-first pattern as the Meta tools
in meta_tools.py — no live HotProspector call here).

Exposes `get_call_center_stats`, which closes the lead-quality funnel past GHL:
it lets the agent see whether an ad's leads are actually reachable (connect rate)
and worked (coverage), so a cheap CPL with a low connect rate is exposed as a
zombie-lead source rather than a win.
"""

from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS


async def get_call_center_stats(db, user_id, preset="last_7d", group_ids=None, **_):
    """Per-client HotProspector call stats for a date-window preset, read from the
    `hotprospector_call_cache` on client_groups. Adds derived connect_rate
    (answered / total calls) and leads_called_rate (leads worked / total leads)."""
    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {"id": 1, "name": 1, f"hotprospector_call_cache.{preset}": 1},
    ).to_list(None)

    results = []
    for g in groups:
        stats = (g.get("hotprospector_call_cache") or {}).get(preset)
        if not stats:
            # No HotProspector data cached for this client/preset — skip rather
            # than emit misleading zeros.
            continue

        total_calls = stats.get("total_calls", 0) or 0
        answered = stats.get("answered_calls", 0) or 0
        total_leads = stats.get("total_leads", 0) or 0
        leads_with_calls = stats.get("leads_with_calls", 0) or 0

        results.append({
            "client_group_id": g.get("id"),
            "client_group_name": g.get("name"),
            "preset": preset,
            "total_calls": total_calls,
            "inbound_count": stats.get("inbound_count", 0),
            "outbound_count": stats.get("outbound_count", 0),
            "transfers": stats.get("transfers", 0),
            "answered_calls": answered,
            "total_talk_min": stats.get("total_talk_min", 0),
            "total_leads": total_leads,
            "leads_with_calls": leads_with_calls,
            # % of placed calls that connected (talk time > 0) — lead reachability.
            "connect_rate": round(answered / total_calls * 100, 1) if total_calls else 0.0,
            # % of leads that got >= 1 call — call-center coverage. Low coverage is
            # an operations gap, not a lead-quality problem.
            "leads_called_rate": round(leads_with_calls / total_leads * 100, 1) if total_leads else 0.0,
        })

    return {"call_center": results[:MAX_RESULT_ITEMS], "total": len(results), "preset": preset}


# ── Schema ──────────────────────────────────────────────────────────────────

_CALL_CENTER_PARAMS = {
    "type": "object",
    "properties": {
        "preset": {
            "type": "string",
            "description": (
                "Date-window preset. One of: today, yesterday, last_7d, last_14d, "
                "last_30d, this_month, last_month, this_quarter, last_quarter, "
                "this_year, last_year, maximum. Default last_7d."
            ),
        },
        "group_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional client group ids to filter to (from get_client_groups).",
        },
    },
    "required": [],
}


def register_hp_tools():
    registry.register(
        name="get_call_center_stats",
        description=(
            "Get HotProspector call-center stats per client for a date window: total / "
            "inbound / outbound calls, transfers, answered calls, talk time, leads called, "
            "plus connect_rate (answered / total calls — lead reachability) and "
            "leads_called_rate (coverage). Use for lead-quality analysis: an ad with a "
            "cheap CPL but a low connect_rate is producing unreachable / low-intent leads. "
            "If leads_called_rate is low, that's a call-center coverage (operations) gap, "
            "not a media problem. Pass a preset and optional group_ids."
        ),
        parameters=_CALL_CENTER_PARAMS,
        executor=get_call_center_stats,
    )
