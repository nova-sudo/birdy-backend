"""
services/slack_block_formatter.py
------------------------------------
Converts a raw Birdy AI reply (as returned by ai/orchestrator.py::run_chat(),
untouched) into real Slack Block Kit blocks. The reply text contains custom
fenced blocks (:::metric, :::stats, :::chart, :::status, :::ui — see
ai/prompts/birdy.py for the full grammar) that only the web frontend
currently parses; this is the server-side equivalent for Slack.

Pure/synchronous and DB-free by design — easy to unit test with literal
example payloads. :::ui blocks need a pending-interaction record persisted
before the message is sent (see services/slack_interaction_store.py); this
module returns everything the caller needs to do that (`ui_pending`) rather
than writing to Mongo itself.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

_BLOCK_RE = re.compile(r":::(metric|stats|chart|status|ui)\n(.*?)\n:::", re.DOTALL)

_MAX_SECTION_CHARS = 2900  # Slack's limit is 3000; leave headroom for wrapper text
_MAX_BLOCKS = 50

_METRIC_ICONS = {
    "dollar-sign": "💰", "target": "🎯", "users": "👥",
    "activity": "📈", "bar-chart": "📊",
}
_VARIANT_EMOJI = {"success": "✅", "warning": "⚠️", "error": "❌", "info": "ℹ️"}
_SPARK_LEVELS = "▁▂▃▄▅▆▇█"

_INLINE_UI_TYPES = {"select", "checkboxes", "radio", "date"}
_MODAL_ONLY_UI_TYPES = {"text", "number"}


def _format_value(value, fmt: str | None, currency: str = "$") -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)

    is_whole = num == int(num)
    if fmt == "currency":
        return f"{currency}{num:,.0f}" if is_whole else f"{currency}{num:,.2f}"
    if fmt == "percentage":
        return f"{num:g}%"
    if fmt == "decimal":
        return f"{num:,.2f}"
    if fmt == "compact":
        for suffix, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(num) >= div:
                return f"{num / div:.1f}{suffix}"
        return f"{num:,.0f}"
    # "integer" or unspecified
    return f"{num:,.0f}"


def _prose_to_mrkdwn(text: str) -> str:
    """GFM -> Slack mrkdwn. Slack's mrkdwn is NOT the same syntax as GFM."""
    if not text.strip():
        return ""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                 # **bold** -> *bold*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.M)     # ## Heading -> *Heading*
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.M)              # - item -> • item
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)      # [text](url) -> <url|text>
    return text.strip()


def _error_section(block_type: str, raw: str) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"⚠️ _Couldn't render this {block_type} block_"},
    }


def _metric_block(payload: dict, idx: int) -> list[dict]:
    icon = _METRIC_ICONS.get(payload.get("icon"), "")
    label = payload.get("label", "")
    value = _format_value(payload["value"], payload.get("format"), payload.get("currency", "$"))

    line = f"{icon} *{label}*\n*{value}*".strip()
    delta = payload.get("delta")
    if delta is not None:
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
        delta_str = _format_value(abs(delta), payload.get("format"), payload.get("currency", "$"))
        delta_label = payload.get("deltaLabel", "")
        line += f"   {arrow} {delta_str}{f' {delta_label}' if delta_label else ''}"

    blocks = [{"type": "section", "block_id": f"metric_{idx}", "text": {"type": "mrkdwn", "text": line}}]

    sparkline = payload.get("sparkline")
    if sparkline and len(sparkline) >= 2:
        lo, hi = min(sparkline), max(sparkline)
        span = (hi - lo) or 1
        spark = "".join(
            _SPARK_LEVELS[min(len(_SPARK_LEVELS) - 1, int((v - lo) / span * (len(_SPARK_LEVELS) - 1)))]
            for v in sparkline
        )
        blocks.append({
            "type": "context",
            "block_id": f"metric_{idx}_spark",
            "elements": [{"type": "mrkdwn", "text": f"trend  {spark}"}],
        })
    return blocks


def _stats_block(payload: list[dict], idx: int) -> list[dict]:
    fields = []
    for stat in payload[:10]:
        emoji = _VARIANT_EMOJI.get(stat.get("variant"), "")
        value = _format_value(stat["value"], stat.get("format"), stat.get("currency", "$"))
        prefix = f"{emoji} " if emoji else ""
        fields.append({"type": "mrkdwn", "text": f"{prefix}*{stat.get('label', '')}*\n{value}"})
    return [{"type": "section", "block_id": f"stats_{idx}", "fields": fields}]


def _status_block(payload: dict, idx: int) -> list[dict]:
    emoji = _VARIANT_EMOJI.get(payload.get("variant"), "ℹ️")
    label = payload.get("label", "")
    detail = payload.get("detail", "")
    text = f"{emoji} *{label}*" + (f"\n{detail}" if detail else "")
    return [{"type": "section", "block_id": f"status_{idx}", "text": {"type": "mrkdwn", "text": text}}]


def _bar_rows(data: list[dict], value_key: str, currency: str, sort: bool) -> str:
    rows = list(data)
    if sort:
        rows.sort(key=lambda r: r.get(value_key, 0), reverse=True)
    max_value = max((abs(r.get(value_key, 0)) for r in rows), default=1) or 1
    label_width = max((len(str(r.get("label", ""))) for r in rows), default=0)
    lines = []
    for row in rows:
        value = row.get(value_key, 0)
        bar_width = max(1, round(abs(value) / max_value * 20))
        bar = "█" * bar_width + "░" * (20 - bar_width)
        label = str(row.get("label", "")).ljust(label_width)
        value_str = _format_value(value, "currency", currency) if currency else f"{value:,.0f}"
        lines.append(f"{label}  {bar} {value_str}")
    return "\n".join(lines)


def _chart_block(payload: dict, idx: int) -> list[dict]:
    chart_type = payload.get("type", "bar")
    title = payload.get("title")
    currency = payload.get("currency", "")
    sort = payload.get("sort", True)
    data = payload.get("data", [])
    series = payload.get("series")

    header = f"*{title}*\n" if title else ""

    if chart_type == "donut":
        total = sum(r.get("value", 0) for r in data) or 1
        value_fmt = "currency" if currency else "integer"
        lines = [
            f"{r.get('label', '')} — {_format_value(r.get('value', 0), value_fmt, currency)} "
            f"({r.get('value', 0) / total * 100:.0f}%)"
            for r in data
        ]
        body = "\n".join(lines)
        return [{"type": "section", "block_id": f"chart_{idx}",
                  "text": {"type": "mrkdwn", "text": f"{header}```\n{body}\n```"}}]

    if series:
        # composed/multi-series: one bar block per series, subheaded by series name
        blocks = []
        for s_idx, s in enumerate(series):
            key = s["key"]
            s_currency = s.get("currency", currency)
            body = _bar_rows(data, key, s_currency, sort)
            sub_header = f"*{title} — {s.get('name', key)}*\n" if title else f"*{s.get('name', key)}*\n"
            blocks.append({
                "type": "section",
                "block_id": f"chart_{idx}_{s_idx}",
                "text": {"type": "mrkdwn", "text": f"{sub_header}```\n{body}\n```"},
            })
        return blocks

    # single-series bar/line — same text-bar treatment for both (Slack can't draw a line either)
    body = _bar_rows(data, "value", currency, sort)
    return [{"type": "section", "block_id": f"chart_{idx}",
              "text": {"type": "mrkdwn", "text": f"{header}```\n{body}\n```"}}]


def _ui_option(opt: dict) -> dict:
    return {"text": {"type": "plain_text", "text": opt["label"]}, "value": str(opt["value"])}


def _ui_inline_accessory(field: dict) -> dict | None:
    ftype = field["type"]
    if ftype == "select":
        options = field.get("options", [])
        if options and "group" in options[0]:
            return {
                "type": "static_select",
                "action_id": "ui_field",
                "option_groups": [
                    {"label": {"type": "plain_text", "text": g["group"]},
                     "options": [_ui_option(o) for o in g["options"]]}
                    for g in options
                ],
            }
        return {"type": "static_select", "action_id": "ui_field",
                 "options": [_ui_option(o) for o in options]}
    if ftype == "checkboxes":
        return {"type": "checkboxes", "action_id": "ui_field",
                 "options": [_ui_option(o) for o in field.get("options", [])]}
    if ftype == "radio":
        return {"type": "radio_buttons", "action_id": "ui_field",
                 "options": [_ui_option(o) for o in field.get("options", [])]}
    if ftype == "date":
        return {"type": "datepicker", "action_id": "ui_field"}
    return None


def _ui_blocks(fields: list[dict], idx: int, iid_prefix: str) -> tuple[list[dict], dict]:
    """Returns (blocks, pending_record) — pending_record must be persisted by
    the caller via services/slack_interaction_store.py before the message is sent."""
    iid = f"{iid_prefix}:{idx}"
    needs_modal = any(f["type"] in _MODAL_ONLY_UI_TYPES for f in fields)

    if needs_modal:
        blocks = [{
            "type": "actions",
            "block_id": f"ui|{iid}|__open_modal__",
            "elements": [{
                "type": "button",
                "action_id": "ui_open_modal",
                "text": {"type": "plain_text", "text": "📝 Fill out form"},
                "value": iid,
            }],
        }]
        mode = "modal"
    else:
        blocks = []
        for field in fields:
            accessory = _ui_inline_accessory(field)
            if accessory is None:
                continue
            blocks.append({
                "type": "section",
                "block_id": f"ui|{iid}|{field['id']}",
                "text": {"type": "mrkdwn", "text": f"*{field.get('label', field['id'])}*"},
                "accessory": accessory,
            })
        blocks.append({
            "type": "actions",
            "block_id": f"ui|{iid}|__submit__",
            "elements": [{
                "type": "button", "action_id": "ui_submit", "style": "primary",
                "text": {"type": "plain_text", "text": "Submit"}, "value": iid,
            }],
        })
        mode = "inline"

    pending_record = {"iid": iid, "fields": fields, "mode": mode}
    return blocks, pending_record


def build_modal_view(fields: list[dict], iid: str) -> dict:
    """Builds the Slack `view` payload for views_open when a "Fill out form"
    button is clicked. Shared by routers/slack_interactions.py."""
    view_blocks = []
    for field in fields:
        ftype = field["type"]
        block_id = field["id"]
        label = {"type": "plain_text", "text": field.get("label", field["id"])[:2000]}
        if ftype == "text":
            element = {"type": "plain_text_input", "action_id": "value"}
            if field.get("placeholder"):
                element["placeholder"] = {"type": "plain_text", "text": field["placeholder"]}
        elif ftype == "number":
            element = {"type": "number_input", "action_id": "value", "is_decimal_allowed": True}
            if field.get("min") is not None:
                element["min_value"] = str(field["min"])
            if field.get("max") is not None:
                element["max_value"] = str(field["max"])
        else:
            accessory = _ui_inline_accessory(field)
            if accessory is None:
                continue
            accessory["action_id"] = "value"
            element = accessory
        view_blocks.append({
            "type": "input",
            "block_id": block_id,
            "label": label,
            "element": element,
            "optional": not field.get("required", False),
        })
    return {
        "type": "modal",
        "callback_id": "ui_form_submit",
        "private_metadata": iid,
        "title": {"type": "plain_text", "text": "Birdy AI"},
        "submit": {"type": "plain_text", "text": "Submit"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": view_blocks,
    }


_BUILDERS = {
    "metric": _metric_block,
    "stats": _stats_block,
    "status": _status_block,
    "chart": _chart_block,
}


def _chunk_section_text(block: dict) -> list[dict]:
    """Splits a Section block's text on paragraph boundaries if it exceeds Slack's limit."""
    text = block.get("text", {}).get("text", "")
    if len(text) <= _MAX_SECTION_CHARS:
        return [block]
    parts, current = [], ""
    for para in text.split("\n\n"):
        if len(current) + len(para) + 2 > _MAX_SECTION_CHARS:
            if current:
                parts.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        parts.append(current)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": p}} for p in parts]


def build_blocks_from_reply(reply_text: str, *, iid_prefix: str) -> tuple[list[dict], str, list[dict]]:
    """Returns (blocks, fallback_text, ui_pending).

    ui_pending is a list of {"iid", "fields", "mode"} dicts — one per :::ui
    block found — that the caller must persist via
    services/slack_interaction_store.py before posting the message.
    """
    blocks: list[dict] = []
    ui_pending: list[dict] = []
    prose_parts: list[str] = []
    last_end = 0
    block_idx = 0

    for match in _BLOCK_RE.finditer(reply_text):
        prose = reply_text[last_end:match.start()]
        mrkdwn = _prose_to_mrkdwn(prose)
        if mrkdwn:
            prose_parts.append(mrkdwn)
            section = {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}
            blocks.extend(_chunk_section_text(section))

        block_type, raw_json = match.group(1), match.group(2)
        try:
            payload = json.loads(raw_json)
            if block_type == "ui":
                new_blocks, pending = _ui_blocks(payload, block_idx, iid_prefix)
                ui_pending.append(pending)
            else:
                new_blocks = _BUILDERS[block_type](payload, block_idx)
            blocks.extend(new_blocks)
        except Exception as e:
            logger.warning(f"Failed to render :::{block_type} block: {e}")
            blocks.append(_error_section(block_type, raw_json))

        block_idx += 1
        last_end = match.end()

    trailing_prose = _prose_to_mrkdwn(reply_text[last_end:])
    if trailing_prose:
        prose_parts.append(trailing_prose)
        section = {"type": "section", "text": {"type": "mrkdwn", "text": trailing_prose}}
        blocks.extend(_chunk_section_text(section))

    if len(blocks) > _MAX_BLOCKS:
        logger.warning(f"Reply produced {len(blocks)} blocks, truncating to {_MAX_BLOCKS}")
        blocks = blocks[:_MAX_BLOCKS]

    fallback_text = " ".join(prose_parts).strip() or "Birdy sent an interactive message"
    return blocks, fallback_text, ui_pending
