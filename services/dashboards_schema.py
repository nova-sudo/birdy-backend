"""
services/dashboards_schema.py
-----------------------------
Validates a dashkit dashboard document against the package's JSON Schema.

`dashkit_schema.json` is a verbatim copy of dashkit's `dist/schema.json`,
refreshed by `scripts/sync_dashkit_schema.sh` in the dashkit repo — never edit
it by hand. The validator is built once at import time; a missing or
malformed schema file should fail the app at startup, not on a user's save.

The schema is dashkit's document v2 (a flat `widgets` list on a 12-column
grid), so saving a v1 doc (sections with spans) is rejected; stored v1 docs are
still served as they are and the editor migrates them on load.
"""

import json
from pathlib import Path

from jsonschema import Draft202012Validator

_SCHEMA_PATH = Path(__file__).with_name("dashkit_schema.json")

_VALIDATOR = Draft202012Validator(json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))

# dashkit's LayoutSchema also refines `x + w <= GRID_COLS`, which its JSON Schema
# cannot carry; a doc past the right edge would fail dashkit's migrate() on load.
_GRID_COLS = 12


def _pointer(path) -> str:
    """JSON-pointer-ish location of an error: `/widgets/0/layout`, or `/` for the root."""
    return "/" + "/".join(str(p) for p in path)


def validate_doc(doc: dict) -> list[str]:
    """Every schema violation in `doc` as `"<json path>: <message>"`; empty when valid."""
    errors = sorted(_VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path)))
    return [f"{_pointer(e.absolute_path)}: {e.message}" for e in errors] + _right_edge_errors(doc)


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


def schema_has_meta_updated_at() -> bool:
    """Whether the schema declares `meta.updatedAt`, i.e. whether the server should stamp it."""
    meta = _VALIDATOR.schema.get("properties", {}).get("meta", {})
    return "updatedAt" in meta.get("properties", {})
