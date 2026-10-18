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
from ai.prompts.dashboards import prompt_fragment
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


def _doc():
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
        "meta": {"createdAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z", "lastAuthor": "user"},
    }


async def _store(db, doc=None, user=USER):
    doc = doc or _doc()
    await db["dashboards"].insert_one({"id": doc["id"], "user_id": user, "doc": doc})


KPI_BINDING = {"metric": "meta_spend", "window": {"preset": "last_30d"}, "compare": "previous"}


def _summary():
    """What the editor sends: summarize(doc) of the live doc."""
    return summarize_doc(_doc())


# ── propose ────────────────────────────────────────────────────────────────


async def test_propose_widget_returns_add_widget_and_set_binding_ops(mock_db):
    result = await dashboard_propose_widget(
        mock_db, USER, summary=_summary(), sectionId="s1", type="kpi", title="Spend",
        bindings={"value": KPI_BINDING},
    )
    assert result["ok"] is True
    add, bind = result["ops"]
    widget_id = add["widget"]["id"]
    assert len(widget_id) == 12
    assert add == {
        "op": "addWidget", "sectionId": "s1",
        "widget": {"id": widget_id, "type": "kpi", "title": "Spend", "span": {"cols": 3, "rows": 1}},
    }
    assert bind == {"op": "setBinding", "widgetId": widget_id, "slot": "value", "binding": KPI_BINDING}
    assert result["widgetId"] == widget_id
    assert result["note"] == "Applied in the user's editor."


async def test_propose_widget_without_section_uses_first_section_from_summary(mock_db):
    summary = summarize_doc(_doc()).replace("[s1]", "[live-first]")
    result = await dashboard_propose_widget(
        mock_db, USER, summary=summary, type="kpi", bindings={"value": KPI_BINDING},
    )
    assert result["ok"] is True
    assert result["ops"][0]["sectionId"] == "live-first"


async def test_propose_widget_without_section_falls_back_to_stored_doc(mock_db):
    await _store(mock_db)
    result = await dashboard_propose_widget(
        mock_db, USER, dashboard_id="d1", type="kpi", bindings={"value": KPI_BINDING},
    )
    assert result["ops"][0]["sectionId"] == "s1"


async def test_propose_widget_without_any_section_is_rejected(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, type="kpi", bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"]["code"] == "NO_SECTION"


async def test_propose_widget_trusts_a_summary_without_sections_over_the_stored_doc(mock_db):
    await _store(mock_db)
    result = await dashboard_propose_widget(mock_db, USER, dashboard_id="d1", summary="# Empty (0 sections, 0 widgets)",
                                            type="kpi", bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"]["code"] == "NO_SECTION"


async def test_propose_widget_into_an_unknown_section_is_rejected(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), sectionId="nope", type="kpi",
                                            bindings={"value": KPI_BINDING})
    assert result["ok"] is False
    assert result["error"] == {"code": "SECTION_NOT_FOUND", "path": "/sectionId",
                               "message": result["error"]["message"]}


async def test_propose_then_move_the_new_widget_in_the_same_turn(mock_db):
    sink = []
    proposed = await dashboard_propose_widget(mock_db, USER, summary=_summary(), type="kpi",
                                              bindings={"value": KPI_BINDING}, dashboard_ops_sink=sink)
    moved = await dashboard_apply(mock_db, USER, summary=_summary(), dashboard_ops_sink=sink, ops=[
        {"op": "moveWidget", "widgetId": proposed["widgetId"], "toSectionId": "s2", "toIndex": 0},
        {"op": "resizeWidget", "widgetId": proposed["widgetId"], "span": {"cols": 4, "rows": 1}},
    ])
    assert moved["ok"] is True
    assert [o["op"] for o in sink] == ["addWidget", "setBinding", "moveWidget", "resizeWidget"]


async def test_propose_widget_requires_required_slots(mock_db):
    result = await dashboard_propose_widget(mock_db, USER, sectionId="s1", type="kpi", bindings={})
    assert result["ok"] is False
    assert result["error"]["code"] == "SLOT_REQUIRED"
    assert result["error"]["path"] == "/bindings/value"


async def test_propose_widget_rejects_a_dimension_the_metric_lacks(mock_db):
    binding = {"metric": "meta_ctr", "by": "day", "window": {"preset": "last_30d"}}
    result = await dashboard_propose_widget(mock_db, USER, sectionId="s1", type="trend",
                                            bindings={"series": binding})
    assert result["ok"] is False
    assert result["error"]["code"] == "DIMENSION_NOT_ALLOWED"
    assert result["error"]["path"] == "/bindings/series/by"


async def test_propose_widget_puts_ops_in_the_sink(mock_db):
    sink = []
    result = await dashboard_propose_widget(mock_db, USER, summary=_summary(), sectionId="s1", type="kpi",
                                            bindings={"value": KPI_BINDING}, dashboard_ops_sink=sink)
    assert sink == result["ops"]


# ── apply ──────────────────────────────────────────────────────────────────


async def test_apply_returns_the_ops(mock_db):
    ops = [{"op": "resizeWidget", "widgetId": "w1", "span": {"cols": 6, "rows": 2}}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result == {"ok": True, "ops": ops, "note": "Applied in the user's editor."}


async def test_apply_with_unknown_widget_id_is_rejected_with_path(mock_db):
    sink = []
    ops = [{"op": "resizeWidget", "widgetId": "w1", "span": {"cols": 6, "rows": 2}},
           {"op": "removeWidget", "widgetId": "ghost"}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops, dashboard_ops_sink=sink)
    assert result["ok"] is False
    assert result["error"]["code"] == "WIDGET_NOT_FOUND"
    assert result["error"]["path"] == "/ops/1/widgetId"
    assert sink == []


async def test_apply_with_unknown_target_section_is_rejected_with_path(mock_db):
    ops = [{"op": "moveWidget", "widgetId": "w1", "toSectionId": "ghost", "toIndex": 0}]
    result = await dashboard_apply(mock_db, USER, summary=_summary(), ops=ops)
    assert result["error"]["code"] == "SECTION_NOT_FOUND"
    assert result["error"]["path"] == "/ops/0/toSectionId"
    removed = await dashboard_apply(mock_db, USER, summary=_summary(),
                                    ops=[{"op": "removeSection", "sectionId": "ghost"}])
    assert removed["error"]["path"] == "/ops/0/sectionId"


async def test_apply_ids_come_from_the_stored_doc_when_no_summary_is_sent(mock_db):
    await _store(mock_db)
    ok = await dashboard_apply(mock_db, USER, dashboard_id="d1", ops=[{"op": "removeWidget", "widgetId": "w2"}])
    assert ok["ok"] is True
    missing = await dashboard_apply(mock_db, USER, ops=[{"op": "removeWidget", "widgetId": "w2"}])
    assert missing["error"]["code"] == "WIDGET_NOT_FOUND"


async def test_apply_ops_see_ids_created_and_removed_earlier_in_the_call(mock_db):
    created = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "addSection", "section": {"id": "s3", "title": "New"}},
        {"op": "addWidget", "sectionId": "s3", "widget": {"id": "w9", "type": "kpi"}},
        {"op": "setBinding", "widgetId": "w9", "slot": "value", "binding": KPI_BINDING},
    ])
    assert created["ok"] is True
    removed = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "removeSection", "sectionId": "s1"},
        {"op": "resizeWidget", "widgetId": "w1", "span": {"cols": 6, "rows": 2}},
    ])
    assert removed["error"] == {"code": "WIDGET_NOT_FOUND", "path": "/ops/1/widgetId",
                                "message": removed["error"]["message"]}
    duplicate = await dashboard_apply(mock_db, USER, summary=_summary(), ops=[
        {"op": "addWidget", "sectionId": "s1", "widget": {"id": "w1", "type": "kpi"}},
    ])
    assert duplicate["error"]["code"] == "DUPLICATE_ID"


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


# ── describe / search ──────────────────────────────────────────────────────


async def test_describe_summarizes_a_stored_doc(mock_db):
    await _store(mock_db)
    result = await dashboard_describe(mock_db, USER, dashboard_id="d1")
    assert result == {"summary": "\n".join([
        "# Growth (2 sections, 2 widgets)",
        "## Overview [s1] (2 widgets)",
        '- [w1] kpi "Spend" 3x1  value→meta_spend last_30d',
        '- [w2] trend "" 6x2  series→spend by day 2026-09-01..2026-09-30 (hidden) (locked)',
        "## (untitled) [s2] (0 widgets)",
    ])}


async def test_describe_returns_the_sent_summary_verbatim(mock_db):
    await _store(mock_db)
    sent = "# Live edits (1 sections, 0 widgets)\n## Draft [x] (0 widgets)"
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
    assert "- `kpi` — KPI: " in text
    assert "(slots: value (value, required); default 3×1, min 2×1, max 6×2)" in text
    assert "- `meta_spend` — Meta Spend [currency, higher is better] by: client, day" in text
    assert "- `meta_cpc` — Meta Cpc [currency, lower is better] by: client" in text
    assert "- `day` — Day" in text
    assert "## Examples" in text


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
    # the failed apply contributed nothing; propose landed in the first section
    assert result["dashboard_ops"][0] == ops[0]
    assert [o["op"] for o in result["dashboard_ops"]] == ["removeWidget", "addWidget", "setBinding"]
    assert result["dashboard_ops"][1]["sectionId"] == "s1"
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
