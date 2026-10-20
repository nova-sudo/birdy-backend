"""
render_ui — inline charts, KPI cards and tables in the Ask Birdy chat.

The model calls this with a structured spec instead of typing `:::chart` JSON
into its prose. The executor validates the spec (numbers are numbers, series
keys exist, sizes stay renderable), turns each component into the canonical
block the web chat (src/lib/chat-ui-parser.js) and the Slack formatter
(services/slack_block_formatter.py) already parse, and hands the model back a
placeholder per component. The orchestrator swaps the placeholders in the
final reply for the blocks, so what is stored in the session and the archive
is the reply exactly as rendered — history replays without a second store.

A spec that fails validation comes back to the model as an error naming the
field, so it can fix and resend rather than the user seeing a broken chart.
"""

import json
import math
import re

RENDER_UI_TOOL_NAME = "render_ui"

CHART_KINDS = ("line", "area", "bar", "combo", "donut")
KINDS = ("kpi", "kpi_row") + CHART_KINDS + ("table",)
FORMATS = ("integer", "currency", "percentage", "decimal", "compact", "multiplier")
POLARITIES = ("higher_is_better", "lower_is_better")

MAX_COMPONENTS_PER_CALL = 4
MAX_COMPONENTS_PER_TURN = 6
MAX_POINTS = 120
MAX_DONUT_SLICES = 12
MAX_SERIES = 4
MAX_KPIS = 4
MAX_TABLE_ROWS = 25
MAX_TABLE_COLS = 6
MAX_SPARKLINE = 60
MAX_TEXT = 80

PLACEHOLDER_RE = re.compile(r"\[\[viz:(\d+)\]\]")


class SpecError(ValueError):
    pass


# ── Value coercion ──────────────────────────────────────────────────────────

_NUMERIC_JUNK = re.compile(r"[,\s£$€%x]")


def _num(value, where: str, allow_none: bool = False):
    """A finite float. Numeric strings ("1,234", "£12.50", "8.4%") are
    accepted because models emit them; anything else is a spec error."""
    if value is None:
        if allow_none:
            return None
        raise SpecError(f"{where} is missing")
    if isinstance(value, bool):
        raise SpecError(f"{where} must be a number, got {value!r}")
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        cleaned = _NUMERIC_JUNK.sub("", value)
        try:
            num = float(cleaned)
        except ValueError:
            raise SpecError(f"{where} must be a number, got {value!r}")
    else:
        raise SpecError(f"{where} must be a number, got {value!r}")
    if not math.isfinite(num):
        raise SpecError(f"{where} must be a finite number")
    return int(num) if num == int(num) and abs(num) < 1e15 else round(num, 4)


def _text(value, where: str, required: bool = False, limit: int = MAX_TEXT) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise SpecError(f"{where} is required")
        return None
    s = str(value).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _fmt(value, where: str) -> str:
    if value is None:
        return "integer"
    if value not in FORMATS:
        raise SpecError(f"{where} must be one of {', '.join(FORMATS)}")
    return value


def _currency(value) -> str | None:
    return _text(value, "currency", limit=4)


def _fmt_for(spec: dict, where: str) -> str:
    # A currency symbol with no format means money, not a bare count.
    if spec.get("format") is None and _currency(spec.get("currency")):
        return "currency"
    return _fmt(spec.get("format"), f"{where}.format")


def _polarity(value, where: str) -> str | None:
    if value is None:
        return None
    if value not in POLARITIES:
        raise SpecError(f"{where} must be one of {', '.join(POLARITIES)}")
    return "lower" if value == "lower_is_better" else "higher"


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ── Components → canonical blocks ───────────────────────────────────────────

def _kpi_fields(spec: dict, where: str) -> dict:
    return _drop_none({
        "label": _text(spec.get("label"), f"{where}.label", required=True),
        "value": _num(spec.get("value"), f"{where}.value"),
        "format": _fmt_for(spec, where),
        "currency": _currency(spec.get("currency")),
        "deltaPct": _num(spec.get("delta_pct"), f"{where}.delta_pct", allow_none=True),
        "deltaLabel": _text(spec.get("delta_label"), f"{where}.delta_label", limit=40),
        "polarity": _polarity(spec.get("polarity"), f"{where}.polarity"),
    })


def _kpi(spec: dict, where: str) -> tuple[str, object]:
    payload = _kpi_fields(spec, where)
    spark = spec.get("sparkline")
    if spark is not None:
        if not isinstance(spark, list):
            raise SpecError(f"{where}.sparkline must be a list of numbers")
        if len(spark) > MAX_SPARKLINE:
            raise SpecError(f"{where}.sparkline has {len(spark)} points; at most {MAX_SPARKLINE}")
        if len(spark) >= 2:
            payload["sparkline"] = [_num(v, f"{where}.sparkline[{i}]") for i, v in enumerate(spark)]
    subtitle = _text(spec.get("subtitle"), f"{where}.subtitle")
    if subtitle:
        payload["subtitle"] = subtitle
    return "metric", payload


def _kpi_row(spec: dict, where: str) -> tuple[str, object]:
    items = spec.get("items")
    if not isinstance(items, list) or not items:
        raise SpecError(f"{where}.items must be a non-empty list of KPIs")
    if len(items) > MAX_KPIS:
        raise SpecError(f"{where}.items has {len(items)} KPIs; at most {MAX_KPIS} per row — split into two rows")
    return "stats", [_kpi_fields(item or {}, f"{where}.items[{i}]") for i, item in enumerate(items)]


def _chart(spec: dict, where: str, kind: str) -> tuple[str, object]:
    data = spec.get("data")
    if not isinstance(data, list) or not data:
        raise SpecError(f"{where}.data must be a non-empty list of rows")
    limit = MAX_DONUT_SLICES if kind == "donut" else MAX_POINTS
    if len(data) > limit:
        raise SpecError(f"{where}.data has {len(data)} rows; at most {limit} for a {kind} chart")

    fmt = _fmt_for(spec, where)
    currency = _currency(spec.get("currency"))
    series_in = spec.get("series")

    if series_in is not None and not isinstance(series_in, list):
        raise SpecError(f"{where}.series must be a list")
    if series_in and kind == "donut":
        raise SpecError(f"{where}: a donut takes one value per row, not series")
    if kind == "combo" and not (series_in and len(series_in) >= 2):
        raise SpecError(f"{where}: a combo chart needs at least two series")
    if series_in and len(series_in) > MAX_SERIES:
        raise SpecError(f"{where}.series has {len(series_in)} entries; at most {MAX_SERIES}")

    base_type = "composed" if kind == "combo" else kind
    payload = _drop_none({
        "type": base_type,
        "title": _text(spec.get("title"), f"{where}.title"),
        "subtitle": _text(spec.get("subtitle"), f"{where}.subtitle", limit=120),
        "format": fmt,
        "currency": currency,
    })

    if not series_in or len(series_in) == 1:
        # Single series: rows of {label, value} — the shape every renderer
        # (web, Slack native charts and the text fallback) reads directly.
        key = (series_in[0] or {}).get("key") if series_in else "value"
        rows = []
        for i, row in enumerate(data):
            if not isinstance(row, dict):
                raise SpecError(f"{where}.data[{i}] must be an object")
            raw = row.get(key, row.get("value"))
            rows.append({
                "label": _text(row.get("label"), f"{where}.data[{i}].label", required=True, limit=40),
                "value": _num(raw, f"{where}.data[{i}].{key}"),
            })
        payload["data"] = rows
        if series_in:
            payload["seriesName"] = _text(series_in[0].get("name"), f"{where}.series[0].name", limit=40)
            s_fmt = series_in[0].get("format")
            if s_fmt:
                payload["format"] = _fmt(s_fmt, f"{where}.series[0].format")
        if kind in ("line", "area"):
            payload["sort"] = False
        elif "sort" in spec:
            payload["sort"] = bool(spec["sort"])
        return "chart", payload

    series = []
    seen = set()
    for i, s in enumerate(series_in):
        if not isinstance(s, dict):
            raise SpecError(f"{where}.series[{i}] must be an object")
        key = _text(s.get("key"), f"{where}.series[{i}].key", required=True, limit=40)
        if key in seen or key == "label":
            raise SpecError(f"{where}.series[{i}].key {key!r} is a duplicate or reserved")
        seen.add(key)
        s_type = s.get("type") or ("bar" if kind == "bar" else "area" if kind == "area" else "line")
        if s_type not in ("line", "bar", "area"):
            raise SpecError(f"{where}.series[{i}].type must be line, bar or area")
        axis = s.get("axis") or ("right" if kind == "combo" and i > 0 else "left")
        if axis not in ("left", "right"):
            raise SpecError(f"{where}.series[{i}].axis must be left or right")
        s_fmt = _fmt(s.get("format") or fmt, f"{where}.series[{i}].format")
        s_currency = _currency(s.get("currency")) or currency
        series.append(_drop_none({
            "key": key,
            "name": _text(s.get("name"), f"{where}.series[{i}].name", limit=40) or key,
            "type": s_type,
            "axis": axis,
            "format": s_fmt,
            # Slack's formatter labels a series axis with its currency only.
            "currency": s_currency if s_fmt == "currency" else None,
        }))

    rows = []
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise SpecError(f"{where}.data[{i}] must be an object")
        out = {"label": _text(row.get("label"), f"{where}.data[{i}].label", required=True, limit=40)}
        for s in series:
            out[s["key"]] = _num(row.get(s["key"]), f"{where}.data[{i}].{s['key']}", allow_none=True)
        rows.append(out)
    if all(all(r[s["key"]] is None for r in rows) for s in series):
        raise SpecError(f"{where}: no row has a value for any series — check the series keys match the data")

    payload["data"] = rows
    payload["series"] = series
    payload["sort"] = False
    return "chart", payload


def _cell(value, fmt: str, currency: str | None) -> str:
    if value is None or value == "":
        return "–"
    if fmt == "text":
        return str(value).replace("|", "\\|").replace("\n", " ")
    try:
        num = float(_num(value, "cell"))
    except SpecError:
        return str(value).replace("|", "\\|").replace("\n", " ")
    cur = currency or "$"
    if fmt == "currency":
        return f"{cur}{num:,.0f}" if num == int(num) and abs(num) >= 100 else f"{cur}{num:,.2f}"
    if fmt == "percentage":
        return f"{num:.1f}%"
    if fmt == "decimal":
        return f"{num:,.2f}"
    if fmt == "multiplier":
        return f"{num:.1f}x"
    if fmt == "compact":
        for suffix, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
            if abs(num) >= div:
                return f"{num / div:.1f}{suffix}"
    return f"{num:,.0f}"


def _table(spec: dict, where: str) -> tuple[str, object]:
    """A GFM table: the web chat styles it, Slack's formatter already turns
    GFM tables into native table blocks — no third block type to keep in sync."""
    cols = spec.get("columns")
    rows = spec.get("rows")
    if not isinstance(cols, list) or not cols:
        raise SpecError(f"{where}.columns must be a non-empty list")
    if len(cols) > MAX_TABLE_COLS:
        raise SpecError(f"{where}.columns has {len(cols)} columns; at most {MAX_TABLE_COLS}")
    if not isinstance(rows, list) or not rows:
        raise SpecError(f"{where}.rows must be a non-empty list")
    if len(rows) > MAX_TABLE_ROWS:
        raise SpecError(f"{where}.rows has {len(rows)} rows; at most {MAX_TABLE_ROWS}")
    currency = _currency(spec.get("currency"))
    parsed = []
    for i, c in enumerate(cols):
        c = c or {}
        key = _text(c.get("key"), f"{where}.columns[{i}].key", required=True, limit=40)
        fmt = c.get("format") or "text"
        if fmt != "text":
            fmt = _fmt(fmt, f"{where}.columns[{i}].format")
        parsed.append((key, _text(c.get("label"), f"{where}.columns[{i}].label", limit=40) or key, fmt))
    head = "| " + " | ".join(label.replace("|", "\\|") for _, label, _ in parsed) + " |"
    rule = "| " + " | ".join("---:" if fmt != "text" else "---" for _, _, fmt in parsed) + " |"
    body = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SpecError(f"{where}.rows[{i}] must be an object")
        body.append("| " + " | ".join(_cell(r.get(k), fmt, currency) for k, _, fmt in parsed) + " |")
    title = _text(spec.get("title"), f"{where}.title")
    md = "\n".join([head, rule, *body])
    return "table", (f"**{title}**\n\n{md}" if title else md)


def build_block(spec: dict, where: str = "component") -> str:
    """One component spec → the block text that goes into the reply."""
    if not isinstance(spec, dict):
        raise SpecError(f"{where} must be an object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise SpecError(f"{where}.kind must be one of {', '.join(KINDS)}")
    if kind == "kpi":
        block_type, payload = _kpi(spec, where)
    elif kind == "kpi_row":
        block_type, payload = _kpi_row(spec, where)
    elif kind == "table":
        return _table(spec, where)[1]
    else:
        block_type, payload = _chart(spec, where, kind)
    # One line of JSON between the fences: Slack's parser matches
    # `:::type\n(.*?)\n:::`, the web parser is brace-balanced either way.
    return f":::{block_type}\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n:::"


# ── Executor ────────────────────────────────────────────────────────────────

async def render_ui(db=None, user_id=None, components=None, ui_blocks_sink=None, **kwargs):
    if ui_blocks_sink is None:
        # Only the chat orchestrator can place a block in a reply.
        return {"error": "render_ui is only available in the chat."}
    if not isinstance(components, list) or not components:
        return {"error": "components must be a non-empty list of component specs."}
    if len(components) > MAX_COMPONENTS_PER_CALL:
        return {"error": f"At most {MAX_COMPONENTS_PER_CALL} components per call."}
    if len(ui_blocks_sink) + len(components) > MAX_COMPONENTS_PER_TURN:
        return {"error": f"At most {MAX_COMPONENTS_PER_TURN} visuals per answer. Pick the ones that matter."}

    try:
        blocks = [build_block(c, f"components[{i}]") for i, c in enumerate(components)]
    except SpecError as e:
        return {"error": f"Invalid spec: {e}. Fix it and call render_ui again. Nothing was rendered."}

    placeholders = []
    for block in blocks:
        ui_blocks_sink.append(block)
        placeholders.append(f"[[viz:{len(ui_blocks_sink)}]]")
    return {
        "ok": True,
        "placeholders": placeholders,
        "note": ("Rendered. Put each placeholder on its own line in your reply where the visual "
                 "should appear, with a one-line lead-in. Don't repeat the visual's numbers as a "
                 "list or table."),
    }


def materialize(reply: str, blocks: list[str]) -> str:
    """Swap [[viz:N]] placeholders in the final reply for their blocks.

    A block the model forgot to place goes after the reply's first paragraph
    (the lead-in sentence), so a rendered chart is never silently lost; an
    unknown placeholder is removed rather than shown as raw text.
    """
    if not blocks:
        return PLACEHOLDER_RE.sub("", reply)
    used = set()

    def swap(m):
        n = int(m.group(1))
        if 1 <= n <= len(blocks) and n not in used:
            used.add(n)
            return f"\n\n{blocks[n - 1]}\n\n"
        return ""

    out = PLACEHOLDER_RE.sub(swap, reply)
    missing = [blocks[i] for i in range(len(blocks)) if (i + 1) not in used]
    if missing:
        extra = "\n\n".join(missing)
        parts = out.strip().split("\n\n", 1)
        out = f"{parts[0]}\n\n{extra}" + (f"\n\n{parts[1]}" if len(parts) > 1 else "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# ── Schema ──────────────────────────────────────────────────────────────────

_FORMAT_PROP = {"type": "string", "enum": list(FORMATS),
                "description": "How to print values. Default integer."}
_KPI_PROPS = {
    "label": {"type": "string", "description": "What the number is, e.g. 'Cost per lead'."},
    "value": {"type": "number"},
    "format": _FORMAT_PROP,
    "currency": {"type": "string", "description": "Currency symbol from the data, e.g. '£'."},
    "delta_pct": {"type": "number", "description": "Percent change vs the comparison period: -12.5 means down 12.5%. Omit if you didn't fetch a comparison."},
    "delta_label": {"type": "string", "description": "What the delta compares to, e.g. 'vs previous 7 days'."},
    "polarity": {"type": "string", "enum": list(POLARITIES), "description": "lower_is_better for costs (CPL, CPA, CPC, cost per booking); default higher_is_better."},
}

RENDER_UI_SCHEMA = {
    "type": "object",
    "properties": {
        "components": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_COMPONENTS_PER_CALL,
            "description": "Visuals to render, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS), "description": (
                        "kpi: one headline number card. kpi_row: 2-4 related numbers. "
                        "line/area: a metric over time. bar: compare clients/campaigns/periods. "
                        "combo: two metrics on one time axis (e.g. spend bars + CPL line on a right axis). "
                        "donut: share of a total (max 12 slices). table: a compact ranked table.")},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string", "description": "Period and scope, e.g. 'Aura · last 30 days'."},
                    **_KPI_PROPS,
                    "sparkline": {"type": "array", "items": {"type": "number"},
                                  "description": "kpi only: the metric's recent values, oldest first."},
                    "items": {"type": "array", "maxItems": MAX_KPIS,
                              "items": {"type": "object", "properties": _KPI_PROPS, "required": ["label", "value"]},
                              "description": "kpi_row only."},
                    "data": {"type": "array", "items": {"type": "object"}, "description": (
                        "Charts: rows like {\"label\": \"Oct 3\", \"value\": 120} for one series, or "
                        "{\"label\": \"Oct 3\", \"spend\": 120, \"cpl\": 14.2} with a series entry per key. "
                        "Time series oldest first.")},
                    "series": {"type": "array", "maxItems": MAX_SERIES, "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["line", "bar", "area"]},
                            "axis": {"type": "string", "enum": ["left", "right"],
                                     "description": "Put a metric on a very different scale on the right."},
                            "format": _FORMAT_PROP,
                            "currency": {"type": "string"},
                        },
                        "required": ["key", "name"],
                    }, "description": "Charts with more than one metric."},
                    "columns": {"type": "array", "maxItems": MAX_TABLE_COLS, "items": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}, "label": {"type": "string"},
                                       "format": {"type": "string", "enum": ["text", *FORMATS]}},
                        "required": ["key", "label"],
                    }, "description": "table only."},
                    "rows": {"type": "array", "maxItems": MAX_TABLE_ROWS, "items": {"type": "object"},
                             "description": "table only: objects keyed by column key."},
                },
                "required": ["kind"],
            },
        },
    },
    "required": ["components"],
}

RENDER_UI_DESCRIPTION = (
    "Render charts, KPI cards and tables inline in your answer from data you already fetched with "
    "other tools this conversation. Returns a placeholder per component; put each placeholder on its "
    "own line in your reply where it should appear. Every number must come from a tool result."
)


def register_render_ui_tools(tool_registry=None):
    if tool_registry is None:
        from ai.tools.registry import registry as tool_registry
    tool_registry.register(
        name=RENDER_UI_TOOL_NAME,
        description=RENDER_UI_DESCRIPTION,
        parameters=RENDER_UI_SCHEMA,
        executor=render_ui,
    )
