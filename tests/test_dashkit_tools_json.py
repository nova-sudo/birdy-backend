"""
tests/test_dashkit_tools_json.py
--------------------------------
Parity guard between the metric catalog (services/dashboards_catalog.build_catalog)
and the two generated copies of it:

  - ai/tools/dashkit_tools.json: the AI tool schemas dashkit generates from the
    catalog snapshot; every query's metric / sort metric / by / filter dimension /
    window preset enum must list exactly the catalog's metrics, dimensions and
    windows. Its per-type `queryRules` must equal the Python QUERY_RULES that
    save validation and the AI tools enforce.
  - scripts/birdy-catalog.json in the dashkit repo: the snapshot those schemas
    are generated from; it must equal build_catalog().
  - tests/fixtures/dashkit_prompt_fragment.md: dashkit's systemPromptFragment
    for the same widgets and catalog (written by dashkit's export-tools); the
    Python port ai.prompts.dashboards.prompt_fragment() must equal it byte for byte.

When either fails, re-run dashkit's scripts/sync_dashkit_schema.sh and commit
the regenerated files in both repos.
"""

import json
import os
from pathlib import Path

import pytest

from ai.prompts.dashboards import prompt_fragment
from services.dashboards_catalog import build_catalog
from services.dashboards_schema import QUERY_RULES, QUERYLESS_TYPES

ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = ROOT / "ai" / "tools" / "dashkit_tools.json"
PROMPT_FRAGMENT = ROOT / "tests" / "fixtures" / "dashkit_prompt_fragment.md"
DASHKIT_CATALOG = Path(
    os.environ.get("DASHKIT_DIR", str(ROOT.parent.parent / "dashkit"))
) / "scripts" / "birdy-catalog.json"


def _query_schemas(node):
    """
    Every catalog-constrained query schema in the tools JSON: an object with
    `metrics` and `window` properties whose metric carries an enum.
    """
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "window" in props and \
                "enum" in (((props.get("metrics") or {}).get("items") or {}).get("properties") or {}).get("metric", {}):
            yield node
        for value in node.values():
            yield from _query_schemas(value)
    elif isinstance(node, list):
        for value in node:
            yield from _query_schemas(value)


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
def queries():
    found = list(_query_schemas(json.loads(TOOLS_JSON.read_text())))
    # addWidget and setQuery (dashboard_apply) plus dashboard_propose_widget.
    assert len(found) >= 3, "catalog-constrained query schemas missing from ai/tools/dashkit_tools.json"
    return found


def test_metric_enums_equal_the_catalog_metric_keys(queries, catalog):
    keys = [m["key"] for m in catalog["metrics"]]
    for schema in queries:
        assert _same(schema["properties"]["metrics"]["items"]["properties"]["metric"]["enum"], keys)
        assert _same(schema["properties"]["sort"]["properties"]["metric"]["enum"], keys)


def test_by_and_filter_enums_equal_the_catalog_dimensions(queries, catalog):
    keys = [d["key"] for d in catalog["dimensions"]]
    for schema in queries:
        assert _same(schema["properties"]["by"]["enum"], keys)
        assert _same(schema["properties"]["filters"]["propertyNames"]["enum"], keys)


def test_window_preset_enums_equal_the_catalog_windows(queries, catalog):
    presets = [w["preset"] for w in catalog["windows"]]
    for schema in queries:
        enums = list(_preset_enums(schema["properties"]["window"]))
        assert enums, "query window schema has no preset enum"
        for enum in enums:
            assert _same(enum, presets)


def test_query_rules_equal_dashkit_query_rules():
    tools = {t["name"]: t for t in json.loads(TOOLS_JSON.read_text())}
    exported = {w["type"]: w["queryRules"]
                for w in tools["dashboard_propose_widget"]["x-dashkit-widgets"] if "queryRules" in w}
    assert QUERY_RULES == exported
    queryless = {w["type"] for w in tools["dashboard_propose_widget"]["x-dashkit-widgets"] if "queryRules" not in w}
    assert set(QUERYLESS_TYPES) == queryless


def test_dashkit_catalog_snapshot_equals_build_catalog(catalog):
    if not DASHKIT_CATALOG.exists():
        pytest.skip(f"dashkit catalog snapshot not found at {DASHKIT_CATALOG} (set DASHKIT_DIR to the dashkit repo)")
    assert json.loads(DASHKIT_CATALOG.read_text()) == catalog


def test_tools_are_dashkit_v3_query_ops():
    tools = {t["name"]: t for t in json.loads(TOOLS_JSON.read_text())}
    ops = [m["properties"]["op"]["const"]
           for m in tools["dashboard_apply"]["parameters"]["properties"]["ops"]["items"]["anyOf"]]
    assert {"setLayout", "setQuery", "clearQuery"} <= set(ops)
    assert not {"setBinding", "addSection", "removeSection", "moveSection", "updateSection",
                "replaceDashboard"} & set(ops)
    propose = tools["dashboard_propose_widget"]
    assert {"after", "size", "query"} <= set(propose["parameters"]["properties"])
    assert not {"sectionId", "span", "bindings"} & set(propose["parameters"]["properties"])
    for widget in propose["x-dashkit-widgets"]:
        assert {"defaultSize", "minSize", "maxSize"} <= set(widget), widget["type"]
        assert "slots" not in widget, widget["type"]
    assert "heading" in [w["type"] for w in propose["x-dashkit-widgets"]]


def test_prompt_fragment_matches_dashkit():
    golden = PROMPT_FRAGMENT.read_text(encoding="utf-8")
    assert prompt_fragment() == golden
