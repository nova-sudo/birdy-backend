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

# Slack's native `data_visualization` block (shipped June 2026) — real bar/line/
# area/pie charts with no image generation or upload round-trip. Limits below
# are Slack's own; anything over them gets folded into an "Other" bucket rather
# than silently dropped, or truncated with an ellipsis for text fields.
_VIZ_MAX_LABEL_CHARS = 20
_VIZ_MAX_NAME_CHARS = 20
_VIZ_MAX_TITLE_CHARS = 50
_VIZ_MAX_DATA_POINTS = 20
_VIZ_MAX_PIE_SEGMENTS = 12
_VIZ_MAX_SERIES = 12
# Slack caps an entire MESSAGE at 2 data_visualization blocks — not per-block,
# per-message. Exceeding it makes chat.postMessage reject the whole message
# (invalid_blocks), so this budget is shared across every :::chart in one reply.
_MAX_VIZ_BLOCKS_PER_MESSAGE = 2


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


_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
_MAX_TABLE_ROWS = 100  # Slack's own limit (data rows, excluding header)


def _looks_numeric(value: str) -> bool:
    stripped = value.strip().replace(",", "").rstrip("%")
    for prefix in ("£", "$", "€"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
            break
    try:
        float(stripped)
        return bool(stripped)
    except ValueError:
        return False


def _split_table_row(line: str) -> list[str]:
    line = line.strip()
    inner = line[1:-1] if line.startswith("|") and line.endswith("|") else line
    return [c.strip() for c in inner.split("|")]


def _parse_gfm_table(paragraph: str) -> dict | None:
    """A GFM table (as the prompt instructs the model to emit for dense data)
    has no native mrkdwn equivalent — Slack's real Table block replaces what
    used to be broken raw pipe-text. Returns None if `paragraph` isn't a table."""
    lines = [l for l in paragraph.split("\n") if l.strip()]
    if len(lines) < 2 or not _TABLE_ROW_RE.match(lines[0]) or not _TABLE_SEP_RE.match(lines[1]):
        return None

    header = _split_table_row(lines[0])
    n_cols = len(header)
    data_rows = [_split_table_row(l) for l in lines[2:] if _TABLE_ROW_RE.match(l)][:_MAX_TABLE_ROWS]
    if not data_rows:
        return None

    def cell(value: str) -> dict:
        return {"type": "raw_number" if _looks_numeric(value) else "raw_text", "text": value}

    rows = [[{"type": "raw_text", "text": h} for h in header]]
    for row in data_rows:
        row = (row + [""] * n_cols)[:n_cols]
        rows.append([cell(v) for v in row])

    numeric_cols = [
        all(r[c]["type"] == "raw_number" for r in rows[1:]) if rows[1:] else False
        for c in range(n_cols)
    ]
    column_settings = [{"align": "right" if numeric_cols[c] else "left"} for c in range(n_cols)]
    return {"type": "table", "rows": rows, "column_settings": column_settings}


def _prose_to_blocks(prose: str) -> list[dict]:
    """Converts a prose segment to blocks — GFM tables become native Table
    blocks; everything else becomes chunked mrkdwn Section blocks."""
    blocks: list[dict] = []
    buffer: list[str] = []

    def flush():
        if not buffer:
            return
        mrkdwn = _prose_to_mrkdwn("\n\n".join(buffer))
        if mrkdwn:
            blocks.extend(_chunk_section_text({"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}))
        buffer.clear()

    for paragraph in prose.split("\n\n"):
        table = _parse_gfm_table(paragraph)
        if table:
            flush()
            blocks.append(table)
        else:
            buffer.append(paragraph)
    flush()
    return blocks


def _error_section(block_type: str, raw: str) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": f"⚠️ _Couldn't render this {block_type} block_"},
    }


def _metric_block(payload: dict, idx: int) -> list[dict]:
    icon = _METRIC_ICONS.get(payload.get("icon"), "")
    label = payload.get("label", "")
    value = _format_value(payload["value"], payload.get("format"), payload.get("currency", "$"))

    # Header is Slack's biggest/boldest native text (plain_text only, 150 char
    # cap) — the headline number belongs there, not squeezed into a Section.
    blocks = [{
        "type": "header",
        "block_id": f"metric_{idx}_header",
        "text": {"type": "plain_text", "text": value[:150]},
    }]

    sub_line = f"{icon} *{label}*".strip()
    delta = payload.get("delta")
    if delta is not None:
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
        delta_str = _format_value(abs(delta), payload.get("format"), payload.get("currency", "$"))
        delta_label = payload.get("deltaLabel", "")
        sub_line += f"   {arrow} {delta_str}{f' {delta_label}' if delta_label else ''}"
    if sub_line.strip():
        blocks.append({
            "type": "context",
            "block_id": f"metric_{idx}_label",
            "elements": [{"type": "mrkdwn", "text": sub_line}],
        })

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


def _truncate(value, max_len: int) -> str:
    s = str(value)
    return s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"


def _cap_data_points(rows: list[dict], value_key: str, sort: bool, limit: int) -> list[dict]:
    """Cap to Slack's data-point limit. The remainder is folded into a single
    'Other (N)' bucket rather than silently dropped, so totals stay honest."""
    rows = list(rows)
    if sort:
        rows.sort(key=lambda r: abs(r.get(value_key, 0) or 0), reverse=True)
    if len(rows) <= limit:
        return rows
    kept, rest = rows[: limit - 1], rows[limit - 1:]
    other_total = sum(r.get(value_key, 0) or 0 for r in rest)
    kept.append({"label": f"Other ({len(rest)})", value_key: other_total})
    return kept


def _bar_rows(data: list[dict], value_key: str, currency: str, sort: bool) -> str:
    """Text-bar fallback — used only if native chart construction fails."""
    rows = _cap_data_points(data, value_key, sort, limit=_VIZ_MAX_DATA_POINTS)
    max_value = max((abs(r.get(value_key, 0) or 0) for r in rows), default=1) or 1
    label_width = max((len(str(r.get("label", ""))) for r in rows), default=0)
    lines = []
    for row in rows:
        value = row.get(value_key, 0) or 0
        bar_width = max(1, round(abs(value) / max_value * 20)) if value else 0
        bar = "█" * bar_width + "░" * (20 - bar_width)
        label = str(row.get("label", "")).ljust(label_width)
        value_str = _format_value(value, "currency", currency) if currency else f"{value:,.0f}"
        lines.append(f"{label}  {bar} {value_str}")
    return "\n".join(lines)


def _native_series_chart(
    native_type: str, title: str, rows: list[dict], value_key: str,
    sort: bool, series_name: str, y_label: str | None = None,
) -> dict:
    """A real Slack `data_visualization` block — bar, line, or area."""
    rows = _cap_data_points(rows, value_key, sort, limit=_VIZ_MAX_DATA_POINTS)
    labels = [_truncate(r.get("label", ""), _VIZ_MAX_LABEL_CHARS) for r in rows]
    chart = {
        "type": native_type,
        "series": [{
            "name": _truncate(series_name or "Value", _VIZ_MAX_NAME_CHARS),
            "data": [{"label": l, "value": r.get(value_key, 0) or 0} for l, r in zip(labels, rows)],
        }],
        "axis_config": {"categories": labels},
    }
    if y_label:
        chart["axis_config"]["y_label"] = _truncate(y_label, 50)
    return {
        "type": "data_visualization",
        "title": _truncate(title or "Chart", _VIZ_MAX_TITLE_CHARS),
        "chart": chart,
    }


def _native_pie(title: str, rows: list[dict]) -> dict | None:
    """A real Slack `data_visualization` pie block. Segments must be > 0 —
    zero/negative rows are dropped (a 0% slice isn't meaningful anyway)."""
    positive = [r for r in rows if (r.get("value", 0) or 0) > 0]
    if not positive:
        return None
    positive = _cap_data_points(positive, "value", sort=True, limit=_VIZ_MAX_PIE_SEGMENTS)
    return {
        "type": "data_visualization",
        "title": _truncate(title or "Chart", _VIZ_MAX_TITLE_CHARS),
        "chart": {
            "type": "pie",
            "segments": [
                {"label": _truncate(r.get("label", ""), _VIZ_MAX_LABEL_CHARS), "value": r.get("value", 0)}
                for r in positive
            ],
        },
    }


def _fallback_donut_block(title: str, data: list[dict], currency: str, idx: int) -> dict:
    total = sum(r.get("value", 0) or 0 for r in data) or 1
    value_fmt = "currency" if currency else "integer"
    lines = [
        f"{r.get('label', '')} — {_format_value(r.get('value', 0), value_fmt, currency)} "
        f"({(r.get('value', 0) or 0) / total * 100:.0f}%)"
        for r in data
    ]
    body = "\n".join(lines)
    return {"type": "section", "block_id": f"chart_{idx}",
             "text": {"type": "mrkdwn", "text": f"*{title}*\n```\n{body}\n```"}}


def _fallback_series_block(title: str, data: list[dict], value_key: str, currency: str, sort: bool, block_id: str) -> dict:
    body = _bar_rows(data, value_key, currency, sort)
    return {"type": "section", "block_id": block_id,
             "text": {"type": "mrkdwn", "text": f"*{title}*\n```\n{body}\n```"}}


def _chart_block(payload: dict, idx: int, viz_budget: int) -> tuple[list[dict], int]:
    """Returns (blocks, native_viz_blocks_used).

    Slack caps a single message at _MAX_VIZ_BLOCKS_PER_MESSAGE data_visualization
    blocks total — exceeding it makes chat.postMessage reject the ENTIRE message
    (a real incident: a reply with 3+ charts/series 500'd and the user got no
    reply at all). `viz_budget` is how many more native chart blocks this message
    can still afford; once it hits 0, remaining charts/series degrade to the
    text-bar fallback instead of being silently dropped or crashing the send.
    """
    chart_type = payload.get("type", "bar")
    title = payload.get("title") or "Chart"
    currency = payload.get("currency", "")
    sort = payload.get("sort", True)
    data = payload.get("data", [])
    series = payload.get("series")
    y_label = f"{title.split(' — ')[-1]} ({currency})" if currency else None

    try:
        if chart_type == "donut":
            if viz_budget <= 0:
                return [_fallback_donut_block(title, data, currency, idx)], 0
            block = _native_pie(title, data)
            if block is None:
                return [{"type": "section", "block_id": f"chart_{idx}",
                          "text": {"type": "mrkdwn", "text": f"*{title}*\n_No non-zero data to chart._"}}], 0
            block["block_id"] = f"chart_{idx}"
            return [block], 1

        if series:
            # composed/multi-series: one native chart block per series (Slack's
            # data_visualization block is single-chart-type, so a bar+line combo
            # can't be one block — this mirrors how the frontend renders it anyway).
            blocks, used = [], 0
            for s_idx, s in enumerate(series[:_VIZ_MAX_SERIES]):
                key = s["key"]
                s_name = s.get("name", key)
                s_currency = s.get("currency", currency)
                s_title = f"{title} — {s_name}" if title else s_name
                block_id = f"chart_{idx}_{s_idx}"
                if viz_budget - used > 0:
                    native_type = "line" if s.get("type") == "line" else "bar"
                    s_y_label = f"{s_name} ({s_currency})" if s_currency else None
                    block = _native_series_chart(native_type, s_title, data, key, sort, s_name, s_y_label)
                    block["block_id"] = block_id
                    blocks.append(block)
                    used += 1
                else:
                    blocks.append(_fallback_series_block(s_title, data, key, s_currency, sort, block_id))
            return blocks, used

        if viz_budget <= 0:
            return [_fallback_series_block(title, data, "value", currency, sort, f"chart_{idx}")], 0
        native_type = "line" if chart_type == "line" else "bar"
        block = _native_series_chart(native_type, title, data, "value", sort, title, y_label)
        block["block_id"] = f"chart_{idx}"
        return [block], 1
    except Exception:
        logger.exception(f"Native chart block failed for idx={idx}, falling back to text bars")
        if chart_type == "donut":
            return [_fallback_donut_block(title, data, currency, idx)], 0
        if series:
            return [
                _fallback_series_block(
                    f"{title} — {s.get('name', s['key'])}" if title else s.get("name", s["key"]),
                    data, s["key"], s.get("currency", currency), sort, f"chart_{idx}_{s_idx}",
                )
                for s_idx, s in enumerate(series)
            ], 0
        return [_fallback_series_block(title, data, "value", currency, sort, f"chart_{idx}")], 0


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
    viz_remaining = _MAX_VIZ_BLOCKS_PER_MESSAGE

    for match in _BLOCK_RE.finditer(reply_text):
        prose = reply_text[last_end:match.start()]
        if prose.strip():
            mrkdwn = _prose_to_mrkdwn(prose)
            if mrkdwn:
                prose_parts.append(mrkdwn)
            blocks.extend(_prose_to_blocks(prose))

        block_type, raw_json = match.group(1), match.group(2)
        if blocks:
            blocks.append({"type": "divider"})
        try:
            payload = json.loads(raw_json)
            if block_type == "ui":
                new_blocks, pending = _ui_blocks(payload, block_idx, iid_prefix)
                ui_pending.append(pending)
            elif block_type == "chart":
                new_blocks, viz_used = _chart_block(payload, block_idx, viz_remaining)
                viz_remaining -= viz_used
            else:
                new_blocks = _BUILDERS[block_type](payload, block_idx)
            blocks.extend(new_blocks)
        except Exception as e:
            logger.warning(f"Failed to render :::{block_type} block: {e}")
            blocks.append(_error_section(block_type, raw_json))

        block_idx += 1
        last_end = match.end()

    trailing_prose = reply_text[last_end:]
    if trailing_prose.strip():
        mrkdwn = _prose_to_mrkdwn(trailing_prose)
        if mrkdwn:
            prose_parts.append(mrkdwn)
        blocks.extend(_prose_to_blocks(trailing_prose))

    if len(blocks) > _MAX_BLOCKS:
        logger.warning(f"Reply produced {len(blocks)} blocks, truncating to {_MAX_BLOCKS}")
        blocks = blocks[:_MAX_BLOCKS]

    fallback_text = " ".join(prose_parts).strip() or "Birdy sent an interactive message"
    return blocks, fallback_text, ui_pending
