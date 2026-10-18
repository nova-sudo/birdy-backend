"""
tests/test_dashkit_tools_json.py
--------------------------------
Parity guard between the metric catalog (services/dashboards_catalog.build_catalog)
and the two generated copies of it:

  - ai/tools/dashkit_tools.json: the AI tool schemas dashkit generates from the
    catalog snapshot; every binding's metric / by / window preset enum must list
    exactly the catalog's metrics, dimensions and windows.
  - scripts/birdy-catalog.json in the dashkit repo: the snapshot those schemas
    are generated from; it must equal build_catalog().

When either fails, re-run dashkit's scripts/sync_dashkit_schema.sh and commit
the regenerated files in both repos.
"""

import json
import os
from pathlib import Path

import pytest

from services.dashboards_catalog import build_catalog

ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = ROOT / "ai" / "tools" / "dashkit_tools.json"
DASHKIT_CATALOG = Path(
    os.environ.get("DASHKIT_DIR", str(ROOT.parent.parent / "dashkit"))
) / "scripts" / "birdy-catalog.json"


def _binding_schemas(node):
    """
    Every catalog-constrained binding schema in the tools JSON: an object with
    `metric` and `window` properties whose metric carries an enum. (addSection's
    nested widgets use dashkit's generic binding schema without catalog enums.)
    """
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "enum" in (props.get("metric") or {}) and "window" in props:
            yield node
        for value in node.values():
            yield from _binding_schemas(value)
    elif isinstance(node, list):
        for value in node:
            yield from _binding_schemas(value)


def _preset_enums(window_schema):
    for option in window_schema.get("anyOf", [window_schema]):
        preset = (option.get("properties") or {}).get("preset")
        if preset and "enum" in preset:
            yield preset["enum"]


def _same(enum, expected):
    return len(enum) == len(set(enum)) and set(enum) == set(expected)


@pytest.fixture(scope="module")
def catalog():
    return build_catalog()


@pytest.fixture(scope="module")
def bindings():
    found = list(_binding_schemas(json.loads(TOOLS_JSON.read_text())))
    # addWidget and setBinding (dashboard_apply) plus dashboard_propose_widget.
    assert len(found) >= 3, "catalog-constrained binding schemas missing from ai/tools/dashkit_tools.json"
    return found


def test_metric_enums_equal_the_catalog_metric_keys(bindings, catalog):
    keys = [m["key"] for m in catalog["metrics"]]
    for schema in bindings:
        assert _same(schema["properties"]["metric"]["enum"], keys)


def test_by_enums_equal_the_catalog_dimensions(bindings, catalog):
    keys = [d["key"] for d in catalog["dimensions"]]
    for schema in bindings:
        assert _same(schema["properties"]["by"]["enum"], keys)


def test_window_preset_enums_equal_the_catalog_windows(bindings, catalog):
    presets = [w["preset"] for w in catalog["windows"]]
    for schema in bindings:
        enums = list(_preset_enums(schema["properties"]["window"]))
        assert enums, "binding window schema has no preset enum"
        for enum in enums:
            assert _same(enum, presets)


def test_dashkit_catalog_snapshot_equals_build_catalog(catalog):
    if not DASHKIT_CATALOG.exists():
        pytest.skip(f"dashkit catalog snapshot not found at {DASHKIT_CATALOG} (set DASHKIT_DIR to the dashkit repo)")
    assert json.loads(DASHKIT_CATALOG.read_text()) == catalog
