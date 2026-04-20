"""
services/metric_orchestrator.py
-------------------------------
Centralized metric resolution and formula evaluation.

Single source of truth for:
  - Metric ID → data source path mapping (METRIC_REGISTRY)
  - Reading a single metric value from a client_group document
  - Evaluating custom formula metrics
  - Aggregating metrics across multiple client groups

Consumers:
  - services/alert_service.py (alert condition evaluation)
  - ai/tools/custom_metrics_tools.py (AI custom metric tool)
  - ai/tools/summary_tools.py (future: use get_metric_value)
  - routers/client_groups.py (future: use for computed fields)

When adding a new base metric:
  1. Add one entry to METRIC_REGISTRY
  2. Every consumer automatically picks it up — no drift.
"""
import ast
import logging
from typing import Any, Callable, Optional

from core.constants import PRESET_ALIAS

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Source readers — functions that extract a source dict from a group document
# ─────────────────────────────────────────────────────────────────────────────

def _meta_insights(group: dict, preset: str) -> dict:
    """Read Meta insights for a specific preset, falling back to legacy flat location."""
    fb = group.get("facebook_cache") or {}
    preset_doc = fb.get(preset) or {}
    insights = (preset_doc.get("metrics") or {}).get("insights") or {}
    if not insights:
        # Legacy flat shape
        insights = (fb.get("metrics") or {}).get("insights") or {}
    return insights


def _ghl_opp_stats(group: dict, preset: str) -> dict:
    """
    Read GHL opp stats for a preset, falling back to maximum, then legacy cache.
    Order: ghl_opp_cache[preset] → ghl_opp_cache.maximum → gohighlevel_cache.metrics.opportunity_stats
    """
    opp_cache = group.get("ghl_opp_cache") or {}
    preset_stats = opp_cache.get(preset)
    if preset_stats and isinstance(preset_stats, dict):
        return preset_stats
    max_stats = opp_cache.get("maximum")
    if max_stats and isinstance(max_stats, dict):
        return max_stats
    legacy = (group.get("gohighlevel_cache") or {}).get("metrics") or {}
    return legacy.get("opportunity_stats") or {}


def _ghl_metrics(group: dict, preset: str) -> dict:
    """Read general GHL metrics (tag_breakdown, total_contacts, etc.)."""
    return (group.get("gohighlevel_cache") or {}).get("metrics") or {}


def _ghl_tag_count(group: dict, preset: str, tag_name: str) -> float:
    """Read a single tag count from tag_breakdown. Case-insensitive match."""
    tag_breakdown = _ghl_metrics(group, preset).get("tag_breakdown") or {}
    # Try exact match first
    if tag_name in tag_breakdown:
        return float(tag_breakdown[tag_name])
    # Case-insensitive
    lower = tag_name.lower()
    for k, v in tag_breakdown.items():
        if k.lower() == lower:
            return float(v)
    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Metric registry
# ─────────────────────────────────────────────────────────────────────────────
# Each entry describes how to pull a single metric from a client_group document.
#   source: the reader function to pull the source dict
#   path:   the key inside that dict (None means the whole dict is the value)
#   aggregation: "sum" | "weighted_ratio" | "recompute"
#     - sum: simple sum across groups
#     - recompute: derived metric, recompute from components after aggregation
#   components: for recompute, the (numerator_metric, denominator_metric) tuple
#   format: "integer" | "currency" | "percentage" | "decimal"

METRIC_REGISTRY: dict[str, dict] = {
    # ── Meta group-level (Client Groups page) ──
    "meta_spend":       {"source": "meta", "path": "spend",       "aggregation": "sum",       "format": "currency"},
    "meta_impressions": {"source": "meta", "path": "impressions", "aggregation": "sum",       "format": "integer"},
    "meta_clicks":      {"source": "meta", "path": "clicks",      "aggregation": "sum",       "format": "integer"},
    "meta_reach":       {"source": "meta", "path": "reach",       "aggregation": "sum",       "format": "integer"},
    "meta_results":     {"source": "meta", "path": "results",     "aggregation": "sum",       "format": "integer"},
    "meta_leads":       {"source": "meta", "path": "results",     "aggregation": "sum",       "format": "integer"},
    "meta_ctr":         {"source": "meta", "path": "ctr",         "aggregation": "recompute", "components": ("meta_clicks", "meta_impressions"), "scale": 100, "format": "percentage"},
    "meta_cpc":         {"source": "meta", "path": "cpc",         "aggregation": "recompute", "components": ("meta_spend", "meta_clicks"),       "format": "currency"},
    "meta_cpm":         {"source": "meta", "path": "cpm",         "aggregation": "recompute", "components": ("meta_spend", "meta_impressions"),  "scale": 1000, "format": "currency"},

    # ── Meta campaign-level (Marketing Hub — same paths, different data context) ──
    "spend":            {"source": "meta", "path": "spend",       "aggregation": "sum",       "format": "currency"},
    "impressions":      {"source": "meta", "path": "impressions", "aggregation": "sum",       "format": "integer"},
    "clicks":           {"source": "meta", "path": "clicks",      "aggregation": "sum",       "format": "integer"},
    "reach":            {"source": "meta", "path": "reach",       "aggregation": "sum",       "format": "integer"},
    "results":          {"source": "meta", "path": "results",     "aggregation": "sum",       "format": "integer"},
    "leads":            {"source": "meta", "path": "results",     "aggregation": "sum",       "format": "integer"},
    "ctr":              {"source": "meta", "path": "ctr",         "aggregation": "recompute", "components": ("clicks", "impressions"),   "scale": 100, "format": "percentage"},
    "cpc":              {"source": "meta", "path": "cpc",         "aggregation": "recompute", "components": ("spend", "clicks"),         "format": "currency"},
    "cpm":              {"source": "meta", "path": "cpm",         "aggregation": "recompute", "components": ("spend", "impressions"),    "scale": 1000, "format": "currency"},
    "cpl":              {"source": "meta", "path": "cost_per_result", "aggregation": "recompute", "components": ("spend", "results"),    "format": "currency"},
    "cost_per_result":  {"source": "meta", "path": "cost_per_result", "aggregation": "recompute", "components": ("spend", "results"),    "format": "currency"},
    "frequency":        {"source": "meta", "path": "frequency",   "aggregation": "recompute", "components": ("impressions", "reach"),    "format": "decimal"},

    # ── GHL Opportunities (API-backed, per-preset cache) ──
    "ghl_won_opps":       {"source": "ghl_opp", "path": "won",       "aggregation": "sum", "format": "integer"},
    "ghl_lost_opps":      {"source": "ghl_opp", "path": "lost",      "aggregation": "sum", "format": "integer"},
    "ghl_open_opps":      {"source": "ghl_opp", "path": "open",      "aggregation": "sum", "format": "integer"},
    "ghl_abandoned_opps": {"source": "ghl_opp", "path": "abandoned", "aggregation": "sum", "format": "integer"},
    "ghl_total_opps":     {"source": "ghl_opp", "path": "total_opportunities", "aggregation": "sum", "format": "integer"},
    "ghl_revenue":        {"source": "ghl_opp", "path": "won_revenue",         "aggregation": "sum", "format": "currency"},
    "ghl_conversion":     {"source": "ghl_opp", "path": "won",       "aggregation": "recompute", "components": ("ghl_won_opps", "ghl_total_opps"), "scale": 100, "format": "percentage"},

    # ── GHL general metrics ──
    "ghl_contacts":   {"source": "ghl", "path": "total_contacts", "aggregation": "sum", "format": "integer"},
    "ghl_leads":      {"source": "ghl", "path": "total_contacts", "aggregation": "sum", "format": "integer"},
    "total_tags":     {"source": "ghl", "path": "tag_breakdown",  "aggregation": "sum", "format": "integer", "transform": "count_keys"},

    # ── Hybrid/derived (Client Groups dashboard) ──
    "conversion_rate": {"aggregation": "recompute", "components": ("ghl_contacts", "meta_leads"), "scale": 100, "format": "percentage"},
    "cost_per_lead":   {"aggregation": "recompute", "components": ("meta_spend", "ghl_contacts"), "format": "currency"},
    "engagement_rate": {"aggregation": "recompute", "components": ("meta_clicks", "meta_impressions"), "scale": 100, "format": "percentage"},
}


_SOURCE_READERS: dict[str, Callable[[dict, str], dict]] = {
    "meta": _meta_insights,
    "ghl_opp": _ghl_opp_stats,
    "ghl": _ghl_metrics,
}


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def resolve_preset(preset: Optional[str]) -> str:
    """Normalize an incoming preset alias to a canonical key."""
    return PRESET_ALIAS.get(preset or "maximum", "maximum")


def get_metric_value(metric_id: str, group: dict, preset: str = "maximum") -> float:
    """
    Return the numeric value of a metric for a single client group at the given preset.
    Returns 0.0 if the metric is unknown or the data path is missing.
    Handles tag metrics (tag_<slug>) dynamically.
    """
    preset = resolve_preset(preset)

    # Dynamic tag metric: tag_<tagname>
    if metric_id.startswith("tag_"):
        tag_name = metric_id[4:].replace("_", " ")
        return _ghl_tag_count(group, preset, tag_name)

    entry = METRIC_REGISTRY.get(metric_id)
    if not entry:
        # Normalize a few legacy aliases
        alt = metric_id.replace("-", "_")
        entry = METRIC_REGISTRY.get(alt)
        if not entry:
            return 0.0

    # Recomputed/derived metric — resolve components, divide
    if entry.get("aggregation") == "recompute":
        num_id, den_id = entry["components"]
        num = get_metric_value(num_id, group, preset)
        den = get_metric_value(den_id, group, preset)
        if den == 0:
            return 0.0
        scale = entry.get("scale", 1)
        return (num / den) * scale

    # Direct source read
    source = entry.get("source")
    reader = _SOURCE_READERS.get(source)
    if not reader:
        return 0.0
    source_dict = reader(group, preset) or {}
    path = entry.get("path")
    raw = source_dict.get(path) if path else source_dict

    # Optional transform
    if entry.get("transform") == "count_keys" and isinstance(raw, dict):
        return float(len(raw))

    return _safe_float(raw)


def aggregate_metric(metric_id: str, groups: list[dict], preset: str = "maximum") -> float:
    """
    Aggregate a metric across a list of client groups.
    - sum metrics → plain sum
    - recompute metrics → sum components, then recompute
    """
    preset = resolve_preset(preset)

    if metric_id.startswith("tag_"):
        return sum(get_metric_value(metric_id, g, preset) for g in groups)

    entry = METRIC_REGISTRY.get(metric_id) or METRIC_REGISTRY.get(metric_id.replace("-", "_"))
    if not entry:
        return 0.0

    if entry.get("aggregation") == "recompute":
        num_id, den_id = entry["components"]
        num_total = aggregate_metric(num_id, groups, preset)
        den_total = aggregate_metric(den_id, groups, preset)
        if den_total == 0:
            return 0.0
        return (num_total / den_total) * entry.get("scale", 1)

    return sum(get_metric_value(metric_id, g, preset) for g in groups)


def build_row(group: dict, preset: str = "maximum") -> dict:
    """
    Build a flat {metric_id: value} dict for a group at a given preset.
    Used by formula evaluators to resolve metric references.
    """
    preset = resolve_preset(preset)
    row = {}
    for metric_id in METRIC_REGISTRY:
        row[metric_id] = get_metric_value(metric_id, group, preset)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Formula evaluation
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_AST_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Num,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd, ast.Mod, ast.Pow,
)


def _normalize_operator(op: str) -> str:
    return {"×": "*", "÷": "/", "x": "*"}.get(op, op)


def evaluate_formula(
    formula_parts: list,
    group: dict,
    preset: str = "maximum",
    row: Optional[dict] = None,
) -> float:
    """
    Evaluate a custom metric formula against a client group document.

    formula_parts: list of {"type": "metric"|"operator", "value": <str>}
    group:         the client_group document
    preset:        date preset for the metric data
    row:           optional pre-built row (from build_row) to avoid re-resolution

    Returns 0.0 on empty formula, invalid expression, or division error.
    """
    if not formula_parts:
        return 0.0

    if row is None:
        row = build_row(group, preset)

    expression_parts = []
    for part in formula_parts:
        ptype = part.get("type")
        pval = part.get("value", "")

        if ptype == "operator":
            expression_parts.append(_normalize_operator(pval))
        elif ptype == "metric":
            # Tag metrics resolved live (not in row)
            if pval.startswith("tag_"):
                val = get_metric_value(pval, group, preset)
            else:
                val = row.get(pval)
                if val is None:
                    # Legacy key normalization (dashes → underscores)
                    val = row.get(pval.replace("-", "_"))
                if val is None:
                    val = 0
            expression_parts.append(str(_safe_float(val)))
        # Silently skip unknown part types

    expression = " ".join(expression_parts)
    if not expression.strip():
        return 0.0

    try:
        tree = ast.parse(expression, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_AST_NODES):
                logger.warning("Disallowed AST node in formula: %s", type(node).__name__)
                return 0.0
        result = eval(compile(tree, "<metric_formula>", "eval"))
        # NaN / inf check
        if result != result or result in (float("inf"), float("-inf")):
            return 0.0
        return float(result)
    except ZeroDivisionError:
        return 0.0
    except Exception as e:
        logger.warning("Formula evaluation error: %s (expr=%r)", e, expression)
        return 0.0


def evaluate_formula_aggregated(
    formula_parts: list,
    groups: list[dict],
    preset: str = "maximum",
    aggregation: str = "total",
) -> float:
    """
    Evaluate a formula across multiple groups.

    aggregation:
      - "total": evaluate per-group then sum results (default)
      - "recompute": aggregate each base metric first, then evaluate formula once
                     (correct for ratios like CPL = spend/leads)
    """
    if not formula_parts or not groups:
        return 0.0

    if aggregation == "recompute":
        # Build an aggregated pseudo-row from component totals
        agg_row = {}
        for metric_id in METRIC_REGISTRY:
            if METRIC_REGISTRY[metric_id].get("aggregation") == "sum":
                agg_row[metric_id] = aggregate_metric(metric_id, groups, preset)
        # For tag metrics referenced in the formula, compute aggregated counts
        for part in formula_parts:
            if part.get("type") == "metric" and part.get("value", "").startswith("tag_"):
                agg_row[part["value"]] = sum(
                    get_metric_value(part["value"], g, preset) for g in groups
                )
        return evaluate_formula(formula_parts, {}, preset, row=agg_row)

    # "total": per-group evaluation, sum the results
    return sum(evaluate_formula(formula_parts, g, preset) for g in groups)
