"""
services/dashboards_query.py
----------------------------
dashkit queries -> dashkit Frames for POST /api/dashboards/query (spec §1/§2).

One request loads the caller's active client groups once, reading only the
daily series its metrics need, then answers each query on its own: a query
that cannot be answered (bad shape, unknown metric, a split or grain the
metric does not support) yields None and a log line, and the rest still work.

Where a number comes from:
  - Metrics with daily history (the catalog's `over: true`: DAILY_SOURCES, and
    ratios whose two parts both have it) are summed from daily rows inside the
    window, so a total, a row, a time bucket and a sparkline always agree.
  - Other metrics read the cached preset bucket (get_metric_value /
    aggregate_metric); they have no value for a custom range or a bucket.
  - `maximum` totals and rows read the cached `maximum` bucket for every
    metric: daily history is capped (Meta keeps ~400 days), the bucket is all
    time. Its buckets and sparkline still come from the daily rows.
  - Ratios (METRIC_REGISTRY aggregation `recompute`) are scale × Σnum ÷ Σden at
    every level (total, row, bucket, previous, spark day); a zero denominator
    is None, never 0 or an average of ratios.

Previous period (core.constants.previous_window): a group's daily rows answer
it when its history reaches back to the period's start (the T14 guard in
dashboards_resolve); otherwise the cached previous_preset bucket answers it
when that bucket covers exactly the previous period (today -> yesterday, or a
this_* window on the period's last day); otherwise it is unknown and the
value is None. A group with no rows of a series at all contributes nothing to
either period.

Frame shapes follow dashkit's reference data source
(src/adapters/mock-data-source.ts): no by/over -> totals only; `by` -> one
row per dimension value (sort with nulls last, then limit); `over` -> one row
per bucket keyed by its ISO start date (weeks start on Monday), sort and
limit ignored; over + by -> one metric, columns are the top `limit` (default
5, max 8) clients by the window total.
"""

import logging
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.constants import PRESET_ALIAS, previous_preset, previous_window, window_range
from services.dashboards_catalog import DAILY_SOURCES, build_catalog
from services.dashboards_resolve import (
    _covers,
    _daily_rows,
    _filter_groups,
    _finite,
    _metric_sources,
    load_groups,
)
from services.metric_orchestrator import METRIC_REGISTRY, aggregate_metric, get_metric_value

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 5
MAX_TOP_N = 8


class QueryError(Exception):
    """A query this engine cannot answer; its frame is None."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")


# --- query shape (dashkit QuerySchema, src/core/query.ts) -----------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _QueryMetric(_Strict):
    metric: str
    label: Optional[str] = None


class _PresetWindow(_Strict):
    preset: str


class _RangeWindow(_Strict):
    start: str = Field(alias="from")
    to: str


class _Sort(_Strict):
    metric: str
    dir: Literal["asc", "desc"]


class Query(_Strict):
    metrics: List[_QueryMetric] = Field(min_length=1, max_length=20)
    by: Optional[str] = None
    over: Optional[Literal["day", "week", "month"]] = None
    window: Union[_PresetWindow, _RangeWindow]
    filters: Optional[Dict[str, List[str]]] = None
    compare: Optional[Literal["previous"]] = None
    sort: Optional[_Sort] = None
    limit: Optional[int] = Field(default=None, ge=1, le=100)
    spark: Optional[bool] = None

    @model_validator(mode="after")
    def _refine(self):
        keys = [m.metric for m in self.metrics]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate metric")
        if self.sort and self.sort.metric not in keys:
            raise ValueError(f"Sort metric {self.sort.metric!r} is not in metrics")
        return self

    def window_dict(self) -> dict:
        if isinstance(self.window, _PresetWindow):
            return {"preset": self.window.preset}
        return {"from": self.window.start, "to": self.window.to}


# --- daily series -----------------------------------------------------------------

@dataclass
class _Series:
    """One group's daily values of one row key: sorted days, prefix sums, and a day lookup."""
    days: List[str]
    prefix: List[float]
    by_day: Dict[str, float]

    def total(self, start: str, end: str, only_days: Optional[set]) -> float:
        if only_days is not None:
            return sum(v for d, v in self.by_day.items() if start <= d <= end and d in only_days)
        i, j = bisect_left(self.days, start), bisect_right(self.days, end)
        return self.prefix[j] - self.prefix[i]


@dataclass
class _Period:
    """
    A span to measure: inclusive ISO days, the cached preset bucket that covers
    exactly this span (None when none does), and whether it is a previous
    period (coverage rules apply) or `maximum` (read from the bucket).
    """
    start: str
    end: str
    preset: Optional[str] = None
    previous: bool = False
    maximum: bool = False
    current_preset: Optional[str] = None  # the window's own preset, for a previous period
    only_days: Optional[set] = None


def _metric_daily_fields(metric: str) -> set:
    entry = METRIC_REGISTRY.get(metric) or {}
    if entry.get("aggregation") == "recompute":
        return set().union(*(_metric_daily_fields(part) for part in entry["components"]))
    source = DAILY_SOURCES.get(metric)
    return {source["field"]} if source else set()


class _Engine:
    def __init__(self, groups: List[dict], today: date):
        catalog = build_catalog()
        self.today = today
        self.metrics = {m["key"]: m for m in catalog["metrics"]}
        self.dimensions = {d["key"] for d in catalog["dimensions"]}
        self.window_labels = {w["preset"]: w["label"] for w in catalog["windows"]}
        self.groups = sorted(groups, key=lambda g: (str(g.get("name") or "").lower(), str(g.get("id") or "")))
        self._series: Dict[tuple, _Series] = {}
        self._field_days: Dict[tuple, List[str]] = {}

    # daily rows ----------------------------------------------------------------

    def field_days(self, group: dict, field: str) -> List[str]:
        """Every day `group` has a row of `field` for, sorted."""
        key = (id(group), field)
        if key not in self._field_days:
            self._field_days[key] = sorted(day for day, _ in _daily_rows(group, field, None, None))
        return self._field_days[key]

    def series(self, group: dict, field: str, row_key: str) -> _Series:
        key = (id(group), field, row_key)
        found = self._series.get(key)
        if found is None:
            by_day: Dict[str, float] = {}
            for day, row in _daily_rows(group, field, None, None):
                if row_key in row:
                    by_day[day] = by_day.get(day, 0.0) + _finite(row.get(row_key))
            days = sorted(by_day)
            prefix = [0.0]
            for day in days:
                prefix.append(prefix[-1] + by_day[day])
            found = self._series[key] = _Series(days, prefix, by_day)
        return found

    def _covered(self, group: dict, field: str, row_key: str, start: str, end: str) -> bool:
        """The T14 guard: history reaches back to `start`, and rows in the span carry the key."""
        days = self.field_days(group, field)
        if not days or days[0] > start:
            return False
        if bisect_right(days, end) == bisect_left(days, start):
            return True  # nothing happened in the span
        s = self.series(group, field, row_key)
        return bisect_right(s.days, end) > bisect_left(s.days, start)

    # measuring -----------------------------------------------------------------

    def _daily(self, metric: str) -> bool:
        return self.metrics[metric].get("over") is True

    def value(self, metric: str, groups: List[dict], period: _Period) -> Optional[float]:
        """The metric over `groups` for `period`, or None when it cannot be known."""
        if not groups:
            return None
        daily = self._daily(metric) and not period.maximum
        entry = METRIC_REGISTRY[metric]
        if entry.get("aggregation") == "recompute":
            num_key, den_key = entry["components"]
            num = self._part(num_key, daily, groups, period)
            den = self._part(den_key, daily, groups, period)
            if num is None or den is None or den == 0:
                return None
            return _clean(entry.get("scale", 1) * num / den)
        return _clean(self._part(metric, daily, groups, period))

    def _part(self, key: str, daily: bool, groups: List[dict], period: _Period) -> Optional[float]:
        if daily:
            source = DAILY_SOURCES[key]
            total = 0.0
            for g in groups:
                part = self._daily_part(g, key, source, period)
                if part is None:
                    return None
                total += part
            return total
        # Cached preset buckets: no value for a custom range, a bucket or a day filter.
        if period.preset is None or period.only_days is not None:
            return None
        if not period.previous:
            return _finite(aggregate_metric(key, groups, period.preset))
        sources = _metric_sources(key)
        total, contributing = 0.0, False
        for g in groups:
            if not _covers(g, sources, period.current_preset):
                continue  # no data for the window either
            if not _covers(g, sources, period.preset):
                return None
            contributing = True
            total += _finite(get_metric_value(key, g, period.preset))
        return total if contributing else None

    def _daily_part(self, group: dict, key: str, source: dict, period: _Period) -> Optional[float]:
        field, row_key = source["field"], source["key"]
        if not period.previous:
            return self.series(group, field, row_key).total(period.start, period.end, period.only_days)
        if not self.field_days(group, field):
            return 0.0  # never had this series: contributes nothing to either period
        if self._covered(group, field, row_key, period.start, period.end):
            return self.series(group, field, row_key).total(period.start, period.end, None)
        if period.preset and _covers(group, _metric_sources(key), period.preset):
            return _finite(get_metric_value(key, group, period.preset))
        return None

    def earliest_day(self, groups: List[dict], metrics: List[str]) -> Optional[str]:
        fields = set().union(*(_metric_daily_fields(m) for m in metrics))
        firsts = [self.field_days(g, f)[0] for g in groups for f in fields if self.field_days(g, f)]
        return min(firsts) if firsts else None

    # one query -----------------------------------------------------------------

    def frame(self, raw: dict) -> dict:
        try:
            q = Query.model_validate(raw)
        except ValidationError as e:
            raise QueryError("QUERY_SHAPE", str(e.errors(include_url=False)))
        infos = []
        for ref in q.metrics:
            metric = self.metrics.get(ref.metric)
            if metric is None:
                raise QueryError("METRIC_UNKNOWN", f"Unknown metric {ref.metric!r}")
            if q.by is not None and q.by not in metric["dimensions"]:
                raise QueryError("DIMENSION_UNSUPPORTED", f"{ref.metric!r} cannot be split by {q.by!r}")
            if q.over is not None and metric.get("over") is not True:
                raise QueryError("GRAIN_UNSUPPORTED", f"{ref.metric!r} cannot be shown over time")
            infos.append((ref, metric))
        if q.by is not None and q.over is not None and (len(infos) != 1 or q.by != "client"):
            raise QueryError("QUERY_SHAPE", "A query with over and by takes exactly one metric, split by client")
        filters = q.filters or {}
        for dim in filters:
            if dim not in self.dimensions:
                raise QueryError("FILTER_DIMENSION_UNKNOWN", f"Unknown filter dimension {dim!r}")

        window = q.window_dict()
        try:
            start, end = window_range(window, self.today)
            prev_range = previous_window(window, self.today)
        except ValueError as e:
            raise QueryError("WINDOW_INVALID", str(e))
        preset = PRESET_ALIAS.get(window["preset"]) if "preset" in window else None
        maximum = preset == "maximum"

        groups = _filter_groups(self.groups, filters)
        only_days = set(filters["day"]) if filters.get("day") else None
        keys = [metric["key"] for _, metric in infos]
        if maximum:
            first = self.earliest_day(groups, keys)
            start = date.fromisoformat(first) if first else end
        current = _Period(start.isoformat(), end.isoformat(), preset=preset, maximum=maximum, only_days=only_days)

        previous: Optional[_Period] = None
        if q.compare == "previous" and prev_range is not None:
            p_start, p_end = prev_range
            bucket = previous_preset(preset) if preset else None
            if bucket and window_range({"preset": bucket}, self.today) != prev_range:
                bucket = None  # the cached bucket spans a different period (this_month vs all of last_month)
            previous = _Period(p_start.isoformat(), p_end.isoformat(), preset=bucket, previous=True,
                               current_preset=preset)

        def measure(gs: List[dict], period: Optional[_Period], key: str) -> Optional[float]:
            if period is None:
                return None
            if period.previous and only_days is not None:
                return None  # the listed days are inside the window, not the previous period
            return self.value(key, gs, period)

        def values(gs, period) -> Dict[str, Optional[float]]:
            return {k: measure(gs, period, k) for k in keys}

        def with_previous(entry: dict, gs, prev_period) -> dict:
            if previous is not None:
                entry["previous"] = values(gs, prev_period)
            return entry

        columns = [_column(metric, ref.label or metric["label"]) for ref, metric in infos]
        rows: List[dict] = []
        totals = with_previous({"values": values(groups, current)}, groups, previous)

        if q.over is None and q.by == "client":
            rows = [
                with_previous({"key": str(g.get("id") or ""), "label": _name(g), "values": values([g], current)},
                              [g], previous)
                for g in groups
            ]
            rows = _sort_limit(rows, q)
        elif q.over is not None or q.by == "day":
            grain = q.over or "day"
            buckets = _buckets(start, end, grain)
            before = _buckets(*prev_range, grain) if previous is not None else []

            def span(i: int, source: list, prev: bool) -> Optional[_Period]:
                if i >= len(source):
                    return None
                _, b_start, b_end = source[i]
                return _Period(b_start, b_end, previous=prev, only_days=None if prev else only_days)

            if q.by == "client" and q.over is not None:
                key = keys[0]
                ranked = sorted(groups, key=lambda g: _desc_nulls_last(measure([g], current, key)))
                top = ranked[: min(q.limit or DEFAULT_TOP_N, MAX_TOP_N)]
                ids = [str(g.get("id") or "") for g in top]
                columns = [_column(infos[0][1], _name(g), key=gid) for g, gid in zip(top, ids)]

                def across(period):
                    return {gid: measure([g], period, key) for g, gid in zip(top, ids)}

                totals = {"values": across(current)}
                if previous is not None:
                    totals["previous"] = across(previous)
                for i, (bkey, _, _) in enumerate(buckets):
                    row = {"key": bkey, "label": bkey, "values": across(span(i, buckets, False))}
                    if previous is not None:
                        row["previous"] = across(span(i, before, True))
                    rows.append(row)
            else:
                for i, (bkey, _, _) in enumerate(buckets):
                    row = {"key": bkey, "label": bkey, "values": values(groups, span(i, buckets, False))}
                    rows.append(with_previous(row, groups, span(i, before, True)))
                if q.by == "day":
                    rows = _sort_limit(rows, q)

        frame = {
            "columns": columns,
            "rows": rows,
            "totals": totals,
            "previousAvailable": not maximum,
            "window": {"label": self._window_label(window), "from": start.isoformat(), "to": end.isoformat()},
        }
        if q.spark:
            days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
            frame["spark"] = {
                k: [{"date": d, "value": measure(groups, _Period(d, d, only_days=only_days), k)} for d in days]
                for k in keys if self._daily(k)
            }
        return frame

    def _window_label(self, window: dict) -> str:
        if "preset" not in window:
            return f"{window['from']} – {window['to']}"
        preset = window["preset"]
        if preset in self.window_labels:
            return self.window_labels[preset]
        return " ".join(w[:1].upper() + w[1:] for w in preset.replace("_", " ").split())


# --- helpers ----------------------------------------------------------------------

def _clean(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _name(group: dict) -> str:
    return str(group.get("name") or group.get("id") or "")


def _column(metric: dict, label: str, key: Optional[str] = None) -> dict:
    column = {"key": key or metric["key"], "label": label, "format": metric["format"]}
    if metric.get("polarity"):
        column["polarity"] = metric["polarity"]
    return column


def _desc_nulls_last(value: Optional[float]):
    return (value is None, -(value or 0.0))


def _sort_limit(rows: List[dict], q: Query) -> List[dict]:
    """`sort` by one metric with nulls last either way (stable), then the first `limit` rows."""
    if q.sort:
        key = q.sort.metric
        known = [r for r in rows if r["values"].get(key) is not None]
        unknown = [r for r in rows if r["values"].get(key) is None]
        known.sort(key=lambda r: r["values"][key], reverse=q.sort.dir == "desc")
        rows = known + unknown
    if q.limit is not None:
        rows = rows[: q.limit]
    return rows


def _bucket_start(day: date, grain: str) -> date:
    if grain == "week":
        return day - timedelta(days=day.weekday())  # ISO week, Monday first
    if grain == "month":
        return day.replace(day=1)
    return day


def _next_bucket(start: date, grain: str) -> date:
    if grain == "week":
        return start + timedelta(days=7)
    if grain == "month":
        return (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start + timedelta(days=1)


def _buckets(start: date, end: date, grain: str) -> List[tuple]:
    """(key, first day, last day) per bucket of `grain` in [start, end]; the key is the bucket's own start."""
    out, day = [], start
    while day <= end:
        b_start = _bucket_start(day, grain)
        b_end = min(_next_bucket(b_start, grain) - timedelta(days=1), end)
        out.append((b_start.isoformat(), day.isoformat(), b_end.isoformat()))
        day = b_end + timedelta(days=1)
    return out


def _daily_fields_for(queries: List) -> set:
    """The daily series the request's metrics read (metrics without daily history read none)."""
    fields: set = set()
    for q in queries:
        metrics = q.get("metrics") if isinstance(q, dict) else None
        for ref in metrics if isinstance(metrics, list) else []:
            metric = ref.get("metric") if isinstance(ref, dict) else None
            if isinstance(metric, str):
                fields |= _metric_daily_fields(metric)
    return fields


async def run_queries(db, user_id: str, queries: List[dict], today: Optional[date] = None) -> List[Optional[dict]]:
    """One Frame dict per query, in order; None for a query that fails (logged). The caller caps the count."""
    if not queries:
        return []
    groups = await load_groups(db, user_id, daily_fields=_daily_fields_for(queries))
    engine = _Engine(groups, today or date.today())
    frames: List[Optional[dict]] = []
    for i, query in enumerate(queries):
        try:
            frames.append(engine.frame(query))
        except QueryError as e:
            logger.warning(f"Skipping dashboard query {i} for {user_id}: {e}")
            frames.append(None)
        except Exception:
            logger.exception(f"Failed dashboard query {i} for {user_id}: {query!r}")
            frames.append(None)
    return frames
