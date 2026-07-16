"""services/slack_block_formatter.py — literal example payloads from
ai/prompts/birdy.py for all 5 block types (that file is the wire-format
source of truth), plus graceful degradation on malformed LLM output."""

from services.slack_block_formatter import build_blocks_from_reply, build_modal_view


def test_metric_block_renders_with_delta_and_sparkline():
    reply = (
        ':::metric\n'
        '{"label":"Total Revenue","value":27122,"format":"currency","delta":3141,'
        '"deltaLabel":"vs last month","icon":"dollar-sign","sparkline":[12000,14500,18200,21300,23800,27122]}\n'
        ':::'
    )
    blocks, fallback, ui_pending = build_blocks_from_reply(reply, iid_prefix="t1")
    assert ui_pending == []
    section = blocks[0]
    assert section["type"] == "section"
    assert "Total Revenue" in section["text"]["text"]
    assert "$27,122" in section["text"]["text"]
    assert "▲" in section["text"]["text"]  # positive delta arrow
    assert blocks[1]["type"] == "context"  # sparkline


def test_metric_negative_delta_shows_down_arrow():
    reply = ':::metric\n{"label":"CPL","value":12,"format":"currency","delta":-2}\n:::'
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert "▼" in blocks[0]["text"]["text"]


def test_stats_block_uses_native_fields_grid():
    reply = (
        ':::stats\n'
        '[{"label":"Won","value":40,"format":"integer","variant":"success"},'
        '{"label":"Lost","value":104,"format":"integer","variant":"error"}]\n'
        ':::'
    )
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert blocks[0]["type"] == "section"
    assert len(blocks[0]["fields"]) == 2
    assert "Won" in blocks[0]["fields"][0]["text"]
    assert "40" in blocks[0]["fields"][0]["text"]


def test_status_block_shows_variant_emoji():
    reply = ':::status\n{"label":"Alert triggered","variant":"warning","detail":"CPL exceeded $15"}\n:::'
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert "⚠️" in blocks[0]["text"]["text"]
    assert "CPL exceeded $15" in blocks[0]["text"]["text"]


def test_bar_chart_sorted_descending_with_currency():
    reply = (
        ':::chart\n'
        '{"type":"bar","title":"Won opps by client",'
        '"data":[{"label":"Aura","value":40},{"label":"BBL","value":41},{"label":"Plush","value":12}],'
        '"currency":"£"}\n:::'
    )
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    text = blocks[0]["text"]["text"]
    assert "£41" in text and "£40" in text and "£12" in text
    # BBL (41) must appear before Aura (40) — descending sort
    assert text.index("BBL") < text.index("Aura")


def test_donut_chart_shows_percentages():
    reply = (
        ':::chart\n'
        '{"type":"donut","title":"Lead sources",'
        '"data":[{"label":"Meta","value":600},{"label":"GHL","value":400}]}\n:::'
    )
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    text = blocks[0]["text"]["text"]
    assert "60%" in text and "40%" in text


def test_composed_chart_emits_one_block_per_series():
    reply = (
        ':::chart\n'
        '{"type":"composed","title":"Spend vs CPL",'
        '"data":[{"label":"Jan","spend":1200,"cpl":3.5},{"label":"Feb","spend":1400,"cpl":3.2}],'
        '"series":[{"key":"spend","name":"Ad Spend","type":"bar"},{"key":"cpl","name":"CPL","type":"line"}]}\n:::'
    )
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert len(blocks) == 2
    assert "Ad Spend" in blocks[0]["text"]["text"]
    assert "CPL" in blocks[1]["text"]["text"]


def test_malformed_block_degrades_gracefully_without_dropping_rest_of_reply():
    reply = "Before.\n\n:::metric\n{not valid json}\n:::\n\nAfter."
    blocks, fallback, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    texts = [b["text"]["text"] for b in blocks]
    assert any("Before." in t for t in texts)
    assert any("Couldn't render" in t for t in texts)
    assert any("After." in t for t in texts)


def test_prose_converted_to_mrkdwn():
    reply = "**Bold text** and a [link](https://example.com) and:\n- item one\n- item two"
    blocks, _, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    text = blocks[0]["text"]["text"]
    assert "*Bold text*" in text
    assert "<https://example.com|link>" in text
    assert "• item one" in text


def test_fallback_text_strips_blocks_to_prose_only():
    reply = "Summary:\n\n:::metric\n{\"label\":\"X\",\"value\":1}\n:::\n\nDone."
    _, fallback, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert "Summary:" in fallback and "Done." in fallback
    assert ":::" not in fallback


def test_all_blocks_fallback_text_is_placeholder():
    reply = ':::metric\n{"label":"X","value":1}\n:::'
    _, fallback, _ = build_blocks_from_reply(reply, iid_prefix="t1")
    assert fallback == "Birdy sent an interactive message"


# ── :::ui — inline vs modal decision ─────────────────────────────────────────

def test_ui_all_selectable_fields_renders_inline_with_submit_button():
    reply = (
        ':::ui\n'
        '[{"id":"type","type":"radio","label":"Warning or win?",'
        '"options":[{"value":"warning","label":"Warning"},{"value":"win","label":"Win"}]}]\n:::'
    )
    blocks, _, ui_pending = build_blocks_from_reply(reply, iid_prefix="chat_abc")
    assert len(ui_pending) == 1
    assert ui_pending[0]["mode"] == "inline"
    assert ui_pending[0]["iid"] == "chat_abc:0"
    # one field-row Section + one Actions (submit) block
    assert blocks[0]["type"] == "section"
    assert blocks[0]["accessory"]["type"] == "radio_buttons"
    assert blocks[0]["block_id"] == "ui|chat_abc:0|type"
    assert blocks[-1]["type"] == "actions"
    assert blocks[-1]["elements"][0]["action_id"] == "ui_submit"


def test_ui_grouped_select_options():
    reply = (
        ':::ui\n'
        '[{"id":"metric","type":"select","label":"Metric","options":['
        '{"group":"Meta Ads","options":[{"value":"spend","label":"Spend"}]}]}]\n:::'
    )
    blocks, _, ui_pending = build_blocks_from_reply(reply, iid_prefix="chat_abc")
    accessory = blocks[0]["accessory"]
    assert accessory["type"] == "static_select"
    assert "option_groups" in accessory
    assert accessory["option_groups"][0]["label"]["text"] == "Meta Ads"


def test_ui_with_text_field_renders_modal_trigger_button_only():
    reply = (
        ':::ui\n'
        '[{"id":"name","type":"text","label":"Alert name","required":true},'
        '{"id":"metric","type":"select","label":"Metric","options":[{"value":"spend","label":"Spend"}]}]\n:::'
    )
    blocks, _, ui_pending = build_blocks_from_reply(reply, iid_prefix="chat_xyz")
    assert len(ui_pending) == 1
    assert ui_pending[0]["mode"] == "modal"
    assert len(blocks) == 1
    assert blocks[0]["type"] == "actions"
    assert blocks[0]["elements"][0]["action_id"] == "ui_open_modal"
    assert blocks[0]["elements"][0]["value"] == "chat_xyz:0"


def test_ui_with_number_field_also_triggers_modal():
    reply = ':::ui\n[{"id":"value","type":"number","label":"Threshold"}]\n:::'
    _, _, ui_pending = build_blocks_from_reply(reply, iid_prefix="chat_xyz")
    assert ui_pending[0]["mode"] == "modal"


def test_build_modal_view_maps_field_types_correctly():
    fields = [
        {"id": "name", "type": "text", "label": "Name", "required": True, "placeholder": "e.g. My Alert"},
        {"id": "threshold", "type": "number", "label": "Threshold", "min": 0, "max": 100},
        {"id": "metric", "type": "select", "label": "Metric",
         "options": [{"value": "spend", "label": "Spend"}]},
    ]
    view = build_modal_view(fields, "chat_xyz:0")
    assert view["type"] == "modal"
    assert view["private_metadata"] == "chat_xyz:0"
    assert view["callback_id"] == "ui_form_submit"

    name_block = next(b for b in view["blocks"] if b["block_id"] == "name")
    assert name_block["element"]["type"] == "plain_text_input"
    assert name_block["optional"] is False

    threshold_block = next(b for b in view["blocks"] if b["block_id"] == "threshold")
    assert threshold_block["element"]["type"] == "number_input"
    assert threshold_block["element"]["min_value"] == "0"
    assert threshold_block["optional"] is True  # not marked required

    metric_block = next(b for b in view["blocks"] if b["block_id"] == "metric")
    assert metric_block["element"]["type"] == "static_select"
    assert metric_block["element"]["action_id"] == "value"
