"""
services/dashboards_resolve.py
------------------------------
Turns dashkit bindings into ResolvedSlot dicts for POST /api/dashboards/resolve.

Every number comes from metric_orchestrator (get_metric_value / aggregate_metric)
or from the daily series the client groups already carry (DAILY_SOURCES), so a
dashboard widget and the hub tiles never disagree about a figure.

One request loads the caller's active client groups once, then resolves each
binding independently: a binding that cannot be resolved yields a null slot
and an `errors` entry rather than failing the whole request.

  no `by`        -> value slot   (aggregate across groups, optional `previous`)
  `by: 'client'` -> series slot  (one point per group, sorted + limited)
  `by: 'day'`    -> series slot  (daily rows summed across groups per date)
  shapes[i] == 'table' -> the same data as a table slot.
"""

import logging
import math
from typing import Optional

from core.constants import PRESET_ALIAS, ghl_date_bounds, previous_preset
from services.dashboards_catalog import DAILY_SOURCES, build_catalog
from services.metric_orchestrator import (
    METRIC_REGISTRY,
    aggregate_metric,
    get_metric_value,
    resolve_preset,
)

logger = logging.getLogger(__name__)

MAX_BINDINGS = 50

# Everything metric_orchestrator's source readers touch, plus the daily series.
_GROUP_PROJECTION = {
    "_id": 0,
    "id": 1,
    "name": 1,
    "facebook_cache": 1,
    "ghl_opp_cache": 1,
    "gohighlevel_cache": 1,
    "hotprospector_call_cache": 1,
    "hotprospector_cache": 1,
    **{src["field"]: 1 for src in DAILY_SOURCES.values()},
}


class BindingError(Exception):
    """A binding the resolver cannot answer; becomes a null slot plus an error entry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


async def load_groups(db, user_id: str) -> list[dict]:
    """The caller's client groups that are not Inactive."""
    return await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        _GROUP_PROJECTION,
    ).to_list(length=None)


def _filter_groups(groups: list[dict], filters: Optional[dict]) -> list[dict]:
    """`filters.client` is a list of group ids (empty = no filter); other filter keys are ignored."""
    ids = (filters or {}).get("client")
    if not ids:
        return groups
    wanted = set(ids)
    return [g for g in groups if g.get("id") in wanted]


def _finite(value) -> float:
    """NaN / inf would make the JSON response fail; read them as 0 like any other unusable value."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _preset_of(binding: dict) -> Optional[str]:
    """The binding's canonical preset, or None for a custom {from, to} window."""
    window = binding.get("window") or {"preset": "maximum"}
    if "preset" not in window:
        return None
    preset = window["preset"]
    if preset not in PRESET_ALIAS:
        raise BindingError("WINDOW_UNSUPPORTED", f"Unknown window preset '{preset}'")
    return resolve_preset(preset)


def _required_preset(binding: dict) -> str:
    """The binding's canonical preset; custom {from, to} windows are only for by: 'day'."""
    preset = _preset_of(binding)
    if preset is None:
        raise BindingError("WINDOW_UNSUPPORTED", "Custom date windows are only supported with by: 'day'")
    return preset


# --- previous-window coverage --------------------------------------------------
# metric_orchestrator's readers never come back empty-handed: a missing preset
# bucket falls back to `maximum` (ghl_opp, hp_call) or the legacy flat insights
# (meta), and the general `ghl` source ignores the preset entirely. Reading the
# previous window through them would compare against all-time numbers, so a
# group only counts towards `previous` when every source the metric reads holds
# a real bucket for that window.

def _metric_sources(metric_id: str) -> set[str]:
    entry = METRIC_REGISTRY.get(metric_id) or {}
    if entry.get("aggregation") == "recompute":
        num_id, den_id = entry["components"]
        return _metric_sources(num_id) | _metric_sources(den_id)
    return {entry.get("source")}


def _has_bucket(group: dict, source: Optional[str], preset: str) -> bool:
    if source == "meta":
        fb = group.get("facebook_cache") or {}
        bucket = (fb.get("presets") or {}).get(preset) or fb.get(preset) or {}
        return bool((bucket.get("metrics") or {}).get("insights"))
    if source == "ghl_opp":
        bucket = (group.get("ghl_opp_cache") or {}).get(preset)
        return isinstance(bucket, dict) and bool(bucket)
    if source == "hp_call":
        bucket = (group.get("hotprospector_call_cache") or {}).get(preset)
        return isinstance(bucket, dict) and bool(bucket)
    return False  # `ghl` general metrics are not windowed at all


def _covers(group: dict, sources: set[str], preset: str) -> bool:
    return all(_has_bucket(group, s, preset) for s in sources)


def _previous_preset_for(binding: dict, preset: str) -> Optional[str]:
    return previous_preset(preset) if binding.get("compare") == "previous" else None


# --- resolvers ---------------------------------------------------------------

def _limited(points: list[dict], binding: dict) -> list[dict]:
    """The first `limit` points."""
    limit = binding.get("limit")
    if isinstance(limit, (int, float)) and limit >= 0:
        return points[: int(limit)]
    return points


def _sorted_limited(points: list[dict], binding: dict, default_sort: str) -> list[dict]:
    """Sort by value (ties by label), then keep the first `limit`."""
    sort = binding.get("sort") or default_sort
    if sort == "asc":
        points = sorted(points, key=lambda p: (p["value"], p["label"]))
    else:
        points = sorted(points, key=lambda p: (-p["value"], p["label"]))
    return _limited(points, binding)


def _resolve_value(metric: dict, binding: dict, groups: list[dict]) -> dict:
    preset = _required_preset(binding)
    slot = {
        "shape": "value",
        "value": _finite(aggregate_metric(metric["key"], groups, preset)),
        "format": metric["format"],
        "polarity": metric["polarity"],
    }
    prev = _previous_preset_for(binding, preset)
    if prev:
        # Compare only when every group with data for the current window also
        # has data for the previous one; a group with neither stays out of both.
        sources = _metric_sources(metric["key"])
        contributing = [g for g in groups if _covers(g, sources, preset)]
        if contributing and all(_covers(g, sources, prev) for g in contributing):
            slot["previous"] = _finite(aggregate_metric(metric["key"], contributing, prev))
    return slot


def _resolve_by_client(metric: dict, binding: dict, groups: list[dict]) -> dict:
    preset = _required_preset(binding)
    prev = _previous_preset_for(binding, preset)
    sources = _metric_sources(metric["key"]) if prev else set()
    points = []
    for g in groups:
        point = {
            "label": g.get("name") or g.get("id") or "",
            "value": _finite(get_metric_value(metric["key"], g, preset)),
        }
        if prev and _covers(g, sources, prev):
            point["previous"] = _finite(get_metric_value(metric["key"], g, prev))
        points.append(point)
    return {
        "shape": "series",
        "points": _sorted_limited(points, binding, "desc"),
        "format": metric["format"],
        "polarity": metric["polarity"],
    }


def _day_bounds(binding: dict) -> tuple[Optional[str], Optional[str]]:
    preset = _preset_of(binding)
    if preset is not None:
        return ghl_date_bounds(preset)
    window = binding["window"]
    return window.get("from"), window.get("to")


def _resolve_by_day(metric: dict, binding: dict, groups: list[dict]) -> dict:
    source = DAILY_SOURCES.get(metric["key"])
    if not source:
        raise BindingError("DAY_SERIES_UNAVAILABLE", f"'{metric['key']}' has no daily series")
    field, row_key = source["field"], source["key"]
    start, end = _day_bounds(binding)

    totals: dict[str, float] = {}
    for g in groups:
        for row in g.get(field) or []:
            day = row.get("date") if isinstance(row, dict) else None
            if not isinstance(day, str):
                continue
            day = day[:10]
            if (start and day < start) or (end and day > end):
                continue
            totals[day] = totals.get(day, 0.0) + _finite(row.get(row_key))

    # `limit` keeps the most recent days; `sort` then orders them by date (asc by default).
    points = [{"label": day, "value": totals[day]} for day in sorted(totals)]
    limit = binding.get("limit")
    if isinstance(limit, (int, float)) and limit >= 0:
        points = points[len(points) - min(int(limit), len(points)):]
    if binding.get("sort") == "desc":
        points.reverse()
    return {
        "shape": "series",
        "points": points,
        "format": metric["format"],
        "polarity": metric["polarity"],
    }


def _as_table(slot: dict, metric: dict, by: Optional[str]) -> dict:
    columns = [
        {"key": "label", "label": "Day" if by == "day" else "Client"},
        {"key": "value", "label": metric["label"], "format": metric["format"]},
    ]
    if slot["shape"] == "value":
        rows = [{"label": "Total", "value": slot["value"]}]
    else:
        rows = [{"label": p["label"], "value": p["value"]} for p in slot["points"]]
    return {"shape": "table", "columns": columns, "rows": rows}


def _resolve_one(binding: dict, shape: Optional[str], groups: list[dict], metrics: dict) -> dict:
    metric = metrics.get(binding.get("metric"))
    if metric is None:
        raise BindingError("METRIC_UNKNOWN", f"Unknown metric '{binding.get('metric')}'")

    scoped = _filter_groups(groups, binding.get("filters"))
    by = binding.get("by")
    if by is None:
        slot = _resolve_value(metric, binding, scoped)
    elif by == "client":
        slot = _resolve_by_client(metric, binding, scoped)
    elif by == "day":
        slot = _resolve_by_day(metric, binding, scoped)
    else:
        raise BindingError("DIMENSION_UNKNOWN", f"Unknown dimension '{by}'")

    if shape == "table":
        slot = _as_table(slot, metric, by)
    return slot


async def resolve_bindings(db, user_id: str, bindings: list[dict], shapes: Optional[list] = None) -> dict:
    """
    {"slots": [ResolvedSlot | None, ...], "errors": [{"index", "code", "message"}, ...]}
    with one slot per binding, in order. The caller enforces MAX_BINDINGS.
    """
    if not bindings:
        return {"slots": [], "errors": []}

    groups = await load_groups(db, user_id)
    metrics = {m["key"]: m for m in build_catalog()["metrics"]}
    shapes = shapes or []

    slots, errors = [], []
    for i, binding in enumerate(bindings):
        shape = shapes[i] if i < len(shapes) else None
        try:
            slots.append(_resolve_one(binding, shape, groups, metrics))
        except BindingError as e:
            slots.append(None)
            errors.append({"index": i, "code": e.code, "message": e.message})
        except Exception:
            logger.exception(f"Resolving dashboard binding {i} failed for {user_id}: {binding!r}")
            slots.append(None)
            errors.append({"index": i, "code": "RESOLVE_FAILED", "message": "This widget's data could not be loaded"})
    return {"slots": slots, "errors": errors}
