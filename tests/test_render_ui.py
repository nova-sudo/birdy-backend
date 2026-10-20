"""
tests/test_render_ui.py
-----------------------
The render_ui tool: spec validation, the blocks it writes, placeholder
swapping in the final reply, the orchestrator wiring on every analysis page,
and that its blocks still render natively in Slack.
"""

import json

import pytest

from ai.orchestrator import run_chat, _build_client_detail_prompt, _PAGE_SYSTEM_PROMPTS, _PAGE_TOOLS
from ai.prompts.birdy import get_system_prompt
from ai.providers.base import BaseLLMProvider, ProviderResponse, ToolCall
from ai.tools.registry import ToolRegistry
from ai.tools.render_ui_tools import (
    MAX_COMPONENTS_PER_TURN, SpecError, build_block, materialize, register_render_ui_tools, render_ui,
)
from services.slack_block_formatter import build_blocks_from_reply


def _payload(block: str):
    head, body, tail = block.split("\n")
    assert tail == ":::"
    return head[3:], json.loads(body)


# ── build_block ─────────────────────────────────────────────────────────────

def test_kpi_becomes_metric_block_with_percent_delta_and_polarity():
    kind, p = _payload(build_block({
        "kind": "kpi", "label": "Cost per lead", "value": "£14.20", "format": "currency",
        "currency": "£", "delta_pct": -12.5, "delta_label": "vs previous 30 days",
        "polarity": "lower_is_better", "sparkline": [18, 16, 15, 14.2],
    }))
    assert kind == "metric"
    assert p == {
        "label": "Cost per lead", "value": 14.2, "format": "currency", "currency": "£",
        "deltaPct": -12.5, "deltaLabel": "vs previous 30 days", "polarity": "lower",
        "sparkline": [18, 16, 15, 14.2],
    }


def test_neutral_polarity_for_spend():
    _, p = _payload(build_block({"kind": "kpi", "label": "Spend", "value": 10, "delta_pct": -3, "polarity": "neutral"}))
    assert p["polarity"] == "neutral"


def test_kpi_value_can_be_unknown():
    _, p = _payload(build_block({"kind": "kpi", "label": "CPL", "value": None, "format": "currency", "currency": "£"}))
    assert p["value"] is None and p["label"] == "CPL"


def test_kpi_row_becomes_stats_block():
    kind, p = _payload(build_block({"kind": "kpi_row", "items": [
        {"label": "Spend", "value": 4210, "format": "currency", "currency": "£"},
        {"label": "Leads", "value": 296, "delta_pct": 8.1},
    ]}))
    assert kind == "stats"
    assert [s["label"] for s in p] == ["Spend", "Leads"]
    assert p[1]["deltaPct"] == 8.1 and p[1]["format"] == "integer"


def test_single_series_line_keeps_time_order_and_uses_value_key():
    kind, p = _payload(build_block({
        "kind": "line", "title": "Daily leads", "subtitle": "All clients · last 3 days",
        "data": [{"label": "Oct 1", "leads": 10}, {"label": "Oct 2", "leads": 14}, {"label": "Oct 3", "leads": 9}],
        "series": [{"key": "leads", "name": "Leads"}],
    }))
    assert kind == "chart"
    assert p["type"] == "line" and p["sort"] is False
    assert p["data"] == [{"label": "Oct 1", "value": 10}, {"label": "Oct 2", "value": 14}, {"label": "Oct 3", "value": 9}]
    assert p["seriesName"] == "Leads"


def test_combo_puts_second_series_on_right_axis():
    kind, p = _payload(build_block({
        "kind": "combo", "title": "Ad spend vs CPL", "currency": "£",
        "data": [{"label": "Oct 1", "spend": 120.5, "cpl": 14.1}, {"label": "Oct 2", "spend": 98, "cpl": None}],
        "series": [
            {"key": "spend", "name": "Ad spend", "type": "bar", "format": "currency"},
            {"key": "cpl", "name": "CPL", "type": "line", "format": "currency"},
        ],
    }))
    assert p["type"] == "composed"
    assert [(s["key"], s["axis"], s["type"], s["currency"]) for s in p["series"]] == [
        ("spend", "left", "bar", "£"), ("cpl", "right", "line", "£"),
    ]
    assert p["data"][1] == {"label": "Oct 2", "spend": 98, "cpl": None}


def test_iso_currency_codes_become_symbols():
    _, p = _payload(build_block({"kind": "kpi", "label": "Spend", "value": 10, "currency": "GBP"}))
    assert p["currency"] == "£" and p["format"] == "currency"


def test_a_second_unit_gets_the_right_axis():
    _, p = _payload(build_block({"kind": "bar", "data": [{"label": "Aura", "spend": 4238.6, "leads": 294}],
                                 "series": [{"key": "spend", "name": "Spend", "format": "currency", "currency": "£"},
                                            {"key": "leads", "name": "Leads", "format": "integer"}]}))
    assert [s["axis"] for s in p["series"]] == ["left", "right"]


def test_table_is_a_gfm_table():
    out = build_block({
        "kind": "table", "title": "Top campaigns", "currency": "£",
        "columns": [{"key": "name", "label": "Campaign"}, {"key": "spend", "label": "Spend", "format": "currency"},
                    {"key": "ctr", "label": "CTR", "format": "percentage"}],
        "rows": [{"name": "Botox | Spring", "spend": 1520, "ctr": 1.234}],
    })
    assert out.splitlines() == [
        "**Top campaigns**", "",
        "| Campaign | Spend | CTR |",
        "| --- | ---: | ---: |",
        "| Botox \\| Spring | £1,520 | 1.2% |",
    ]


@pytest.mark.parametrize("spec, msg", [
    ({"kind": "pie"}, "kind must be one of"),
    ({"kind": "kpi", "label": "Spend", "value": "lots"}, "value must be a number"),
    ({"kind": "kpi", "label": "Spend"}, "value is missing"),
    ({"kind": "kpi", "value": 3}, "label is required"),
    ({"kind": "combo", "data": [{"label": "a", "x": 1}], "series": [{"key": "x", "name": "X"}]}, "at least two series"),
    ({"kind": "bar", "data": [{"label": str(i), "value": i} for i in range(121)]}, "at most 120"),
    ({"kind": "line", "data": [{"label": "a", "x": 1}],
      "series": [{"key": "y", "name": "Y"}, {"key": "z", "name": "Z"}]}, "no row has a value"),
    ({"kind": "kpi_row", "items": [{"label": str(i), "value": i} for i in range(5)]}, "at most 4"),
    ({"kind": "kpi", "label": "CPL", "value": 3, "polarity": "down"}, "polarity must be"),
])
def test_bad_specs_name_the_field(spec, msg):
    with pytest.raises(SpecError, match=msg):
        build_block(spec)


# ── executor + materialize ──────────────────────────────────────────────────

async def test_render_ui_returns_placeholders_and_fills_the_sink():
    sink = []
    out = await render_ui(components=[{"kind": "kpi", "label": "Leads", "value": 4}], ui_blocks_sink=sink)
    assert out["placeholders"] == ["[[viz:1]]"]
    assert len(sink) == 1 and sink[0].startswith(":::metric\n")
    out = await render_ui(components=[{"kind": "kpi", "label": "Spend", "value": 9}], ui_blocks_sink=sink)
    assert out["placeholders"] == ["[[viz:2]]"]


async def test_render_ui_errors_render_nothing():
    sink = []
    out = await render_ui(components=[{"kind": "kpi", "label": "ok", "value": 1}, {"kind": "kpi", "label": "bad", "value": "x"}],
                          ui_blocks_sink=sink)
    assert "components[1].value must be a number" in out["error"]
    assert sink == []


async def test_render_ui_caps_visuals_per_turn_and_needs_the_chat():
    sink = ["x"] * MAX_COMPONENTS_PER_TURN
    out = await render_ui(components=[{"kind": "kpi", "label": "a", "value": 1}], ui_blocks_sink=sink)
    assert "At most" in out["error"]
    assert "only available in the chat" in (await render_ui(components=[{"kind": "kpi"}]))["error"]


def test_materialize_places_blocks_and_rescues_unplaced_ones():
    blocks = [":::metric\n{}\n:::", ":::chart\n{}\n:::"]
    out = materialize("Here's the week.\n\n[[viz:2]]\n\nCPL fell. [[viz:9]]", blocks)
    assert out == "Here's the week.\n\n:::metric\n{}\n:::\n\n:::chart\n{}\n:::\n\nCPL fell."
    assert materialize("No visuals [[viz:1]] here.", []) == "No visuals  here."


# ── orchestrator wiring ─────────────────────────────────────────────────────

class ScriptedProvider(BaseLLMProvider):
    def __init__(self, responses):
        self._responses = list(responses)
        self.model = "stub-model"
        self.tool_names = []
        self.system_prompt = None

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
        self.system_prompt = messages[0]["content"]
        if tools:
            self.tool_names = [t["function"]["name"] for t in tools]
        return self._responses.pop(0)


@pytest.mark.parametrize("page", ["client_detail", "dashboard", "call_center", "campaigns", None])
async def test_chat_renders_a_chart_on_every_analysis_page(mock_db, page):
    reg = ToolRegistry()
    register_render_ui_tools(reg)
    spec = {"components": [{"kind": "line", "title": "Spend", "data": [
        {"label": "Oct 1", "value": 10}, {"label": "Oct 2", "value": 12}]}]}
    provider = ScriptedProvider([
        ProviderResponse(content=None, tool_calls=[ToolCall(id="t1", name="render_ui", arguments=json.dumps(spec))],
                         finish_reason="tool_calls"),
        ProviderResponse(content="Spend is climbing.\n\n[[viz:1]]\n\nUp 20% in a day."),
    ])
    result = await run_chat(provider=provider, tool_registry=reg, db=mock_db, user_id="user-1",
                            message="chart my spend", page=page, client_group_id="grp-A" if page else None,
                            client_name="Acme" if page else None)
    assert "render_ui" in provider.tool_names
    assert "You CAN draw charts" in provider.system_prompt
    assert result["tools_used"] == ["render_ui"]
    assert result["reply"].startswith("Spend is climbing.\n\n:::chart\n{")
    assert "[[viz:" not in result["reply"]
    # Stored as rendered, so reopening the thread replays the chart.
    stored = await mock_db["ai_chat_sessions"].find_one({"_id": result["session_id"]})
    assert stored["messages"][-1]["content"] == result["reply"]


def test_every_analysis_prompt_documents_render_ui_and_tool_is_allowed():
    prompts = [get_system_prompt(), _build_client_detail_prompt("Acme", "grp-A"),
               _PAGE_SYSTEM_PROMPTS["dashboard"], _PAGE_SYSTEM_PROMPTS["call_center"]]
    for p in prompts:
        assert "render_ui" in p and "Never say you can't make charts" in p
    for page in ("dashboard", "campaigns", "leads", "opportunities", "call_center", "client_detail"):
        assert "render_ui" in _PAGE_TOOLS[page]
    # Narrow single-purpose assistants stay as they were.
    for page in ("alerts", "custom_metrics", "report_designer", "dashboards"):
        assert "render_ui" not in _PAGE_TOOLS[page]


# ── Slack ───────────────────────────────────────────────────────────────────

def test_render_ui_blocks_render_natively_in_slack():
    line = build_block({"kind": "area", "title": "Daily spend", "currency": "£", "format": "currency",
                        "data": [{"label": "Oct 1", "value": 10}, {"label": "Oct 2", "value": 30}]})
    kpi = build_block({"kind": "kpi", "label": "CPL", "value": 14.2, "format": "currency", "currency": "£",
                       "delta_pct": -12.5, "delta_label": "vs last week", "polarity": "lower_is_better"})
    blocks, _, _ = build_blocks_from_reply(f"Here you go.\n\n{kpi}\n\n{line}", iid_prefix="s")
    viz = [b for b in blocks if b["type"] == "data_visualization"]
    assert len(viz) == 1 and viz[0]["chart"]["type"] == "area"
    # time order kept (sort False), not re-ranked by value
    assert [d["label"] for d in viz[0]["chart"]["series"][0]["data"]] == ["Oct 1", "Oct 2"]
    context = [b for b in blocks if b["type"] == "context"]
    assert any("▼ 12.5% vs last week" in b["elements"][0]["text"] for b in context)


def test_unknown_kpi_value_prints_a_dash_in_slack():
    kpi = build_block({"kind": "kpi", "label": "CPL", "value": None, "format": "currency", "currency": "£"})
    blocks, _, _ = build_blocks_from_reply(kpi, iid_prefix="s")
    assert blocks[0]["text"]["text"] == "–"
