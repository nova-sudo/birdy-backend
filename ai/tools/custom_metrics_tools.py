"""
AI tools for user-defined custom metrics (formulas built via the metrics page).
Delegates all metric resolution and formula evaluation to the central
services/metric_orchestrator so logic stays in one place.
"""
from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from services.metric_orchestrator import (
    evaluate_formula,
    evaluate_formula_aggregated,
    resolve_preset,
)


async def list_custom_metrics(db, user_id):
    """Return all custom metrics defined by the user."""
    user_doc = await db["users"].find_one({"user_id": user_id}, {"custom_metrics": 1})
    metrics = (user_doc or {}).get("custom_metrics", []) or []

    # Simplify to what the AI needs
    simplified = []
    for m in metrics:
        simplified.append({
            "id": m.get("id"),
            "name": m.get("name"),
            "description": m.get("description", ""),
            "formula": m.get("formula_display", ""),
            "format_type": m.get("format_type", "integer"),
            "dashboards": m.get("dashboards", []),
            "aggregation": m.get("aggregation", "total"),
        })
    return {"custom_metrics": simplified, "total": len(simplified)}


async def compute_custom_metric(db, user_id, metric_id, preset="last_7d", group_ids=None):
    """
    Compute a custom metric across all (or selected) client groups.
    All metric resolution + formula evaluation is delegated to
    services.metric_orchestrator so the AI, alerts, and frontend agree.
    """
    resolved = resolve_preset(preset or "last_7d")

    # Look up the metric definition
    user_doc = await db["users"].find_one({"user_id": user_id}, {"custom_metrics": 1})
    metrics = (user_doc or {}).get("custom_metrics", []) or []
    metric = next((m for m in metrics if m.get("id") == metric_id), None)
    if not metric:
        return {"error": f"Custom metric '{metric_id}' not found"}

    formula_parts = metric.get("formula_parts", [])
    format_type = metric.get("format_type", "integer")
    metric_aggregation = metric.get("aggregation", "total")

    # Fetch client groups (pull the caches the orchestrator knows how to read)
    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            "facebook_cache": 1,
            "ghl_opp_cache": 1,
            "gohighlevel_cache.metrics": 1,
            "_id": 0,
        },
    ).to_list(None)

    # Evaluate per group via the orchestrator
    per_group = []
    for g in groups:
        value = evaluate_formula(formula_parts, g, resolved)
        per_group.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "value": round(value, 2),
        })

    # Aggregate — "recompute" for ratio metrics, "total" for sums
    agg_mode = "recompute" if metric_aggregation == "average" or format_type == "percentage" else "total"
    overall = evaluate_formula_aggregated(formula_parts, groups, resolved, aggregation=agg_mode)
    valid_count = len([g for g in per_group if g["value"] is not None])

    return {
        "metric": {
            "id": metric_id,
            "name": metric.get("name"),
            "formula": metric.get("formula_display", ""),
            "format_type": format_type,
        },
        "per_group": per_group[:MAX_RESULT_ITEMS],
        "overall_total": round(overall, 2),
        "overall_average": round(overall / valid_count, 2) if valid_count else 0,
        "aggregation_mode": agg_mode,
        "preset": resolved,
        "groups_evaluated": valid_count,
    }


def register_custom_metrics_tools():
    registry.register(
        name="list_custom_metrics",
        description=(
            "List all user-defined custom metrics (formulas). Each metric has an id, name, formula, "
            "format type (currency/percentage/decimal/integer), and target dashboards. Use this first "
            "to discover what custom metrics exist before computing one."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        executor=list_custom_metrics,
    )

    registry.register(
        name="compute_custom_metric",
        description=(
            "Evaluate a custom metric formula across client groups. Returns per-group values and "
            "an overall total/average. Use when the user asks about their own custom metric by name "
            "(e.g. 'what's my ROAS' if they defined a ROAS metric)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric_id": {"type": "string", "description": "The custom metric ID (from list_custom_metrics)"},
                "preset": {
                    "type": "string",
                    "description": "Date preset (maximum, today, last_7d, last_30d, this_month, etc.). Default: last_7d.",
                },
                "group_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter to specific group IDs. Omit for all groups.",
                },
            },
            "required": ["metric_id"],
        },
        executor=compute_custom_metric,
    )
