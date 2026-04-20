"""
AI tools for user-defined custom metrics (formulas built via the metrics page).
Allows the AI to list metrics, get their definitions, and evaluate them across
client groups / campaigns using the same data the frontend sees.
"""
from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from core.constants import PRESET_ALIAS


# Mapping from metric ID → data key (mirrors the frontend BASE_METRIC_MAPPING)
METRIC_ID_TO_DATA_KEY = {
    # GHL group-level
    "ghl_contacts": "ghl_contacts",
    "ghl_revenue": "ghl_revenue",
    "ghl_won_opps": "ghl_won_opps",
    "ghl_lost_opps": "ghl_lost_opps",
    "ghl_open_opps": "ghl_open_opps",
    "ghl_abandoned_opps": "ghl_abandoned_opps",
    "ghl_total_opps": "ghl_total_opps",
    # Meta group-level
    "meta_spend": "meta_spend",
    "meta_impressions": "meta_impressions",
    "meta_clicks": "meta_clicks",
    "meta_reach": "meta_reach",
    "meta_leads": "meta_leads",
    "meta_results": "meta_results",
    "meta_ctr": "meta_ctr",
    "meta_cpc": "meta_cpc",
    "meta_cpm": "meta_cpm",
    # Meta campaign-level (Marketing Hub)
    "spend": "spend",
    "impressions": "impressions",
    "clicks": "clicks",
    "reach": "reach",
    "results": "results",
    "leads": "leads",
    "ctr": "ctr",
    "cpc": "cpc",
    "cpm": "cpm",
    "cpl": "cpl",
    "frequency": "frequency",
    "cost_per_result": "cost_per_result",
}


def _evaluate_formula(formula_parts, row):
    """Evaluate a formula (list of {type, value} parts) against a row of data."""
    if not formula_parts:
        return None
    expression = ""
    for part in formula_parts:
        if part.get("type") == "metric":
            key = part.get("value")
            # Map metric ID to data key
            data_key = METRIC_ID_TO_DATA_KEY.get(key, key)
            value = row.get(data_key)
            # Tag metrics (tag_foo_bar) — fall back to 0 (we don't have per-row tag counts here)
            if value is None and key and key.startswith("tag_"):
                value = 0
            if value is None:
                value = 0
            try:
                expression += str(float(value))
            except (TypeError, ValueError):
                expression += "0"
        elif part.get("type") == "operator":
            expression += f" {part.get('value')} "
    try:
        # Safe-ish evaluation — only arithmetic operators allowed from the builder
        import ast
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, (
                ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                ast.Num, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub,
            )):
                return None
        result = eval(compile(tree, "<formula>", "eval"))
        if result != result or result in (float("inf"), float("-inf")):  # NaN/inf check
            return 0
        return float(result)
    except Exception:
        return None


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
    Compute a custom metric across all (or selected) client groups using data
    from facebook_cache (for the preset) and ghl_opp_cache / gohighlevel_cache.
    """
    resolved = PRESET_ALIAS.get(preset or "last_7d", "last_7d")

    # Look up the metric definition
    user_doc = await db["users"].find_one({"user_id": user_id}, {"custom_metrics": 1})
    metrics = (user_doc or {}).get("custom_metrics", []) or []
    metric = next((m for m in metrics if m.get("id") == metric_id), None)
    if not metric:
        return {"error": f"Custom metric '{metric_id}' not found"}

    formula_parts = metric.get("formula_parts", [])
    format_type = metric.get("format_type", "integer")

    # Fetch client groups with the needed cache fields
    query = {"user_id": user_id}
    if group_ids:
        query["id"] = {"$in": group_ids}

    groups = await db["client_groups"].find(
        query,
        {
            "id": 1, "name": 1,
            f"facebook_cache.{resolved}.metrics.insights": 1,
            f"ghl_opp_cache.{resolved}": 1,
            "ghl_opp_cache.maximum": 1,
            "gohighlevel_cache.metrics": 1,
            "_id": 0,
        },
    ).to_list(None)

    per_group = []
    for g in groups:
        fb_preset = (g.get("facebook_cache", {}) or {}).get(resolved, {}) or {}
        insights = (fb_preset.get("metrics", {}) or {}).get("insights", {}) or {}
        opp_cache = g.get("ghl_opp_cache") or {}
        opp_stats = opp_cache.get(resolved) or opp_cache.get("maximum") or {}
        ghl_metrics = (g.get("gohighlevel_cache", {}) or {}).get("metrics", {}) or {}

        # Build a flat row with all metric keys
        row = {
            # Meta group-level
            "meta_spend": insights.get("spend", 0) or 0,
            "meta_impressions": insights.get("impressions", 0) or 0,
            "meta_clicks": insights.get("clicks", 0) or 0,
            "meta_reach": insights.get("reach", 0) or 0,
            "meta_leads": insights.get("results", 0) or insights.get("total_leads", 0) or 0,
            "meta_results": insights.get("results", 0) or 0,
            "meta_ctr": insights.get("ctr", 0) or 0,
            "meta_cpc": insights.get("cpc", 0) or 0,
            "meta_cpm": insights.get("cpm", 0) or 0,
            # GHL group-level
            "ghl_contacts": ghl_metrics.get("total_contacts", 0) or 0,
            "ghl_revenue": opp_stats.get("won_revenue", 0) or 0,
            "ghl_won_opps": opp_stats.get("won", 0) or 0,
            "ghl_lost_opps": opp_stats.get("lost", 0) or 0,
            "ghl_open_opps": opp_stats.get("open", 0) or 0,
            "ghl_abandoned_opps": opp_stats.get("abandoned", 0) or 0,
            "ghl_total_opps": opp_stats.get("total_opportunities", 0) or 0,
        }

        value = _evaluate_formula(formula_parts, row)
        per_group.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "value": round(value, 2) if value is not None else None,
        })

    # Aggregate
    valid = [g["value"] for g in per_group if g["value"] is not None]
    overall = sum(valid) if valid else 0

    return {
        "metric": {
            "id": metric_id,
            "name": metric.get("name"),
            "formula": metric.get("formula_display", ""),
            "format_type": format_type,
        },
        "per_group": per_group[:MAX_RESULT_ITEMS],
        "overall_total": round(overall, 2),
        "overall_average": round(overall / len(valid), 2) if valid else 0,
        "preset": resolved,
        "groups_evaluated": len(valid),
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
