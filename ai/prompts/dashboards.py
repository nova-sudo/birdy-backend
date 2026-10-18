"""
ai/prompts/dashboards.py
------------------------
System prompt for the "My dashboards" chat (page "dashboards").

`prompt_fragment()` is a Python port of dashkit's `systemPromptFragment`
(src/ai/prompt.ts): the same markdown, built from the same sources — the widget
types from `x-dashkit-widgets` in ai/tools/dashkit_tools.json and the catalog
from services/dashboards_catalog.build_catalog(). Keep it line-for-line in step
with prompt.ts when dashkit changes it.

`system_prompt()` is called when a session starts (not at import), so a
regenerated tools file or catalog reaches new sessions after a restart.
"""

import json

from ai.tools.dashboard_tools import widget_manifest
from services.dashboards_catalog import build_catalog


def _fence(value) -> str:
    return "\n".join(["```json", json.dumps(value, indent=2, ensure_ascii=False), "```"])


def _size(label: str, span: dict) -> str:
    return f"{label} {span['cols']}×{span['rows']}"


def _examples(manifest: list[dict], catalog: dict) -> list[str]:
    entry = manifest[0] if manifest else None
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
        "Add a widget (`dashboard_propose_widget` arguments):",
        "",
        _fence({
            "type": entry["type"] if entry else "<type>",
            "title": metric["label"] if metric else "Title",
            "bindings": bindings,
        }),
        "",
        "Move a widget to the top of another section and resize it (`dashboard_apply` arguments; "
        "ids come from `dashboard_describe`):",
        "",
        _fence({
            "ops": [
                {"op": "moveWidget", "widgetId": "<widget id>", "toSectionId": "<section id>", "toIndex": 0},
                {"op": "resizeWidget", "widgetId": "<widget id>", "span": {"cols": 6, "rows": 2}},
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
        "- Emit ops, never code, pixel positions, CSS or HTML. Layout is a 12-column grid of spans.",
        "- When editing an existing dashboard, always call `dashboard_describe` first and use the ids it returns.",
        "- Prefer `dashboard_propose_widget` for new widgets; use `dashboard_apply` to move, resize, retitle, "
        "rebind or remove.",
        "- Bind only metric keys, dimensions and window presets listed below; use `catalog_search` when unsure.",
        "- Edits are rejected only if they introduce new validation errors; existing problems do not block "
        "other changes.",
        "- If a tool returns `ok: false`, read its `code` and `path`, fix the call and retry once.",
        "",
        "## Widgets",
        "",
    ]

    for w in manifest:
        slots = ", ".join(
            f"{s['name']} ({s['shape']}, {'required' if s['required'] else 'optional'})" for s in w["slots"]
        ) or "none"
        spans = [_size("default", w["defaultSpan"])]
        if w.get("minSpan") and w.get("maxSpan"):
            spans += [_size("min", w["minSpan"]), _size("max", w["maxSpan"])]
        lines.append(f"- `{w['type']}` — {w['label']}: {w['aiHint']} (slots: {slots}; {', '.join(spans)})")

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
        "- The grid has 12 columns; a span is `{ cols, rows }` with cols 1–12 and rows 1–4.",
        "- Typical widths: kpi 3 columns, charts 6, tables 12.",
        "- Widgets flow left to right; keep each row's spans summing to at most 12.",
        "",
        "## Examples",
        "",
        *_examples(manifest, catalog),
    ]
    return "\n".join(lines)


_INTRO = """You are Birdy AI, the marketing analytics assistant inside Birdy. The user is building one of their own dashboards ("My dashboards") in the editor on the right of this chat, and you help them shape it: add, move, resize, retitle, rebind or remove widgets.

## How changes reach the dashboard
- Every change goes through the dashboard tools below. A change the tool returns (`ok: true` with ops) is applied in the user's editor at once; nothing else changes the dashboard.
- Describe what changed ONLY from the tool results of this turn. Never say you added, moved or changed something unless a tool returned it with `ok: true`. If a call fails twice, say plainly what you could not do and why.
- Never quote metric values; the widgets show live numbers themselves.
- To scope a widget to one client, call `get_client_groups` and put that client's id in the binding's `filters.client` list. Leave `filters` out to cover all clients.
- Keep replies to one or two short sentences about what changed, then offer a sensible next step if there is one.
"""


def system_prompt() -> str:
    """The dashboards page system prompt: a Birdy intro plus the dashkit fragment."""
    return _INTRO + "\n" + prompt_fragment()
