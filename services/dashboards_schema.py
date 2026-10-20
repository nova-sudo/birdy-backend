"""
services/dashboards_schema.py
-----------------------------
Validates a dashkit dashboard document against the package's JSON Schema,
plus the rules dashkit enforces in code that a JSON Schema cannot carry.

`dashkit_schema.json` is a verbatim copy of dashkit's `dist/schema.json`,
refreshed by `scripts/sync_dashkit_schema.sh` in the dashkit repo — never edit
it by hand. The validators are built once at import time; a missing or
malformed schema file should fail the app at startup, not on a user's save.

The schema is dashkit's document v3: a flat `widgets` list on a 12-column grid,
each data widget holding one `bindings.query`. During the dashkit 0.4.0 rollout
a v2 doc (bindings per slot) is still accepted, checked against
`dashkit_schema_v2.json`, the v2 schema frozen from dashkit 0.3.x; drop it once
every editor saves v3. Saving a v1 doc (sections with spans) is rejected;
stored v1 and v2 docs are still served as they are and the editor migrates
them on load.

`QUERY_RULES` mirrors each widget type's `QueryRules` (dashkit's per-type query
table, src/core/registry.ts) and `check_query_shape` its `checkQueryShape`;
tests/test_dashkit_tools_json.py asserts the dict equals the `queryRules`
dashkit exports in ai/tools/dashkit_tools.json. The per-type `check` functions
(one value-axis format, donut additivity) need the catalog and widget data,
so they are not part of save validation.
"""

import json
from pathlib import Path
from typing import Optional

from jsonschema import Draft202012Validator

_SCHEMA_PATH = Path(__file__).with_name("dashkit_schema.json")
_V2_SCHEMA_PATH = Path(__file__).with_name("dashkit_schema_v2.json")

_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))
_V2_VALIDATOR = Draft202012Validator(json.loads(_V2_SCHEMA_PATH.read_text(encoding="utf-8")))

# dashkit's LayoutSchema also refines `x + w <= GRID_COLS`, which its JSON Schema
# cannot carry; a doc past the right edge would fail dashkit's migrate() on load.
_GRID_COLS = 12

QUERY_SHAPE = "QUERY_SHAPE"

# Most series a query with both `over` and `by` may draw (dashkit's MAX_OVER_BY_LIMIT).
MAX_OVER_BY_LIMIT = 8

# What each widget type's `query` may hold. Types missing here take no query.
QUERY_RULES: dict[str, dict] = {
    "kpi": {"metrics": {"min": 1, "max": 1}, "by": "forbidden", "over": "forbidden", "spark": True,
            "defaults": {}},
    "kpi-strip": {"metrics": {"min": 2, "max": 4}, "by": "forbidden", "over": "forbidden", "defaults": {}},
    "trend": {"metrics": {"min": 1, "max": 4}, "by": "optional", "over": "required", "maxMetricsWithBy": 1,
              "defaults": {"over": "day"}},
    "chart": {"metrics": {"min": 1, "max": 4}, "by": "required", "over": "forbidden", "defaults": {"by": "client"}},
    "funnel": {"metrics": {"min": 1, "max": 8}, "by": "forbidden", "over": "forbidden", "defaults": {}},
    "leaderboard": {"metrics": {"min": 1, "max": 3}, "by": "required", "over": "forbidden",
                    "defaults": {"by": "client"}},
    "table": {"metrics": {"min": 1, "max": 20}, "by": "optional", "over": "optional", "exactlyOneOfByOver": True,
              "defaults": {"by": "client"}},
}

# Widget types dashkit ships, query or not; a query on one of these that has no rules breaks QUERY_SHAPE.
QUERYLESS_TYPES = ("heading", "text", "briefing")


def _pointer(path) -> str:
    """JSON-pointer-ish location of an error: `/widgets/0/layout`, or `/` for the root."""
    return "/" + "/".join(str(p) for p in path)


def _count_label(lo: int, hi: int, noun: str) -> str:
    return f"exactly {lo} {noun}{'' if lo == 1 else 's'}" if lo == hi else f"{lo}..{hi} {noun}s"


def check_query_shape(rules: dict, query: dict) -> Optional[str]:
    """dashkit's checkQueryShape without the per-type `check`: the first rule `query` breaks, or None."""
    n = len(query.get("metrics") or [])
    lo, hi = rules["metrics"]["min"], rules["metrics"]["max"]
    if n < lo or n > hi:
        return f"Needs {_count_label(lo, hi, 'metric')}, got {n}"
    with_by = rules.get("maxMetricsWithBy")
    if query.get("by") is not None and with_by is not None and n > with_by:
        return f"Needs {_count_label(with_by, with_by, 'metric')} when split by a dimension, got {n}"
    for key in ("by", "over"):
        if rules[key] == "required" and query.get(key) is None:
            return f'Needs "{key}" to be set'
        if rules[key] == "forbidden" and query.get(key) is not None:
            return f'Does not take "{key}"'
    if rules.get("exactlyOneOfByOver") and (query.get("by") is None) == (query.get("over") is None):
        return 'Needs exactly one of "by" or "over"'
    limit = query.get("limit")
    if query.get("by") is not None and query.get("over") is not None and limit is not None \
            and limit > MAX_OVER_BY_LIMIT:
        return f'A query with "over" and "by" shows at most {MAX_OVER_BY_LIMIT} series, got limit {limit}'
    return None


def query_type_problem(widget_type, query: dict) -> Optional[str]:
    """QUERY_SHAPE message for `query` on a widget of `widget_type`; None when it fits or the type is not
    dashkit's (a host type's rules are unknown here)."""
    rules = QUERY_RULES.get(widget_type)
    if rules is not None:
        return check_query_shape(rules, query)
    if widget_type in QUERYLESS_TYPES:
        return f'Widget type "{widget_type}" takes no query'
    return None


def query_refinement_problems(query: dict) -> list[tuple[list, str]]:
    """(path inside the query, message) for dashkit QuerySchema's refinements: unique metrics, and a sort
    metric that is one of them. Assumes the query passed the JSON Schema."""
    problems, seen = [], set()
    for i, ref in enumerate(query["metrics"]):
        if ref["metric"] in seen:
            problems.append((["metrics", i, "metric"], f'Duplicate metric "{ref["metric"]}"'))
        seen.add(ref["metric"])
    sort = query.get("sort")
    if sort is not None and sort["metric"] not in seen:
        problems.append((["sort", "metric"], f'Sort metric "{sort["metric"]}" is not in metrics'))
    return problems


def _is_v2(doc) -> bool:
    return isinstance(doc, dict) and doc.get("version") == 2


def validate_doc(doc: dict) -> list[str]:
    """Every violation in `doc` as `"<json path>: <message>"`; empty when valid. A v3 doc's query problems read
    `"<path>: QUERY_SHAPE: <rule>"`; a v2 doc is checked against the frozen v2 schema."""
    validator = _V2_VALIDATOR if _is_v2(doc) else _VALIDATOR
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    out = [f"{_pointer(e.absolute_path)}: {e.message}" for e in errors] + _right_edge_errors(doc)
    if not errors and validator is _VALIDATOR:
        out += _query_errors(doc)
    return out


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _right_edge_errors(doc) -> list[str]:
    widgets = doc.get("widgets") if isinstance(doc, dict) else None
    if not isinstance(widgets, list):
        return []
    errors = []
    for i, widget in enumerate(widgets):
        layout = widget.get("layout") if isinstance(widget, dict) else None
        if (isinstance(layout, dict) and _is_int(layout.get("x")) and _is_int(layout.get("w"))
                and layout["x"] + layout["w"] > _GRID_COLS):
            errors.append(f"/widgets/{i}/layout/w: x + w must be at most {_GRID_COLS}")
    return errors


def _query_errors(doc: dict) -> list[str]:
    """Query checks on a schema-valid v3 doc: QuerySchema's refinements, then each type's QUERY_RULES."""
    errors = []
    for i, widget in enumerate(doc["widgets"]):
        query = (widget.get("bindings") or {}).get("query")
        if query is None:
            continue
        at = ["widgets", i, "bindings", "query"]
        refinements = query_refinement_problems(query)
        errors += [f"{_pointer(at + path)}: {message}" for path, message in refinements]
        problem = None if refinements else query_type_problem(widget["type"], query)
        if problem:
            errors.append(f"{_pointer(at)}: {QUERY_SHAPE}: {problem}")
    return errors


def schema_has_meta_updated_at() -> bool:
    """Whether the schema declares `meta.updatedAt`, i.e. whether the server should stamp it."""
    meta = _VALIDATOR.schema.get("properties", {}).get("meta", {})
    return "updatedAt" in meta.get("properties", {})
