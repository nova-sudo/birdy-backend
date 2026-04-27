"""
AI tools for user-defined custom metrics (formulas built via the metrics page).
Delegates all metric resolution and formula evaluation to the central
services/metric_orchestrator so logic stays in one place.
"""
import re
from datetime import datetime

from ai.tools.registry import registry
from ai.config import MAX_RESULT_ITEMS
from services.metric_orchestrator import (
    METRIC_REGISTRY,
    evaluate_formula,
    evaluate_formula_aggregated,
    resolve_preset,
)

# ─────────────────────────────────────────────────────────────────────────────
# Formula parsing
# ─────────────────────────────────────────────────────────────────────────────

# Natural-language aliases → metric IDs. Lets the AI accept friendly names
# like "Meta Spend" or "GHL Revenue" instead of the raw id.
_METRIC_ALIASES = {
    # Meta
    "meta spend": "meta_spend", "ad spend": "meta_spend", "spend": "meta_spend",
    "meta impressions": "meta_impressions", "impressions": "meta_impressions",
    "meta clicks": "meta_clicks", "clicks": "meta_clicks",
    "meta reach": "meta_reach", "reach": "meta_reach",
    "meta leads": "meta_leads", "meta results": "meta_results",
    "meta ctr": "meta_ctr", "ctr": "meta_ctr",
    "meta cpc": "meta_cpc", "cpc": "meta_cpc",
    "meta cpm": "meta_cpm", "cpm": "meta_cpm",
    # GHL
    "ghl contacts": "ghl_contacts", "ghl leads": "ghl_contacts", "total contacts": "ghl_contacts",
    "ghl revenue": "ghl_revenue", "revenue": "ghl_revenue", "won revenue": "ghl_revenue",
    "ghl won opps": "ghl_won_opps", "won opps": "ghl_won_opps", "won opportunities": "ghl_won_opps",
    "ghl lost opps": "ghl_lost_opps", "lost opps": "ghl_lost_opps", "lost opportunities": "ghl_lost_opps",
    "ghl open opps": "ghl_open_opps", "open opps": "ghl_open_opps", "open opportunities": "ghl_open_opps",
    "ghl abandoned opps": "ghl_abandoned_opps", "abandoned opps": "ghl_abandoned_opps",
    "ghl total opps": "ghl_total_opps", "total opps": "ghl_total_opps",
    "ghl conversion": "ghl_conversion", "conversion rate": "ghl_conversion",
    # Derived
    "cpl": "cpl", "cost per lead": "cpl",
    "cost per result": "cost_per_result",
}

_OPERATORS = {"+", "-", "*", "/", "×", "÷", "x"}


def parse_formula_text(text: str):
    """
    Convert a free-form formula string into formula_parts.

    Examples:
      "meta_spend / ghl_revenue"          → [meta_spend, /, ghl_revenue]
      "Meta Spend / GHL Revenue"          → [meta_spend, /, ghl_revenue]
      "(spend - ghl_revenue) / spend"     → [spend, -, ghl_revenue, /, spend]   (parens dropped)

    Returns: (formula_parts, formula_display, unresolved_tokens)
      formula_parts: list of {type, value}
      formula_display: readable string using canonical metric ids
      unresolved_tokens: tokens that didn't map to a known metric (caller can warn)
    """
    if not text or not text.strip():
        return [], "", []

    # Normalize operators, collapse whitespace, strip parens (we don't support them yet)
    t = text.lower().strip()
    t = t.replace("×", "*").replace("÷", "/").replace("(", " ").replace(")", " ")

    # Split on operators but keep them as tokens
    # Regex: match any operator or any non-operator run
    token_re = re.compile(r'([+\-*/])|([^+\-*/\s][^+\-*/]*[^+\-*/\s]|[^+\-*/\s])')
    raw_tokens = []
    for m in token_re.finditer(t):
        op, name = m.group(1), m.group(2)
        if op:
            raw_tokens.append(("op", op))
        elif name:
            raw_tokens.append(("name", name.strip()))

    parts = []
    display_parts = []
    unresolved = []

    for kind, tok in raw_tokens:
        if kind == "op":
            parts.append({"type": "operator", "value": tok})
            display_parts.append(tok)
        else:
            # Try exact metric id first, then alias
            metric_id = None
            if tok in METRIC_REGISTRY:
                metric_id = tok
            elif tok.startswith("tag_") or tok.startswith("custom_"):
                metric_id = tok
            else:
                alias = _METRIC_ALIASES.get(tok)
                if alias:
                    metric_id = alias
                else:
                    # Try with underscores replaced by spaces, etc.
                    alt = tok.replace("_", " ").strip()
                    metric_id = _METRIC_ALIASES.get(alt)

            if metric_id:
                parts.append({"type": "metric", "value": metric_id})
                display_parts.append(metric_id)
            else:
                unresolved.append(tok)

    return parts, " ".join(display_parts), unresolved


# ─────────────────────────────────────────────────────────────────────────────
# Valid dashboard targets
# ─────────────────────────────────────────────────────────────────────────────
_VALID_DASHBOARDS = {"clients", "campaigns", "adsets", "ads", "leads", "marketing_leads"}


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

    # Evaluate per group via the orchestrator. Pass the full custom_metrics list
    # so formulas can reference other custom metrics (e.g. ROAS uses CPA).
    per_group = []
    for g in groups:
        value = evaluate_formula(formula_parts, g, resolved, custom_metrics=metrics)
        per_group.append({
            "group_id": g.get("id"),
            "group_name": g.get("name"),
            "value": round(value, 2),
        })

    # Aggregate — "recompute" for ratio metrics, "total" for sums
    agg_mode = "recompute" if metric_aggregation == "average" or format_type == "percentage" else "total"
    overall = evaluate_formula_aggregated(
        formula_parts, groups, resolved, aggregation=agg_mode, custom_metrics=metrics
    )
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


async def create_custom_metric(
    db, user_id,
    name,
    formula_text=None,
    formula_parts=None,
    description=None,
    format_type="integer",
    dashboards=None,
    aggregation="total",
):
    """
    Create a new user-defined custom metric.

    Either `formula_text` (friendly string) OR `formula_parts` (structured) must be provided.
    Dashboards must be in: clients, campaigns, adsets, ads, leads, marketing_leads.
    """
    if not name or not name.strip():
        return {"error": "Name is required"}

    # Build formula_parts if we got text
    if not formula_parts:
        if not formula_text:
            return {"error": "Provide either formula_text (e.g. 'meta_spend / ghl_revenue') or formula_parts"}
        formula_parts, formula_display, unresolved = parse_formula_text(formula_text)
        if unresolved:
            return {
                "error": f"Could not resolve these metric names: {unresolved}. "
                         f"Use known metric ids (call list_custom_metrics or check available metrics) or common aliases like 'Meta Spend', 'GHL Revenue'."
            }
    else:
        formula_display = " ".join(p.get("value", "") for p in formula_parts)

    if not formula_parts:
        return {"error": "Formula is empty"}

    # Validate metric refs in formula_parts
    unknown = [p["value"] for p in formula_parts
               if p.get("type") == "metric"
               and p["value"] not in METRIC_REGISTRY
               and not p["value"].startswith("tag_")
               and not p["value"].startswith("custom_")]
    if unknown:
        return {"error": f"Formula references unknown metrics: {unknown}"}

    # Normalize dashboards
    dashboards = [d for d in (dashboards or []) if d in _VALID_DASHBOARDS]
    if not dashboards:
        # Default based on the metric levels used
        # If formula only uses group-level metrics (meta_*, ghl_*) → clients
        # Otherwise → campaigns
        all_metric_refs = [p["value"] for p in formula_parts if p.get("type") == "metric"]
        if all(r.startswith("meta_") or r.startswith("ghl_") for r in all_metric_refs):
            dashboards = ["clients"]
        else:
            dashboards = ["campaigns"]

    if format_type not in {"integer", "currency", "percentage", "decimal"}:
        format_type = "integer"
    if aggregation not in {"total", "average"}:
        aggregation = "total"

    metric_id = f"custom_{user_id}_{int(datetime.utcnow().timestamp() * 1000)}"

    metric_doc = {
        "id": metric_id,
        "name": name.strip(),
        "description": (description or "").strip(),
        "formula_parts": formula_parts,
        "formula_display": formula_display,
        "dashboards": dashboards,
        "format_type": format_type,
        "aggregation": aggregation,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    await db["users"].update_one(
        {"user_id": user_id},
        {"$push": {"custom_metrics": metric_doc}},
        upsert=True,
    )

    return {"success": True, "metric": metric_doc}


async def update_custom_metric(
    db, user_id,
    metric_id,
    name=None,
    formula_text=None,
    formula_parts=None,
    description=None,
    format_type=None,
    dashboards=None,
    aggregation=None,
):
    """Update fields on an existing custom metric. Only provided fields change."""
    user = await db["users"].find_one({"user_id": user_id}, {"custom_metrics": 1})
    if not user:
        return {"error": "User not found"}

    metrics = user.get("custom_metrics", []) or []
    target = next((m for m in metrics if m.get("id") == metric_id), None)
    if not target:
        return {"error": f"Metric '{metric_id}' not found"}

    # Build new formula_parts if a formula was supplied
    new_parts = None
    new_display = None
    if formula_parts:
        new_parts = formula_parts
        new_display = " ".join(p.get("value", "") for p in formula_parts)
    elif formula_text:
        new_parts, new_display, unresolved = parse_formula_text(formula_text)
        if unresolved:
            return {"error": f"Could not resolve these metric names: {unresolved}"}

    # Apply updates in-place
    for m in metrics:
        if m.get("id") != metric_id:
            continue
        if name is not None:
            m["name"] = name.strip()
        if description is not None:
            m["description"] = description.strip()
        if new_parts is not None:
            m["formula_parts"] = new_parts
            m["formula_display"] = new_display
        if dashboards is not None:
            valid = [d for d in dashboards if d in _VALID_DASHBOARDS]
            if valid:
                m["dashboards"] = valid
        if format_type is not None and format_type in {"integer", "currency", "percentage", "decimal"}:
            m["format_type"] = format_type
        if aggregation is not None and aggregation in {"total", "average"}:
            m["aggregation"] = aggregation
        m["updated_at"] = datetime.utcnow().isoformat()
        break

    await db["users"].update_one(
        {"user_id": user_id},
        {"$set": {"custom_metrics": metrics}},
    )

    return {"success": True, "metric_id": metric_id}


async def delete_custom_metric(db, user_id, metric_id):
    """Delete a custom metric by id."""
    result = await db["users"].update_one(
        {"user_id": user_id},
        {"$pull": {"custom_metrics": {"id": metric_id}}},
    )
    if result.modified_count == 0:
        return {"error": f"Metric '{metric_id}' not found"}
    return {"success": True, "deleted": metric_id}


async def list_available_metric_fields(db, user_id):
    """
    Return the list of built-in metric IDs (with friendly labels) that can be used
    in a custom formula. Use this when the user asks what they can reference.
    """
    metrics = []
    labels = {
        "meta_spend": "Meta Spend", "meta_impressions": "Meta Impressions",
        "meta_clicks": "Meta Clicks", "meta_reach": "Meta Reach",
        "meta_leads": "Meta Leads", "meta_results": "Meta Results",
        "meta_ctr": "Meta CTR", "meta_cpc": "Meta CPC", "meta_cpm": "Meta CPM",
        "ghl_contacts": "GHL Contacts", "ghl_revenue": "GHL Revenue",
        "ghl_won_opps": "Won Opps", "ghl_lost_opps": "Lost Opps",
        "ghl_open_opps": "Open Opps", "ghl_abandoned_opps": "Abandoned Opps",
        "ghl_total_opps": "Total Opps", "ghl_conversion": "GHL Conversion Rate",
        "spend": "Spend (campaign)", "impressions": "Impressions (campaign)",
        "clicks": "Clicks (campaign)", "reach": "Reach (campaign)",
        "results": "Results (campaign)", "leads": "Leads (campaign)",
        "ctr": "CTR (campaign)", "cpc": "CPC (campaign)", "cpm": "CPM (campaign)",
        "cpl": "CPL", "cost_per_result": "Cost per Result", "frequency": "Frequency",
        "conversion_rate": "Conversion Rate", "cost_per_lead": "Cost per Lead",
    }
    for metric_id, entry in METRIC_REGISTRY.items():
        metrics.append({
            "id": metric_id,
            "label": labels.get(metric_id, metric_id.replace("_", " ").title()),
            "format": entry.get("format", "integer"),
        })
    return {
        "available_metrics": metrics,
        "operators": ["+", "-", "*", "/"],
        "valid_dashboards": sorted(_VALID_DASHBOARDS),
        "format_types": ["integer", "currency", "percentage", "decimal"],
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

    registry.register(
        name="list_available_metric_fields",
        description=(
            "List every built-in metric ID the user can reference in a custom formula, "
            "along with valid operators, dashboards, and format types. Call this when the "
            "user asks 'what metrics can I use' or before creating a metric if you're unsure "
            "of the exact id."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        executor=list_available_metric_fields,
    )

    registry.register(
        name="create_custom_metric",
        description=(
            "Create a new custom metric (formula) for the user. Accepts either a friendly "
            "formula string (formula_text, e.g. 'meta_spend / ghl_revenue' or 'Meta Spend / GHL Revenue') "
            "or a structured formula_parts array. "
            "Dashboards control where the metric appears: 'clients' (Client Groups page), "
            "'campaigns' / 'adsets' / 'ads' (Marketing Hub tabs), 'leads' / 'marketing_leads' (Leads Hub). "
            "format_type: integer | currency | percentage | decimal. "
            "Use this when the user asks to 'create', 'make', or 'add' a metric."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name for the metric"},
                "formula_text": {
                    "type": "string",
                    "description": "Friendly formula string (e.g. 'meta_spend / ghl_revenue'). Operators: + - * /",
                },
                "formula_parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["metric", "operator"]},
                            "value": {"type": "string"},
                        },
                    },
                    "description": "Structured formula (alternative to formula_text)",
                },
                "description": {"type": "string", "description": "Optional longer description"},
                "format_type": {
                    "type": "string",
                    "enum": ["integer", "currency", "percentage", "decimal"],
                    "description": "How to display the result. Default: integer",
                },
                "dashboards": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["clients", "campaigns", "adsets", "ads", "leads", "marketing_leads"]},
                    "description": "Where the metric appears. Omit to auto-detect based on formula.",
                },
                "aggregation": {
                    "type": "string",
                    "enum": ["total", "average"],
                    "description": "How to aggregate across groups. 'average' triggers ratio recomputation (recommended for percentage/ratio metrics).",
                },
            },
            "required": ["name"],
        },
        executor=create_custom_metric,
    )

    registry.register(
        name="update_custom_metric",
        description=(
            "Update one or more fields on an existing custom metric. Only provided fields change. "
            "Use to rename, change the formula, change format, or retarget dashboards. "
            "Call list_custom_metrics first if you don't have the metric_id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric_id": {"type": "string", "description": "The metric id to update"},
                "name": {"type": "string"},
                "formula_text": {"type": "string", "description": "New formula as a friendly string"},
                "formula_parts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["metric", "operator"]},
                            "value": {"type": "string"},
                        },
                    },
                },
                "description": {"type": "string"},
                "format_type": {"type": "string", "enum": ["integer", "currency", "percentage", "decimal"]},
                "dashboards": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["clients", "campaigns", "adsets", "ads", "leads", "marketing_leads"]},
                },
                "aggregation": {"type": "string", "enum": ["total", "average"]},
            },
            "required": ["metric_id"],
        },
        executor=update_custom_metric,
    )

    registry.register(
        name="delete_custom_metric",
        description=(
            "Delete a custom metric by id. Confirm with the user before calling — deletion cannot be undone."
        ),
        parameters={
            "type": "object",
            "properties": {
                "metric_id": {"type": "string", "description": "The metric id to delete"},
            },
            "required": ["metric_id"],
        },
        executor=delete_custom_metric,
    )
