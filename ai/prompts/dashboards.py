"""
ai/prompts/dashboards.py
------------------------
System prompt for the "My dashboards" chat (page "dashboards").

`prompt_fragment()` is a Python port of dashkit's `systemPromptFragment`
(src/ai/prompt.ts): the same markdown, built from the same sources — the widget
types and their `queryRules` from `x-dashkit-widgets` in
ai/tools/dashkit_tools.json and the catalog from
services/dashboards_catalog.build_catalog(). Keep it line-for-line in step
with prompt.ts when dashkit changes it: tests/test_dashkit_tools_json.py asserts
it equals tests/fixtures/dashkit_prompt_fragment.md (dashkit's own output,
copied by its scripts/sync_dashkit_schema.sh) byte for byte.

`system_prompt()` is called when a session starts (not at import), so a
regenerated tools file or catalog reaches new sessions after a restart.
"""

import json

from ai.tools.dashboard_tools import widget_manifest
from services.dashboards_catalog import build_catalog


def _fence(value) -> str:
    return "\n".join(["```json", json.dumps(value, indent=2, ensure_ascii=False), "```"])


def _clamp(n: int, lo: int, hi: int) -> int:
    return min(max(n, lo), hi)


def _size(label: str, size: dict) -> str:
    return f"{label} {size['w']}×{size['h']}"


def _example_query(rules: dict, catalog: dict, steps: int, labelled: bool) -> dict:
    """prompt.ts exampleQuery: a query that fits `rules`, built from the catalog. When the rules call for `over`,
    the metrics are drawn from those the catalog marks `over: true`; with none, `over` is left out."""
    overable = [m for m in catalog["metrics"] if m.get("over") is True]
    with_over = rules["over"] == "required" and len(overable) > 0
    pool = overable if with_over else catalog["metrics"]
    count = _clamp(steps, rules["metrics"]["min"], min(rules["metrics"]["max"], max(len(pool), 1)))
    picked = pool[:count] or [{"key": "<metric>", "label": "<label>"}]
    refs = [{"metric": m["key"], "label": m["label"]} if labelled else {"metric": m["key"]} for m in picked]
    query = {"metrics": refs}
    by = (catalog["dimensions"][0]["key"] if catalog["dimensions"]
          else rules["defaults"].get("by", "<dimension>"))
    if rules["by"] == "required" or (rules.get("exactlyOneOfByOver") and rules["over"] != "required"):
        query["by"] = by
    if with_over:
        query["over"] = rules["defaults"].get("over", "day")
    query["window"] = {"preset": catalog["windows"][0]["preset"] if catalog["windows"] else "last_30d"}
    return query


def _examples(manifest: list[dict], catalog: dict) -> list[str]:
    with_query = [w for w in manifest if w.get("queryRules")]
    # A funnel shows the multi-metric shape best; fall back to the first data type.
    funnel = next((w for w in with_query if w["type"] == "funnel"), None)
    propose = funnel or (with_query[0] if with_query else None)
    # A trend shows `over` and `compare`; fall back to the first data type.
    set_entry = next((w for w in with_query if w["type"] == "trend"), with_query[0] if with_query else None)
    placeholder = {"metrics": [{"metric": "<metric>"}], "window": {"preset": "<preset>"}}
    proposed = _example_query(propose["queryRules"], catalog, 3, True) if propose else placeholder
    set_query = ({**_example_query(set_entry["queryRules"], catalog, 1, False), "compare": "previous"}
                 if set_entry else placeholder)
    return [
        "Add a widget under a heading (`dashboard_propose_widget` arguments; the heading id comes from "
        "`dashboard_describe`)"
        + (". Funnel steps are the metrics in order, each named by its `label`:" if funnel else ":"),
        "",
        _fence({
            "type": propose["type"] if propose else "<type>",
            "title": propose["label"] if propose else "Title",
            "after": "<heading id>",
            "query": proposed,
        }),
        "",
        "Change what a widget shows (`dashboard_apply` arguments; `setQuery` replaces the whole query):",
        "",
        _fence({"ops": [{"op": "setQuery", "widgetId": "<widget id>", "query": set_query}]}),
        "",
        "Move a widget to the top-left corner and resize it (`dashboard_apply` arguments; "
        "ids come from `dashboard_describe`):",
        "",
        _fence({
            "ops": [
                {"op": "moveWidget", "widgetId": "<widget id>", "to": {"x": 0, "y": 0}},
                {"op": "resizeWidget", "widgetId": "<widget id>", "size": {"w": 6, "h": 6}},
            ],
        }),
    ]


def _count_cell(lo: int, hi: int) -> str:
    return f"exactly {lo}" if lo == hi else f"{lo}..{hi}"


def _use_cell(rule: str) -> str:
    return "no" if rule == "forbidden" else rule


def _rules_row(widget_type: str, r: dict) -> str:
    """One row of the per-type query table, rendered from the type's `queryRules` (prompt.ts rulesRow)."""
    notes = []
    if r.get("spark"):
        notes.append("sparkline by default")
    with_by = r.get("maxMetricsWithBy")
    if with_by is not None:
        notes.append(f"{_count_cell(with_by, with_by)} metric{'' if with_by == 1 else 's'} when `by` is set")
    if r.get("exactlyOneOfByOver"):
        notes.append("exactly one of `by` / `over`")
    defaults = " and ".join(
        part for part in (
            f"`by` to {r['defaults']['by']}" if "by" in r["defaults"] else "",
            f"`over` to {r['defaults']['over']}" if "over" in r["defaults"] else "",
        ) if part
    )
    if defaults:
        notes.append(f"type change defaults {defaults}")
    metrics = _count_cell(r["metrics"]["min"], r["metrics"]["max"])
    return f"| `{widget_type}` | {metrics} | {_use_cell(r['by'])} | {_use_cell(r['over'])} | {'; '.join(notes)} |"


def prompt_fragment() -> str:
    """dashkit's systemPromptFragment: tool rules, widgets, query rules, catalog, layout, three examples."""
    manifest = widget_manifest()
    catalog = build_catalog()
    lines = [
        "## Dashboard tools",
        "",
        "- Change dashboards only through the tools `dashboard_describe`, `dashboard_apply`, "
        "`dashboard_propose_widget` and `catalog_search`.",
        "- Emit ops, never code, pixel positions, CSS or HTML. Layout is a 12-column grid of 48px rows.",
        "- When editing an existing dashboard, always call `dashboard_describe` first and use the ids it returns.",
        "- Prefer `dashboard_propose_widget` for new widgets; use `dashboard_apply` to move, resize, retitle, "
        "change the query (`setQuery`, `clearQuery`) or remove.",
        "- Each data widget has one `query`: `metrics` (ordered, each `{ metric, label? }`), an optional `by` "
        "dimension, an optional `over` grain (`day`, `week` or `month`) and a `window`, plus optional `filters`, "
        "`compare: \"previous\"`, `sort` and `limit`. Types missing from the query table below take no query.",
        "- Use only metric keys, dimensions and window presets listed below; use `catalog_search` when unsure.",
        "- Edits are rejected only if they introduce new validation errors; existing problems do not block "
        "other changes.",
        "- If a tool returns `ok: false`, read its `code` and `path`, fix the call and retry once.",
        "- Titles are plain text: never put markdown in a title. Sections are `heading` widgets whose title is "
        "the heading text; to make one larger or smaller use `updateWidgetData` with `level` (1 large, "
        "2 default, 3 small); the dashboard title's size is fixed.",
        "- For custom prose, notes or explanations add a `text` widget and write markdown in its `data.markdown`.",
        "- To change a chart's kind within a type use `updateWidgetData` (e.g. trend `chart`, chart "
        "`orientation`); to turn one widget into another type use `changeWidgetType`, which keeps the query "
        "when it fits the new type and otherwise keeps only the metrics and window and sets `by` or `over` to "
        "the type's default; check the result against the table below.",
        "- Trend is always over time; use chart for clients.",
        "- Funnel steps and table columns are `query.metrics` in order, labelled via `label`.",
        "- A metric can be split `by` a dimension only if its catalog entry lists it, and shown `over` time only "
        "if it is marked `over time`; when it is not, say so and name metrics that are.",
        "- A query that breaks its type's rules fails with `QUERY_SHAPE`, and the message names the rule; fix "
        "the query to fit the table below.",
        "",
        "## Widgets",
        "",
    ]

    for w in manifest:
        sizes = ", ".join([_size("default", w["defaultSize"]), _size("min", w["minSize"]), _size("max", w["maxSize"])])
        lines.append(f"- `{w['type']}` — {w['label']}: {w['aiHint']} ({sizes})")

    rules = [w for w in manifest if w.get("queryRules")]
    if rules:
        lines += [
            "",
            "## Query rules",
            "",
            "| Type | Metrics | by | over | Notes |",
            "|---|---|---|---|---|",
            *(_rules_row(w["type"], w["queryRules"]) for w in rules),
        ]

    lines += ["", "## Metrics", ""]
    for m in catalog["metrics"]:
        polarity = m["polarity"].replace("_", " ") if m.get("polarity") else None
        fmt = ", ".join(p for p in (m.get("format"), m.get("unit"), polarity) if p)
        by = ", ".join(m["dimensions"]) if m.get("dimensions") else "none"
        flags = [f for f in ("over time" if m.get("over") else "",
                             "not additive" if m.get("additive") is False else "") if f]
        lines.append(f"- `{m['key']}` — {m['label']} [{fmt}] by: {by}{''.join(f'; {f}' for f in flags)}")

    lines += ["", "## Dimensions", ""]
    lines += [f"- `{d['key']}` — {d['label']}" for d in catalog["dimensions"]]

    lines += ["", "## Windows", ""]
    lines += [f"- `{w['preset']}` — {w['label']}" for w in catalog["windows"]]

    lines += [
        "",
        "## Layout",
        "",
        "- The grid has 12 columns and rows 48px tall. A size is `{ w, h }` in cells (`w` 1–12 columns, "
        "`h` rows); a position is `{ x, y }` with `x` 0–11 and `y` counted from 0 at the top; `x + w` is at "
        "most 12.",
        "- Sections are `heading` widgets (usually full width, 1 row). To add widgets under a heading, use "
        "`after` (in `dashboard_propose_widget` or `addWidget`). Only the first widget under a heading takes the "
        "heading id as `after`; give each later widget the previous widget's id as `after`, so it goes to that "
        "widget's right when it fits, else on the next row (naming the heading again would put it above the "
        "earlier ones). Without `after`, a new widget starts a new row at the bottom.",
        "- To rearrange, use `moveWidget` (`to: { x, y }`) for one widget or `setLayout` "
        "(`items: [{ widgetId, x, y, w, h }]`) for several at once; resize with `resizeWidget` "
        "(`size: { w, h }`) within the type's min and max. Widgets in the way are pushed down and gaps close "
        "up automatically.",
        "- Typical widths: kpi 3 columns, charts 6, tables 12.",
        "",
        "## Examples",
        "",
        *_examples(manifest, catalog),
    ]
    return "\n".join(lines)


_INTRO = """You are Birdy AI, the marketing analytics assistant inside Birdy. The user is building one of their own dashboards ("My dashboards") in the editor on the right of this chat, and you help them shape it: add, move, resize or retitle widgets, change what they show, or remove them.

## How changes reach the dashboard
- Every change goes through the dashboard tools below. A change the tool returns (`ok: true` with ops) is sent to the user's editor; nothing else changes the dashboard.
- The editor validates the whole dashboard when it applies your ops. The tool results are the only source of truth for what changed: a tool that returns `ok: false` changed nothing.
- Describe what changed ONLY from the tool results of this turn. Never say you added, moved or changed something unless a tool returned it with `ok: true`. If a call fails twice, say plainly what you could not do and why.
- Never quote metric values; the widgets show live numbers themselves.
- To scope a widget to one client, call `get_client_groups` and put that client's id in the query's `filters.client` list. Leave `filters` out to cover all clients.
- Keep replies to one or two short sentences about what changed, then offer a sensible next step if there is one.
- Titles are plain text; sections are `heading` widgets, sized with `updateWidgetData` `level`; for prose add a `text` widget; the dashboard title's size is fixed — say so if asked.
"""


def system_prompt() -> str:
    """The dashboards page system prompt: a Birdy intro plus the dashkit fragment."""
    return _INTRO + "\n" + prompt_fragment()
