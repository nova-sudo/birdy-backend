"""
services/dashboards_catalog.py
------------------------------
The metric catalog served to dashkit: what a dashboard widget may bind to.

Built from METRIC_REGISTRY (the orchestrator's metric definitions) so the
dashboard builder and the existing hubs can never disagree about what a metric
is. v1 splits only by `client` and `day`; `day` exists only for metrics with a
daily source (the fields /api/client-groups/daily serves).
"""

from core.constants import PRESET_ALIAS
from services.metric_orchestrator import METRIC_REGISTRY

# metric key -> daily field in the daily-series data
DAILY_SOURCES: dict[str, str] = {
    "leads": "ghl_daily_leads",
    "meta_leads": "ghl_daily_leads",
    "spend": "meta_daily_spend",
    "meta_spend": "meta_daily_spend",
    "calls": "hp_daily_calls",
}

_DIMENSIONS = [
    {"key": "client", "label": "Client"},
    {"key": "day", "label": "Day"},
]

_FORMATS = {"integer", "currency", "percentage", "decimal", "compact"}

# Metrics where a falling number is good news.
_LOWER_IS_BETTER_MARKERS = ("cpc", "cpl", "cpm", "cost_per", "cpa")


def _label(text: str) -> str:
    # Capitalise each word's first letter only: str.title() would turn "7d" into "7D".
    return " ".join(w[:1].upper() + w[1:] for w in text.split("_"))


def _polarity(key: str) -> str:
    if any(marker in key for marker in _LOWER_IS_BETTER_MARKERS):
        return "lower_is_better"
    return "higher_is_better"


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
        })

    windows = [
        {"preset": preset, "label": _label(preset)}
        for preset in PRESET_ALIAS
        if preset != "data_maximum"
    ]

    return {"metrics": metrics, "dimensions": [dict(d) for d in _DIMENSIONS], "windows": windows}
