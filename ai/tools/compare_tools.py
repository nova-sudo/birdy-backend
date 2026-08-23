from ai.tools.registry import registry
from ai.tools.derived_metrics import enrich, _safe_float
from ai.config import MAX_RESULT_ITEMS

_PRESET_LIST = (
    "maximum, today, yesterday, this_week_mon_today, last_7d, last_14d, "
    "last_30d, this_month, last_month, this_quarter, last_quarter, this_year, last_year"
)

_COMPARE_METRICS = [
    "spend", "impressions", "clicks", "reach", "results",
    "total_leads", "cpm", "cpc", "ctr",
]


def _compute_changes(a: dict, b: dict) -> dict:
    """Compute delta and pct_change for each metric between two enriched dicts."""
    changes = {}
    all_keys = _COMPARE_METRICS + ["cpl", "conversion_rate", "frequency", "cost_per_result"]
    for key in all_keys:
        val_a = _safe_float(a.get(key))
        val_b = _safe_float(b.get(key))

        delta = None
        pct = None
        if val_a is not None and val_b is not None:
            delta = round(val_b - val_a, 4)
            if val_a != 0:
                pct = round((delta / val_a) * 100, 2)

        changes[key] = {"value_a": val_a, "value_b": val_b, "delta": delta, "pct_change": pct}
    return changes


async def compare_periods(db, user_id, preset_a, preset_b, group_ids=None):
    """
    Compare two date presets side-by-side with absolute and percentage change.
    preset_a is the baseline, preset_b is the comparison period.
    """
    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            # Split shape and legacy bucket — preferred in that order below.
            f"facebook_cache.presets.{preset_a}.metrics": 1,
            f"facebook_cache.presets.{preset_b}.metrics": 1,
            f"facebook_cache.{preset_a}.metrics": 1,
            f"facebook_cache.{preset_b}.metrics": 1,
            "facebook_cache.currency": 1,
            "_id": 0,
        },
    ).to_list(None)

    per_group = []
    totals_a = {"spend": 0, "impressions": 0, "clicks": 0, "reach": 0, "results": 0, "total_leads": 0}
    totals_b = {**totals_a}

    for g in groups:
        fb = g.get("facebook_cache", {})
        _split = fb.get("presets") or {}

        def _insights(preset):
            bucket = _split.get(preset) or fb.get(preset) or {}
            return (bucket.get("metrics") or {}).get("insights") or {}

        ins_a = _insights(preset_a)
        ins_b = _insights(preset_b)

        row_a = {k: _safe_float(ins_a.get(k)) or 0 for k in _COMPARE_METRICS}
        row_b = {k: _safe_float(ins_b.get(k)) or 0 for k in _COMPARE_METRICS}

        enrich(row_a)
        enrich(row_b)

        per_group.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "currency": fb.get("currency"),
            "period_a": row_a,
            "period_b": row_b,
            "changes": _compute_changes(row_a, row_b),
        })

        for k in totals_a:
            totals_a[k] += row_a.get(k, 0) or 0
            totals_b[k] += row_b.get(k, 0) or 0

    overall_a = enrich({**totals_a})
    overall_b = enrich({**totals_b})

    return {
        "overall": {
            "period_a": overall_a,
            "period_b": overall_b,
            "changes": _compute_changes(overall_a, overall_b),
        },
        "groups": per_group[:MAX_RESULT_ITEMS],
        "preset_a": preset_a,
        "preset_b": preset_b,
        "total_groups": len(per_group),
    }


def register_compare_tools():
    registry.register(
        name="compare_periods",
        description=(
            "Compare two date presets side-by-side with absolute and percentage change for each metric. "
            "preset_a is the baseline (older period), preset_b is the comparison (current period). "
            "Use when the user says 'vs', 'compared to', 'change from', etc."
        ),
        parameters={
            "type": "object",
            "properties": {
                "preset_a": {
                    "type": "string",
                    "description": f"Baseline period preset. Valid: {_PRESET_LIST}.",
                },
                "preset_b": {
                    "type": "string",
                    "description": f"Comparison period preset. Valid: {_PRESET_LIST}.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to specific group IDs. Omit for all groups.",
                },
            },
            "required": ["preset_a", "preset_b"],
        },
        executor=compare_periods,
    )
