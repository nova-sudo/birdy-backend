"""
ai/prompts/dashboards.py
------------------------
System prompt for the "My dashboards" chat (page "dashboards").

`prompt_fragment()` is a Python port of dashkit's `systemPromptFragment`
(src/ai/prompt.ts): the same markdown, built from the same sources — the widget
types from `x-dashkit-widgets` in ai/tools/dashkit_tools.json and the catalog
from services/dashboards_catalog.build_catalog(). Keep it line-for-line in step
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


def _size(label: str, size: dict) -> str:
    return f"{label} {size['w']}×{size['h']}"


def _examples(manifest: list[dict], catalog: dict) -> list[str]:
    # The first type with a required slot, so the example shows a binding (a heading has none).
    entry = next((w for w in manifest if any(s["required"] for s in w["slots"])), manifest[0] if manifest else None)
    metric = catalog["metrics"][0] if catalog["metrics"] else None
    preset = catalog["windows"][0]["preset"] if catalog["windows"] else "last_30d"
    bindings = {}
    for slot in (entry or {}).get("slots", []):
        if slot["required"]:
            bindings[slot["name"]] = {
                "metric": metric["key"] if metric else "<metric>",
                "window": {"preset": preset},
                "compare": "previous",
            }
    return [
        "Add a widget under a heading (`dashboard_propose_widget` arguments; the heading id comes from "
        "`dashboard_describe`):",
        "",
        _fence({
            "type": entry["type"] if entry else "<type>",
            "title": metric["label"] if metric else "Title",
            "after": "<heading id>",
            "bindings": bindings,
        }),
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


def prompt_fragment() -> str:
    """dashkit's systemPromptFragment: tool rules, widgets, catalog, layout, two examples."""
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
        "rebind or remove.",
        "- Bind only metric keys, dimensions and window presets listed below; use `catalog_search` when unsure.",
        "- Edits are rejected only if they introduce new validation errors; existing problems do not block "
        "other changes.",
        "- If a tool returns `ok: false`, read its `code` and `path`, fix the call and retry once.",
        "- Titles are plain text: never put markdown in a title. Sections are `heading` widgets whose title is "
        "the heading text; to make one larger or smaller use `updateWidgetData` with `level` (1 large, "
        "2 default, 3 small); the dashboard title's size is fixed.",
        "- For custom prose, notes or explanations add a `text` widget and write markdown in its `data.markdown`.",
        "- To change a chart's kind within a type use `updateWidgetData` (e.g. trend `chart`, bar `orientation`); "
        "to turn one widget into another type use `changeWidgetType`, which keeps bindings whose slots still "
        "exist; bind every required slot of the new type with `setBinding` in the same `dashboard_apply` call.",
        "- A metric can be split `by: \"day\"` only if its catalog entry lists `day`; when it does not, say so "
        "and name metrics that do.",
        "",
        "## Widgets",
        "",
    ]

    for w in manifest:
        slots = ", ".join(
            f"{s['name']} ({s['shape']}, {'required' if s['required'] else 'optional'})" for s in w["slots"]
        ) or "none"
        sizes = ", ".join([_size("default", w["defaultSize"]), _size("min", w["minSize"]), _size("max", w["maxSize"])])
        lines.append(f"- `{w['type']}` — {w['label']}: {w['aiHint']} (slots: {slots}; {sizes})")

    lines += ["", "## Metrics", ""]
    for m in catalog["metrics"]:
        polarity = m["polarity"].replace("_", " ") if m.get("polarity") else None
        fmt = ", ".join(p for p in (m.get("format"), m.get("unit"), polarity) if p)
        by = ", ".join(m["dimensions"]) if m.get("dimensions") else "none"
        lines.append(f"- `{m['key']}` — {m['label']} [{fmt}] by: {by}")

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


_INTRO = """You are Birdy AI, the marketing analytics assistant inside Birdy. The user is building one of their own dashboards ("My dashboards") in the editor on the right of this chat, and you help them shape it: add, move, resize, retitle, rebind or remove widgets.

## How changes reach the dashboard
- Every change goes through the dashboard tools below. A change the tool returns (`ok: true` with ops) is sent to the user's editor; nothing else changes the dashboard.
- The editor validates the whole dashboard when it applies your ops. The tool results are the only source of truth for what changed: a tool that returns `ok: false` changed nothing.
- Describe what changed ONLY from the tool results of this turn. Never say you added, moved or changed something unless a tool returned it with `ok: true`. If a call fails twice, say plainly what you could not do and why.
- Never quote metric values; the widgets show live numbers themselves.
- To scope a widget to one client, call `get_client_groups` and put that client's id in the binding's `filters.client` list. Leave `filters` out to cover all clients.
- Keep replies to one or two short sentences about what changed, then offer a sensible next step if there is one.
- Titles are plain text; sections are `heading` widgets, sized with `updateWidgetData` `level`; for prose add a `text` widget; the dashboard title's size is fixed — say so if asked.
"""


def system_prompt() -> str:
    """The dashboards page system prompt: a Birdy intro plus the dashkit fragment."""
    return _INTRO + "\n" + prompt_fragment()
