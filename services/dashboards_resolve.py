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
from typing import Optional

from core.constants import PRESET_ALIAS, ghl_date_bounds, previous_preset
from services.dashboards_catalog import DAILY_SOURCES, build_catalog
from services.metric_orchestrator import aggregate_metric, get_metric_value, resolve_preset

logger = logging.getLogger(__name__)

MAX_BINDINGS = 50

# daily field -> the per-row key holding that day's figure
_DAILY_ROW_KEYS = {
    "meta_daily_spend": "spend",
    "ghl_daily_leads": "leads",
    "hp_daily_calls": "calls",
}

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
    **{field: 1 for field in _DAILY_ROW_KEYS},
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
    """`filters.client` is a list of group ids; any other filter key is ignored."""
    ids = (filters or {}).get("client")
    if ids is None:
        return groups
    wanted = set(ids)
    return [g for g in groups if g.get("id") in wanted]


def _preset_of(binding: dict) -> Optional[str]:
    """The binding's canonical preset, or None for a custom {from, to} window."""
    window = binding.get("window") or {"preset": "maximum"}
    if "preset" not in window:
        return None
    preset = window["preset"]
    if preset not in PRESET_ALIAS:
        raise BindingError("WINDOW_UNSUPPORTED", f"Unknown window preset '{preset}'")
    return resolve_preset(preset)


def _limited(points: list[dict], binding: dict) -> list[dict]:
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
    preset = _preset_of(binding)
    if preset is None:
        raise BindingError("WINDOW_UNSUPPORTED", "Custom date windows are only supported with by: 'day'")
    slot = {
        "shape": "value",
        "value": aggregate_metric(metric["key"], groups, preset),
        "format": metric["format"],
        "polarity": metric["polarity"],
    }
    prev = previous_preset(preset) if binding.get("compare") == "previous" else None
    if prev:
        slot["previous"] = aggregate_metric(metric["key"], groups, prev)
    return slot


def _resolve_by_client(metric: dict, binding: dict, groups: list[dict]) -> dict:
    preset = _preset_of(binding)
    if preset is None:
        raise BindingError("WINDOW_UNSUPPORTED", "Custom date windows are only supported with by: 'day'")
    prev = previous_preset(preset) if binding.get("compare") == "previous" else None
    points = []
    for g in groups:
        point = {"label": g.get("name") or g.get("id") or "", "value": get_metric_value(metric["key"], g, preset)}
        if prev:
            point["previous"] = get_metric_value(metric["key"], g, prev)
        points.append(point)
    return {
        "shape": "series",
        "points": _sorted_limited(points, binding, "desc"),
        "format": metric["format"],
        "polarity": metric["polarity"],
    }


def _day_bounds(binding: dict) -> tuple[Optional[str], Optional[str]]:
    window = binding.get("window") or {"preset": "maximum"}
    if "preset" in window:
        return ghl_date_bounds(_preset_of(binding))
    return window.get("from"), window.get("to")


def _resolve_by_day(metric: dict, binding: dict, groups: list[dict]) -> dict:
    field = DAILY_SOURCES.get(metric["key"])
    if not field:
        raise BindingError("DAY_SERIES_UNAVAILABLE", f"'{metric['key']}' has no daily series")
    row_key = _DAILY_ROW_KEYS[field]
    start, end = _day_bounds(binding)

    totals: dict[str, float] = {}
    for g in groups:
        for row in g.get(field) or []:
            day = row.get("date")
            if not isinstance(day, str):
                continue
            day = day[:10]
            if (start and day < start) or (end and day > end):
                continue
            try:
                value = float(row.get(row_key) or 0)
            except (TypeError, ValueError):
                value = 0.0
            totals[day] = totals.get(day, 0.0) + value

    # A day series sorts by date (ascending by default), then keeps the first `limit`.
    points = [{"label": day, "value": totals[day]} for day in sorted(totals)]
    if binding.get("sort") == "desc":
        points.reverse()
    return {
        "shape": "series",
        "points": _limited(points, binding),
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
