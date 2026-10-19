"""
services/dashboards_catalog.py
------------------------------
The metric catalog served to dashkit: what a dashboard widget may bind to.

Built from METRIC_REGISTRY (the orchestrator's metric definitions) so the
dashboard builder and the existing hubs can never disagree about what a metric
is. v1 splits only by `client` and `day`; `day` exists only for metrics with a
daily source (the fields /api/client-groups/daily serves). `over` says whether a
metric can be bucketed over time (day / week / month) and `additive` whether its
parts add up to a whole (so a donut means something); see DAILY_SOURCES.
"""

from core.constants import PRESET_ALIAS
from services.metric_orchestrator import METRIC_REGISTRY

# metric key -> where its daily series lives: the client-group field holding the
# rows and the per-row key. Every additive metric whose daily rows measure the
# same thing as its value source is listed, so a dashboard can chart it over
# day / week / month by summing rows. Not listed, because they cannot be
# bucketed by day: `reach` / `meta_reach` and `hp_leads_with_calls` (distinct
# counts: a day's distinct leads summed over days counts a repeat caller once
# per day; hp_daily_calls still stores the per-day figure as `leads_with_calls`),
# `hp_leads` (HotProspector leads carry no date; the preset value is a lifetime
# count) and `total_tags` (a snapshot of the tag list). The ratios are not listed
# either: they are recomputed from their components' rows (see `over` below).
DAILY_SOURCES: dict[str, dict[str, str]] = {
    "spend":              {"field": "meta_daily_spend", "key": "spend"},
    "meta_spend":         {"field": "meta_daily_spend", "key": "spend"},
    "impressions":        {"field": "meta_daily_spend", "key": "impressions"},
    "meta_impressions":   {"field": "meta_daily_spend", "key": "impressions"},
    "clicks":             {"field": "meta_daily_spend", "key": "clicks"},
    "meta_clicks":        {"field": "meta_daily_spend", "key": "clicks"},
    "results":            {"field": "meta_daily_spend", "key": "results"},
    "meta_results":       {"field": "meta_daily_spend", "key": "results"},
    "leads":              {"field": "meta_daily_spend", "key": "leads"},
    "meta_leads":         {"field": "meta_daily_spend", "key": "leads"},
    "ghl_leads":          {"field": "ghl_daily_leads",  "key": "leads"},
    "ghl_contacts":       {"field": "ghl_daily_leads",  "key": "leads"},
    "ghl_won_opps":       {"field": "ghl_daily_leads",  "key": "opps_won"},
    "ghl_lost_opps":      {"field": "ghl_daily_leads",  "key": "opps_lost"},
    "ghl_open_opps":      {"field": "ghl_daily_leads",  "key": "opps_open"},
    "ghl_abandoned_opps": {"field": "ghl_daily_leads",  "key": "opps_abandoned"},
    "ghl_total_opps":     {"field": "ghl_daily_leads",  "key": "opps_total"},
    "ghl_revenue":        {"field": "ghl_daily_leads",  "key": "revenue"},
    "hp_total_calls":      {"field": "hp_daily_calls",  "key": "calls"},
    "hp_inbound":          {"field": "hp_daily_calls",  "key": "inbound"},
    "hp_outbound":         {"field": "hp_daily_calls",  "key": "outbound"},
    "hp_answered_calls":   {"field": "hp_daily_calls",  "key": "answered"},
    "hp_transfers":        {"field": "hp_daily_calls",  "key": "transfers"},
    "hp_talk_time":        {"field": "hp_daily_calls",  "key": "talk_time"},
}

# Sum-aggregated metrics whose parts do not add up to a whole: unique counts
# (a person reached by two campaigns is one person; a lead dialled on two days is
# one lead) and a count of distinct tags.
_NON_ADDITIVE_KEYS = {"reach", "meta_reach", "hp_leads_with_calls", "total_tags"}

_DIMENSIONS = [
    {"key": "client", "label": "Client"},
    {"key": "day", "label": "Day"},
]

_FORMATS = {"integer", "currency", "percentage", "decimal", "compact"}

# Metrics where a falling number is good news: cost metrics by marker, plus the
# opportunity counts that measure losses.
_LOWER_IS_BETTER_MARKERS = ("cpc", "cpl", "cpm", "cost_per", "cpa")
_LOWER_IS_BETTER_KEYS = {"ghl_lost_opps", "ghl_abandoned_opps"}

# Words shown in capitals in metric labels ("meta_ctr" -> "Meta CTR").
_ACRONYMS = {"ctr", "cpc", "cpm", "cpl", "cpa", "ghl", "hp", "roas", "crm"}

# Window labels that are not just the preset key in sentence case.
_WINDOW_LABELS = {
    "last_7d": "Last 7 days",
    "last_14d": "Last 14 days",
    "last_30d": "Last 30 days",
    "maximum": "All time",
    "this_week": "This week",
}


def _label(text: str) -> str:
    """A metric key as a label: words capitalised, known acronyms in capitals."""
    return " ".join(
        w.upper() if w.lower() in _ACRONYMS else w[:1].upper() + w[1:]
        for w in text.split("_")
    )


def _window_label(preset: str) -> str:
    """A window preset as a label in sentence case ("this_month" -> "This month")."""
    if preset in _WINDOW_LABELS:
        return _WINDOW_LABELS[preset]
    text = preset.replace("_", " ")
    return text[:1].upper() + text[1:]


def _polarity(key: str) -> str:
    if key in _LOWER_IS_BETTER_KEYS or any(marker in key for marker in _LOWER_IS_BETTER_MARKERS):
        return "lower_is_better"
    return "higher_is_better"


def _supports_over(key: str) -> bool:
    """Whether a metric can be bucketed over time: it has daily rows, or it is a
    ratio whose numerator and denominator both can (Σnum ÷ Σden per bucket)."""
    spec = METRIC_REGISTRY[key]
    if spec.get("aggregation") == "recompute":
        return all(_supports_over(part) for part in spec["components"])
    return key in DAILY_SOURCES


def build_catalog() -> dict:
    """Metrics, dimensions and windows in dashkit's Catalog shape. Pure and deterministic."""
    metrics = []
    for key, spec in METRIC_REGISTRY.items():
        fmt = spec.get("format")
        if fmt not in _FORMATS:
            continue
        metrics.append({
            "key": key,
            "label": _label(key),
            "format": fmt,
            "dimensions": ["client", "day"] if key in DAILY_SOURCES else ["client"],
            "aggregation": "avg" if spec.get("aggregation") == "recompute" else "sum",
            "polarity": _polarity(key),
            "over": _supports_over(key),
            "additive": spec.get("aggregation") != "recompute" and key not in _NON_ADDITIVE_KEYS,
        })

    # Several PRESET_ALIAS keys name the same cached window (data_maximum ->
    # maximum, this_week -> this_week_mon_today); offer each window once, under
    # the first key that names it.
    windows, seen_targets = [], set()
    for preset, target in PRESET_ALIAS.items():
        if target in seen_targets:
            continue
        seen_targets.add(target)
        windows.append({"preset": preset, "label": _window_label(preset)})

    return {"metrics": metrics, "dimensions": [dict(d) for d in _DIMENSIONS], "windows": windows}
