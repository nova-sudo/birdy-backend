"""
tests/test_dashboard_tools.py
-----------------------------
The "My dashboards" AI tools: executors return dashkit ops (never touching the
stored doc), validate their arguments against the generated tool schemas, and
run_chat hands every successful op batch back to the editor as dashboard_ops.
"""

import json

import pytest

from ai.config import MAX_TOOL_ITERATIONS
from ai.orchestrator import run_chat
from ai.prompts.dashboards import prompt_fragment, system_prompt
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
from ai.tools.dashboard_tools import (
    DASHBOARD_TOOL_NAMES,
    catalog_search,
    dashboard_apply,
    dashboard_describe,
    dashboard_propose_widget,
    register_dashboard_tools,
    summarize_doc,
)
from ai.tools.registry import ToolRegistry
from core.models import ChatRequest, ChatResponse
from services.dashboards_catalog import build_catalog

USER = "alice@example.com"


class ScriptedProvider(BaseLLMProvider):
    """Plays back a fixed sequence of responses, one per chat_completion call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "stub-model"
        self.calls = []

    async def chat_completion(self, messages, tools=None, tool_choice="auto",
                              temperature=0.3, max_tokens=2048):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self._responses.pop(0)


def _tool_call_response(name, arguments, call_id="tc-1"):
    return ProviderResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=json.dumps(arguments))],
        finish_reason="tool_calls",
    )


_META = {"createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z", "lastAuthor": "user"}


def _at(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def _doc():
    """A dashkit v2 doc: a heading, then a KPI and a hidden, locked trend side by side under it."""
    return {
        "id": "d1",
        "version": 2,
        "title": "Growth",
        "widgets": [
            {"id": "s1", "type": "heading", "version": 1, "title": "Overview", "layout": _at(0, 0, 12, 1), "data": {}},
            {
                "id": "w1", "type": "kpi", "version": 1, "title": "Spend", "layout": _at(0, 1, 3, 3), "data": {},
                "bindings": {"value": {"metric": "meta_spend", "window": {"preset": "last_30d"}}},
            },
            {
                "id": "w2", "type": "trend", "version": 1, "layout": _at(3, 1, 6, 6), "data": {},
                "bindings": {"series": {"metric": "spend", "by": "day",
                                        "window": {"from": "2026-09-01", "to": "2026-09-30"}}},
                "hidden": True, "locked": True,
            },
        ],
        "meta": dict(_META),
    }


def _v1_doc():
    """The same dashboard as stored before dashkit 0.3.0: sections with spans."""
    return {
        "id": "d1",
        "version": 1,
        "title": "Growth",
        "sections": [
            {
                "id": "s1",
                "title": "Overview",
                "widgets": [
                    {
                        "id": "w1", "type": "kpi", "version": 1, "title": "Spend",
                        "span": {"cols": 3, "rows": 1}, "data": {},
                        "bindings": {"value": {"metric": "meta_spend", "window": {"preset": "last_30d"}}},
                    },
                    {
                        "id": "w2", "type": "trend", "version": 1,
                        "span": {"cols": 6, "rows": 2}, "data": {},
                        "bindings": {"series": {"metric": "spend", "by": "day",
                                                "window": {"from": "2026-09-01", "to": "2026-09-30"}}},
                        "hidden": True, "locked": True,
                    },
                ],
            },
            {"id": "s2", "title": "", "widgets": []},
        ],
        "meta": dict(_META),
    }


async def _store(db, doc=None, user=USER):
    doc = doc or _doc()
    await db["dashboards"].insert_one({"id": doc["id"], "user_id": user, "doc": doc})


KPI_BINDING = {"metric": "meta_spend", "window": {"preset": "last_30d"}, "compare": "previous"}


def _summary():
    """What the editor sends: summarize(doc) of the live doc."""
    return summarize_doc(_doc())


# ── summarize ──────────────────────────────────────────────────────────────


def _w(id, type, layout, **extra):
    return {"id": id, "type": type, "version": 1, "layout": layout, "data": {}, **extra}


def _dashkit_doc(widgets):
    """dashkit's own test fixture shape (src/core/__tests__/utils.test.ts)."""
    return {"id": "d1", "version": 2, "title": "Main", "widgets": widgets,
            "meta": {"createdAt": "a", "updatedAt": "a", "lastAuthor": "user"}}


def test_summarize_doc_v2_matches_dashkit():
    # dashkit utils.test.ts "summary lists widgets in y,x order with @x,y", verbatim.
    doc = _dashkit_doc([
        _w("w2", "kpi", _at(0, 1, 3, 3), title="Spend", bindings={
            "value": {"metric": "spend", "window": {"preset": "last_7d"}},
            "previous": {"metric": "leads", "by": "client", "window": {"from": "2026-01-01", "to": "2026-01-31"}},
        }),
        _w("w3", "note", _at(6, 1, 6, 3), hidden=True, locked=True),
        _w("h1", "heading", _at(0, 0, 12, 1), title="Overview", data={"level": 1}),
    ])
    assert summarize_doc(doc) == "\n".join([
        "# Main (3 widgets)",
        '- [h1] heading "Overview" 12×1 @0,0 (h1)',
        '- [w2] kpi "Spend" 3×3 @0,1  value→spend last_7d; previous→leads by client 2026-01-01..2026-01-31',
        '- [w3] note "" 6×3 @6,1 hidden locked',
    ])


def test_summarize_doc_v2_heading_levels_match_dashkit():
    def heading(id, y, data):
        return _w(id, "heading", _at(0, y, 12, 1), title=id, data=data)

    doc = _dashkit_doc([heading("a", 0, {"level": 1}), heading("b", 1, {"level": 2}),
                        heading("c", 2, {"level": 3}), heading("d", 3, {})])
    assert summarize_doc(doc).split("\n")[1:] == [
        '- [a] heading "a" 12×1 @0,0 (h1)',
        '- [b] heading "b" 12×1 @0,1',
        '- [c] heading "c" 12×1 @0,2 (h3)',
        '- [d] heading "d" 12×1 @0,3',
    ]
    # A non-numeric level reads as 2, as in dashkit (and so does a boolean).
    for level in ("1", True):
        bad = heading("h", 0, {"level": level})
        bad["title"] = "H"
        assert summarize_doc(_dashkit_doc([bad])).split("\n")[1] == '- [h] heading "H" 12×1 @0,0'
    # Only headings carry a level marker.
    assert summarize_doc(_dashkit_doc([_w("k", "kpi", _at(0, 0, 3, 3), data={"level": 1})])).split("\n")[1] == \
        '- [k] kpi "" 3×3 @0,0'


def test_summarize_doc_v2_same_row_x_order_and_empty():
    doc = _dashkit_doc([_w("r", "note", _at(6, 0, 6, 3)), _w("l", "note", _at(0, 0, 6, 3))])
    assert summarize_doc(doc).split("\n")[1:] == ['- [l] note "" 6×3 @0,0', '- [r] note "" 6×3 @6,0']
    assert summarize_doc(_dashkit_doc([])) == "# Main (0 widgets)"


def test_summarize_doc_v1_is_flattened():
    # Expected strings are dashkit's summarize(migrate(doc)) for the same v1 docs.
    assert summarize_doc(_v1_doc()) == "\n".join([
        "# Growth (3 widgets)",
        '- [s1] heading "Overview" 12×1 @0,0',
        '- [w1] kpi "Spend" 3×2 @0,1  value→meta_spend last_30d',
        '- [w2] trend "" 6×4 @3,1 hidden locked  series→spend by day 2026-09-01..2026-09-30',
    ])


def test_summarize_doc_v1_flattening_matches_dashkit_migrate():
    """Heading ids, levels and descriptions, clamped sizes, shelf wrapping and compaction, as dashkit's migrate."""
    def w(id, type, cols, rows, title=None, **extra):
        widget = {"id": id, "type": type, "version": 1, "span": {"cols": cols, "rows": rows}, "data": {}, **extra}
        if title is not None:
            widget["title"] = title
        return widget

    doc = {
        "id": "d2", "version": 1, "title": "Mixed", "meta": dict(_META),
        "sections": [
            {"id": "top", "title": "Top", "level": 1, "description": "Headline numbers", "widgets": [
                w("k1", "kpi", 3, 1, "A"), w("t1", "trend", 6, 2, "B"), w("k2", "kpi", 3, 1, "C"),
                w("k3", "kpi", 3, 1, "D"), w("top", "text", 6, 1, "Note", data={"markdown": "x"})]},
            {"id": "untitled", "widgets": [w("tb", "table", 12, 4), w("zz", "mystery", 5, 3)]},
            {"id": "small", "title": "Small", "level": 3, "widgets": [w("d1w", "donut", 2, 1)]},
            {"id": "mid", "title": "Mid", "level": 2, "widgets": []},
        ],
    }
    assert summarize_doc(doc) == "\n".join([
        "# Mixed (11 widgets)",
        '- [top-heading] heading "Top" 12×2 @0,0 (h1)',
        '- [k1] kpi "A" 3×2 @0,2',
        '- [t1] trend "B" 6×4 @3,2',
        '- [k2] kpi "C" 3×2 @9,2',
        '- [k3] kpi "D" 3×2 @0,4',
        '- [top] text "Note" 6×2 @3,6',
        '- [tb] table "" 12×8 @0,8',
        '- [zz] mystery "" 5×6 @0,16',
        '- [small] heading "Small" 12×1 @0,22 (h3)',
        '- [d1w] donut "" 3×5 @0,23',
        '- [mid] heading "Mid" 12×1 @0,28',
    ])


# ── propose ────────────────────────────────────────────────────────────────


async def test_propose_widget_returns_one_add_widget_op_after_the_given_widget(mock_db):
    result = await dashboard_propose_widget(
        mock_db, USER, summary=_summary(), after="s1", type="kpi", title="Spend",
        bindings={"value": KPI_BINDING},
    )
    assert result["ok"] is True
    (add,) = result["ops"]
    widget_id = add["widget"]["id"]
    assert len(widget_id) == 12
    assert add == {
        "op": "addWidget",
        "widget": {"id": widget_id, "type": "kpi", "title": "Spend", "bindings": {"value": KPI_BINDING}},
        "after": "s1",
    }
    assert result["widgetId"] == widget_id
    assert result["note"] == "Applied in the user's editor."


async def test_propose_widget_without_after_goes_to_the_bottom_even_on_an_empty_dashboard(mock_db):
    for summary in (_summary(), "# Empty (0 widgets)", None):
        result = await dashboard_propose_widget(mock_db, USER, summary=summary, type="kpi",
                                                bindings={"value": KPI_BINDING})
        assert result["ok"] is True
        assert "after" not in result["ops"][0]


async def test_propose_widget_passes_size_and_data(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="heading", title="Costs",
                                            size={"w": 12, "h": 2},
                                            data={"level": 1, "description": "What we pay"})
    assert result["ok"] is True
    assert result["ops"][0]["widget"] == {"id": result["widgetId"], "type": "heading", "title": "Costs",
                                          "size": {"w": 12, "h": 2},
                                          "data": {"level": 1, "description": "What we pay"}}


@pytest.mark.parametrize("size, path", [({"w": 1, "h": 3}, "/size/w"), ({"w": 3, "h": 5}, "/size/h")])
async def test_propose_widget_size_outside_the_type_range_is_rejected(mock_db, size, path):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi", size=size,
                                            bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"]["code"] == "SIZE_OUT_OF_RANGE"
    assert result["error"]["path"] == path


async def test_propose_widget_after_an_unknown_widget_is_rejected(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), after="nope", type="kpi",
                                            bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"] == {"code": "WIDGET_NOT_FOUND", "path": "/after",
                               "message": result["error"]["message"]}


async def test_propose_widget_after_ids_fall_back_to_the_stored_doc(mock_db):
    await _store(mock_db)
    result = await dashboard_propose_widget(mock_db, USER, dashboard_id="d1", after="w2", type="kpi",
                                            bindings={"value": KPI_BINDING})
    assert result["ok"] is True
    await _store(mock_db, {**_v1_doc(), "id": "old"})
    from_v1 = await dashboard_propose_widget(mock_db, USER, dashboard_id="old", after="s1", type="kpi",
                                             bindings={"value": KPI_BINDING})
    assert from_v1["ok"] is True


async def test_propose_widget_rejects_section_id_and_span(mock_db):
    for extra in ({"sectionId": "s1"}, {"span": {"cols": 3, "rows": 1}}):
        result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                                bindings={"value": KPI_BINDING}, **extra)
        assert result["ok"] is False
        assert result["error"]["code"] == "ARGS_INVALID"


async def test_propose_chain_and_move_the_new_widgets_in_the_same_turn(mock_db):
    sink = []
    first = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi", after="s1",
                                           bindings={"value": KPI_BINDING}, dashboard_ops_sink=sink)
    second = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                            after=first["widgetId"], bindings={"value": KPI_BINDING},
                                            dashboard_ops_sink=sink)
    assert second["ok"] is True
    moved = await dashboard_apply(mock_db, USER, summary=_summary(), dashboard_ops_sink=sink, ops=[
        {"op": "moveWidget", "widgetId": first["widgetId"], "to": {"x": 0, "y": 0}},
        {"op": "resizeWidget", "widgetId": second["widgetId"], "size": {"w": 4, "h": 3}},
    ])
    assert moved["ok"] is True
    assert [o["op"] for o in sink] == ["addWidget", "addWidget", "moveWidget", "resizeWidget"]


async def test_propose_widget_requires_required_slots(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, type="kpi", bindings={})
    assert result["ok"] is False
    assert result["error"]["code"] == "SLOT_REQUIRED"
    assert result["error"]["path"] == "/bindings/value"


async def test_propose_a_heading_needs_no_bindings(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="heading", title="Costs")
    assert result["ok"] is True
    assert result["ops"][0]["widget"] == {"id": result["widgetId"], "type": "heading", "title": "Costs"}


async def test_propose_widget_rejects_a_dimension_the_metric_lacks(mock_db):
    binding = {"metric": "meta_ctr", "by": "day", "window": {"preset": "last_30d"}}
    result = await dashboard_propose_widget(mock_db, USER, type="trend", bindings={"series": binding})
    assert result["ok"] is False
    assert result["error"]["code"] == "DIMENSION_NOT_ALLOWED"
    assert result["error"]["path"] == "/bindings/series/by"


async def test_custom_window_is_rejected_unless_split_by_day(mock_db):
    custom = {"from": "2026-09-01", "to": "2026-09-30"}
    value = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                           bindings={"value": {"metric": "spend", "window": custom}})
    assert value["ok"] is False
    assert value["error"]["code"] == "WINDOW_UNSUPPORTED"
    assert value["error"]["path"] == "/bindings/value/window"
    assert "by" in value["error"]["message"] and "preset" in value["error"]["message"]

    ops = [{"op": "setBinding", "widgetId": "w1", "slot": "value",
            "binding": {"metric": "spend", "by": "client", "window": custom}}]
    by_client = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert by_client["ok"] is False
    assert by_client["error"]["code"] == "WINDOW_UNSUPPORTED"
    assert by_client["error"]["path"] == "/ops/0/binding/window"

    by_day = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="trend",
                                            bindings={"series": {"metric": "spend", "by": "day", "window": custom}})
    assert by_day["ok"] is True


async def test_propose_widget_puts_ops_in_the_sink(mock_db):
    sink = []
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                            bindings={"value": KPI_BINDING}, dashboard_ops_sink=sink)
    assert sink == result["ops"]


# ── apply ──────────────────────────────────────────────────────────────────


async def test_apply_returns_the_ops(mock_db):
    ops = [{"op": "resizeWidget", "widgetId": "w1", "size": {"w": 6, "h": 4}},
           {"op": "moveWidget", "widgetId": "w2", "to": {"x": 0, "y": 0}},
           {"op": "setLayout", "items": [{"widgetId": "s1", "x": 0, "y": 0, "w": 6, "h": 1},
                                         {"widgetId": "w1", "x": 6, "y": 0, "w": 3, "h": 3}]}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result == {"ok": True, "ops": ops, "note": "Applied in the user's editor."}


async def test_apply_with_unknown_widget_id_is_rejected_with_path(mock_db):
    sink = []
    ops = [{"op": "resizeWidget", "widgetId": "w1", "size": {"w": 6, "h": 4}},
           {"op": "removeWidget", "widgetId": "ghost"}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops, dashboard_ops_sink=sink)
    assert result["ok"] is False
    assert result["error"]["code"] == "WIDGET_NOT_FOUND"
    assert result["error"]["path"] == "/ops/1/widgetId"
    assert sink == []


@pytest.mark.parametrize("op, path", [
    ({"op": "addWidget", "widget": {"type": "kpi"}, "after": "ghost"}, "/ops/0/after"),
    ({"op": "setLayout", "items": [{"widgetId": "w1", "x": 0, "y": 0, "w": 3, "h": 3},
                                   {"widgetId": "ghost", "x": 3, "y": 0, "w": 3, "h": 3}]},
     "/ops/0/items/1/widgetId"),
])
async def test_apply_with_unknown_after_or_layout_id_is_rejected_with_path(mock_db, op, path):
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[op])
    assert result["ok"] is False
    assert result["error"]["code"] == "WIDGET_NOT_FOUND"
    assert result["error"]["path"] == path


@pytest.mark.parametrize("op", [
    {"op": "addSection", "section": {"id": "s9", "title": "New"}},
    {"op": "removeSection", "sectionId": "s1"},
    {"op": "moveSection", "sectionId": "s1", "toIndex": 0},
    {"op": "updateSection", "sectionId": "s1", "patch": {"level": 1}},
])
async def test_section_ops_are_rejected(mock_db, op):
    sink = []
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[op], dashboard_ops_sink=sink)
    assert result["ok"] is False
    assert result["error"]["code"] == "OP_INVALID"
    assert result["error"]["path"] == "/ops/0/op"
    assert "heading" in result["error"]["message"]
    assert sink == []


async def test_apply_ids_come_from_the_stored_doc_when_no_summary_is_sent(mock_db):
    await _store(mock_db)
    ok = await dashboard_apply(mock_db, USER, dashboard_id="d1", ops=[{"op": "removeWidget", "widgetId": "w2"}])
    assert ok["ok"] is True
    missing = await dashboard_apply(mock_db, USER, ops=[{"op": "removeWidget", "widgetId": "w2"}])
    assert missing["error"]["code"] == "WIDGET_NOT_FOUND"


async def test_apply_ids_come_from_a_stored_v1_doc_flattened(mock_db):
    await _store(mock_db, _v1_doc())
    ops = [{"op": "updateWidgetData", "widgetId": "s1", "patch": {"level": 1}},
           {"op": "removeWidget", "widgetId": "w2"}]
    assert (await dashboard_apply(mock_db, USER, dashboard_id="d1", ops=ops))["ok"] is True
    untitled = await dashboard_apply(mock_db, USER, dashboard_id="d1",
                                     ops=[{"op": "removeWidget", "widgetId": "s2"}])
    assert untitled["error"]["code"] == "WIDGET_NOT_FOUND"  # an untitled section has no heading


async def test_apply_ops_see_ids_created_and_removed_earlier_in_the_call(mock_db):
    created = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "addWidget", "widget": {"id": "h9", "type": "heading", "title": "New"}},
        {"op": "addWidget", "widget": {"id": "w9", "type": "kpi"}, "after": "h9"},
        {"op": "setBinding", "widgetId": "w9", "slot": "value", "binding": KPI_BINDING},
        {"op": "addWidget", "widget": {"id": "w10", "type": "kpi", "bindings": {"value": KPI_BINDING}},
         "after": "w9"},
    ])
    assert created["ok"] is True
    removed = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "removeWidget", "widgetId": "w1"},
        {"op": "resizeWidget", "widgetId": "w1", "size": {"w": 6, "h": 4}},
    ])
    assert removed["error"] == {"code": "WIDGET_NOT_FOUND", "path": "/ops/1/widgetId",
                                "message": removed["error"]["message"]}
    duplicate = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "addWidget", "widget": {"id": "s1", "type": "kpi"}},
    ])
    assert duplicate["error"]["code"] == "DUPLICATE_ID"
    assert duplicate["error"]["path"] == "/ops/0/widget/id"


async def test_apply_with_unknown_metric_returns_ok_false_with_path(mock_db):
    ops = [{"op": "setBinding", "widgetId": "w1", "slot": "value",
            "binding": {"metric": "made_up_metric", "window": {"preset": "last_30d"}}}]
    sink = []
    result = await dashboard_apply(mock_db, USER, ops=ops, dashboard_ops_sink=sink)
    assert result["ok"] is False
    assert result["error"]["code"] == "ARGS_INVALID"
    assert result["error"]["path"].startswith("/ops/0")
    assert result["error"]["message"]
    assert sink == []


async def test_apply_rejects_replace_dashboard(mock_db):
    result = await dashboard_apply(mock_db, USER, ops=[{"op": "replaceDashboard", "doc": _doc()}])
    assert result["ok"] is False
    assert result["error"]["code"] == "OP_FORBIDDEN"
    assert result["error"]["path"].startswith("/ops/0")


async def test_apply_rejects_a_dimension_the_metric_lacks(mock_db):
    ops = [{"op": "setBinding", "widgetId": "w1", "slot": "value",
            "binding": {"metric": "meta_ctr", "by": "day", "window": {"preset": "last_30d"}}}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result["ok"] is False
    assert result["error"]["code"] == "DIMENSION_NOT_ALLOWED"
    assert result["error"]["path"] == "/ops/0/binding/by"


async def test_apply_without_ops_is_invalid(mock_db):
    result = await dashboard_apply(mock_db, USER)
    assert result["ok"] is False
    assert result["error"]["code"] == "ARGS_INVALID"


_TITLE_HINTS = ("updateWidgetData", "level", "`text`")


@pytest.mark.parametrize("op, path", [
    ({"op": "setTitle", "title": "**Growth**"}, "/ops/0/title"),
    ({"op": "updateWidget", "widgetId": "w1", "patch": {"title": "`spend`"}}, "/ops/0/patch/title"),
    ({"op": "addWidget", "widget": {"id": "w9", "type": "kpi", "title": "_Spend_"}}, "/ops/0/widget/title"),
])
async def test_markdown_in_a_title_is_title_plain_text(mock_db, op, path):
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[op])
    assert result["ok"] is False
    assert result["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert result["error"]["path"] == path
    message = result["error"]["message"]
    assert all(hint in message for hint in _TITLE_HINTS)
    assert "updateSection" not in message


@pytest.mark.parametrize("op, path", [
    ({"op": "addWidget", "widget": {"type": "heading", "title": "## Overview"}}, "/ops/0/widget/title"),
    ({"op": "updateWidget", "widgetId": "s1", "patch": {"title": "# Overview"}}, "/ops/0/patch/title"),
])
async def test_heading_title_markdown_is_TITLE_PLAIN_TEXT(mock_db, op, path):
    sink = []
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[op], dashboard_ops_sink=sink)
    assert result["ok"] is False
    assert result["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert result["error"]["path"] == path
    assert all(hint in result["error"]["message"] for hint in _TITLE_HINTS)
    assert sink == []
    proposed = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="heading", title="# Big")
    assert proposed["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert proposed["error"]["path"] == "/title"


async def test_markdown_in_a_proposed_widget_title_is_title_plain_text(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                            title="**Spend**", bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert result["error"]["path"] == "/title"


def _markdownish_doc():
    """A dashboard whose own titles carry markdown markers (and quotes), as people type them in the editor."""
    doc = _doc()
    doc["title"] = "Client #1"
    doc["widgets"][0]["title"] = "CTR_7d"
    doc["widgets"][1]["title"] = '**Spend** "net"'
    return doc


async def test_unchanged_markdownish_titles_pass(mock_db):
    ops = [{"op": "setTitle", "title": "Client #1", "description": "Weekly numbers"},
           {"op": "updateWidget", "widgetId": "s1", "patch": {"title": "CTR_7d"}},
           {"op": "updateWidgetData", "widgetId": "s1", "patch": {"level": 1}},
           {"op": "updateWidget", "widgetId": "w1", "patch": {"title": '**Spend** "net"', "hidden": True}}]
    from_summary = await dashboard_apply(mock_db, USER, summary=summarize_doc(_markdownish_doc()), ops=ops)
    assert from_summary["ok"] is True
    await _store(mock_db, _markdownish_doc())
    from_doc = await dashboard_apply(mock_db, USER, dashboard_id="d1", ops=ops)
    assert from_doc["ok"] is True


@pytest.mark.parametrize("op, path", [
    ({"op": "setTitle", "title": "Client #2"}, "/ops/0/title"),
    ({"op": "updateWidget", "widgetId": "s1", "patch": {"title": "CTR_30d"}}, "/ops/0/patch/title"),
    ({"op": "updateWidget", "widgetId": "w1", "patch": {"title": "*Spend*"}}, "/ops/0/patch/title"),
    # w2 has no title, so w1's title is a change for it.
    ({"op": "updateWidget", "widgetId": "w2", "patch": {"title": '**Spend** "net"'}}, "/ops/0/patch/title"),
])
async def test_changed_markdownish_titles_are_still_rejected(mock_db, op, path):
    result = await dashboard_apply(mock_db, USER, summary=summarize_doc(_markdownish_doc()), ops=[op])
    assert result["ok"] is False
    assert result["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert result["error"]["path"] == path


async def test_a_title_changed_earlier_in_the_call_is_the_current_one(mock_db):
    ops = [{"op": "setTitle", "title": "Clients"},
           {"op": "setTitle", "title": "Client #1", "description": "back to the old name"}]
    result = await dashboard_apply(mock_db, USER, summary=summarize_doc(_markdownish_doc()), ops=ops)
    assert result["error"]["code"] == "TITLE_PLAIN_TEXT"
    assert result["error"]["path"] == "/ops/1/title"


async def test_plain_titles_with_punctuation_are_ok(mock_db):
    ops = [{"op": "setTitle", "title": "Q4 growth: spend & leads (EU) - v2!"},
           {"op": "updateWidget", "widgetId": "s1", "patch": {"title": "Overview / 30 days"}}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result["ok"] is True


async def test_change_widget_type_with_unknown_type_is_widget_type_unknown(mock_db):
    ops = [{"op": "changeWidgetType", "widgetId": "w1", "type": "sparkle"}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result["ok"] is False
    assert result["error"]["code"] == "WIDGET_TYPE_UNKNOWN"
    assert result["error"]["path"] == "/ops/0/type"
    assert "trend" in result["error"]["message"]


async def test_change_widget_type_with_known_id_and_type_is_ok(mock_db):
    ops = [{"op": "changeWidgetType", "widgetId": "w2", "type": "bar"},
           {"op": "setBinding", "widgetId": "w2", "slot": "series",
            "binding": {"metric": "spend", "by": "client", "window": {"preset": "last_30d"}}}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result == {"ok": True, "ops": ops, "note": "Applied in the user's editor."}


async def test_change_widget_type_with_unknown_widget_is_widget_not_found(mock_db):
    ops = [{"op": "changeWidgetType", "widgetId": "ghost", "type": "bar"}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result["error"]["code"] == "WIDGET_NOT_FOUND"
    assert result["error"]["path"] == "/ops/0/widgetId"


async def test_by_day_on_ctr_names_metrics_with_daily_series(mock_db):
    binding = {"metric": "meta_ctr", "by": "day", "window": {"preset": "last_30d"}}
    proposed = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="trend",
                                              bindings={"series": binding})
    applied = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "setBinding", "widgetId": "w2", "slot": "series", "binding": binding}])
    for result in (proposed, applied):
        assert result["error"]["code"] == "DIMENSION_NOT_ALLOWED"
        message = result["error"]["message"]
        assert "meta_ctr" in message and "`spend`" in message
        assert "`meta_ctr`" not in message  # only metrics that do have a daily series


async def test_by_day_refusal_lists_at_most_six_daily_metrics_untruncated(mock_db):
    daily = [m["key"] for m in build_catalog()["metrics"] if "day" in m["dimensions"]]
    assert len(daily) > 6  # the cap is exercised by the real catalog
    binding = {"metric": "meta_ctr", "by": "day", "window": {"preset": "last_30d"}}
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "setBinding", "widgetId": "w2", "slot": "series", "binding": binding}])
    message = result["error"]["message"]
    assert message.count("`") == 2 * 6
    assert all(f"`{key}`" in message for key in daily[:6])
    assert message.endswith(f" and {len(daily) - 6} more")
    assert len(message) <= 300


# ── describe / search ──────────────────────────────────────────────────────


async def test_describe_summarizes_a_stored_doc(mock_db):
    await _store(mock_db)
    result = await dashboard_describe(mock_db, USER, dashboard_id="d1")
    assert result == {"summary": "\n".join([
        "# Growth (3 widgets)",
        '- [s1] heading "Overview" 12×1 @0,0',
        '- [w1] kpi "Spend" 3×3 @0,1  value→meta_spend last_30d',
        '- [w2] trend "" 6×6 @3,1 hidden locked  series→spend by day 2026-09-01..2026-09-30',
    ])}


async def test_describe_flattens_a_stored_v1_doc(mock_db):
    await _store(mock_db, _v1_doc())
    result = await dashboard_describe(mock_db, USER, dashboard_id="d1")
    assert result == {"summary": summarize_doc(_v1_doc())}
    assert result["summary"].startswith("# Growth (3 widgets)\n- [s1] heading")


async def test_ids_are_read_from_summary_lines_with_levels_flags_and_bindings(mock_db):
    doc = _doc()
    doc["widgets"][0]["data"] = {"level": 1}
    doc["widgets"][1]["locked"] = True
    summary = summarize_doc(doc)
    assert "(h1)" in summary and " locked  value→" in summary
    for widget_id in ("s1", "w1", "w2"):
        result = await dashboard_apply(mock_db, USER, summary=summary,
                                       ops=[{"op": "updateWidget", "widgetId": widget_id, "patch": {"hidden": False}}])
        assert result["ok"] is True, widget_id
    proposed = await dashboard_propose_widget(mock_db, USER, summary=summary, type="kpi", after="w2",
                                              bindings={"value": KPI_BINDING})
    assert proposed["ok"] is True


async def test_describe_returns_the_sent_summary_verbatim(mock_db):
    await _store(mock_db)
    sent = '# Live edits (1 widgets)\n- [x] heading "Draft" 12×1 @0,0'
    result = await dashboard_describe(mock_db, USER, dashboard_id="d1", summary=sent)
    assert result == {"summary": sent}


async def test_describe_notes_ops_returned_earlier_in_the_turn(mock_db):
    sink = [{"op": "removeWidget", "widgetId": "w2"}]
    result = await dashboard_describe(mock_db, USER, summary=_summary(), dashboard_ops_sink=sink)
    assert result["summary"] == _summary()
    assert "1 op(s)" in result["note"]
    fresh = await dashboard_describe(mock_db, USER, summary=_summary(), dashboard_ops_sink=[])
    assert "note" not in fresh


async def test_describe_does_not_read_another_users_dashboard(mock_db):
    await _store(mock_db, user="bob@example.com")
    result = await dashboard_describe(mock_db, USER, dashboard_id="d1")
    assert result["ok"] is False
    assert result["error"]["code"] == "NOT_FOUND"


async def test_catalog_search_matches_key_label_and_description(mock_db):
    result = await catalog_search(mock_db, USER, query="SPEND")
    keys = [m["key"] for m in result["metrics"]]
    assert "meta_spend" in keys and "spend" in keys
    assert keys[0] == "spend"  # exact key match ranks first
    assert len(result["metrics"]) <= 20
    assert result["windows"] and {"preset", "label"} <= set(result["windows"][0])
    day = await catalog_search(mock_db, USER, query="day")
    assert day["dimensions"] == [{"key": "day", "label": "Day"}]


# ── prompt ─────────────────────────────────────────────────────────────────


def test_prompt_fragment_lists_widgets_metrics_and_windows():
    text = prompt_fragment()
    assert text.startswith("## Dashboard tools\n")
    assert "- `heading` — Heading: " in text
    assert "- `kpi` — KPI: " in text
    assert "(slots: value (value, required), spark (series, optional); default 3×3, min 2×2, max 6×4)" in text
    assert "- `meta_spend` — Meta Spend [currency, higher is better] by: client, day" in text
    assert "- `meta_cpc` — Meta CPC [currency, lower is better] by: client" in text
    assert "- `day` — Day" in text
    assert "## Examples" in text
    assert "updateSection" not in text and "span" not in text


def test_system_prompt_intro_has_no_section_ops():
    text = system_prompt()
    assert "updateSection" not in text and "addSection" not in text


# ── models + run_chat ──────────────────────────────────────────────────────


def test_chat_models_carry_dashboard_fields():
    req = ChatRequest(message="hi", page="dashboards", dashboard_id="d1", summary="# x")
    assert req.dashboard_id == "d1" and req.summary == "# x"
    ops = [{"op": "removeWidget", "widgetId": "w1"}]
    body = ChatResponse(reply="ok", dashboard_ops=ops).model_dump()
    assert body["dashboard_ops"] == ops
    assert ChatResponse(reply="ok").model_dump()["dashboard_ops"] == []


@pytest.fixture
def dash_registry(monkeypatch):
    # get_client_groups is an MCP tool, so the dashboards page would open a real
    # MCP connection; the registry fallback is what these tests exercise.
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    reg = ToolRegistry()
    register_dashboard_tools(reg)
    return reg


async def test_run_chat_collects_dashboard_ops(mock_db, dash_registry):
    sent_summary = summarize_doc(_doc())
    ops = [{"op": "removeWidget", "widgetId": "w2"}]
    provider = ScriptedProvider([
        _tool_call_response("dashboard_describe", {}, "tc-1"),
        _tool_call_response("dashboard_apply", {"ops": ops}, "tc-2"),
        _tool_call_response("dashboard_apply", {"ops": [{"op": "bogus"}]}, "tc-3"),
        _tool_call_response("dashboard_propose_widget", {"type": "kpi", "bindings": {"value": KPI_BINDING}}, "tc-4"),
        ProviderResponse(content="Removed the trend and added a spend KPI."),
    ])
    result = await run_chat(
        provider=provider, tool_registry=dash_registry, db=mock_db, user_id=USER,
        message="clean this up", page="dashboards",
        dashboard_id="d1", dashboard_summary=sent_summary,
    )
    # describe saw the summary the editor sent
    describe_result = json.loads(provider.calls[1]["messages"][-1]["content"])
    assert describe_result == {"summary": sent_summary}
    # the failed apply contributed nothing; propose added one widget at the bottom
    assert result["dashboard_ops"][0] == ops[0]
    assert [o["op"] for o in result["dashboard_ops"]] == ["removeWidget", "addWidget"]
    assert "after" not in result["dashboard_ops"][1]
    assert result["tools_used"] == ["dashboard_describe", "dashboard_apply", "dashboard_apply",
                                    "dashboard_propose_widget"]
    # the dashboards page gets the dashboard tools and the dashkit prompt
    tool_names = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    assert set(DASHBOARD_TOOL_NAMES) <= tool_names
    system = provider.calls[0]["messages"][0]["content"]
    assert "## Dashboard tools" in system and "Birdy" in system


async def test_run_chat_returns_dashboard_ops_on_the_max_iterations_path(mock_db, dash_registry):
    ops = [{"op": "setTitle", "title": "Renamed"}]
    responses = [_tool_call_response("dashboard_apply", {"ops": ops}, f"tc-{i}")
                 for i in range(MAX_TOOL_ITERATIONS + 1)]
    result = await run_chat(
        provider=ScriptedProvider(responses), tool_registry=dash_registry, db=mock_db, user_id=USER,
        message="rename it", page="dashboards",
    )
    assert "complexity limits" in result["reply"]
    assert result["dashboard_ops"] == ops * (MAX_TOOL_ITERATIONS + 1)


async def test_run_chat_off_the_dashboards_page_returns_no_ops_and_hides_tools(mock_db, dash_registry):
    provider = ScriptedProvider([ProviderResponse(content="hello")])
    result = await run_chat(
        provider=provider, tool_registry=dash_registry, db=mock_db, user_id=USER, message="hi",
    )
    assert result["dashboard_ops"] == []
    tool_names = {t["function"]["name"] for t in provider.calls[0]["tools"] or []}
    assert not tool_names & set(DASHBOARD_TOOL_NAMES)
