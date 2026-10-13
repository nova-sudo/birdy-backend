"""
services/report_blocks.py
-------------------------
The Slack report as a real Block Kit message.

The user (or the report copilot) composes the report from any Block Kit block
a channel message accepts — header, section (text, fields, accessory), divider,
image, context, actions, rich_text, markdown, table, data_table,
data_visualization (native line / bar / area / pie charts), card and carousel —
and Birdy fills it with live numbers at send time in two ways:

  1. {{placeholders}} inside any text, anywhere in any block
     (services/report_template.py has the grammar: {{spend}},
     {{cpl client="Glow" show=vs_target}}, {{ranking ...}}, {{birdy_take}} ...).

  2. A `birdy_data` binding on a data_visualization, table, data_table or
     carousel block, which Birdy expands into the block's chart series, rows
     or cards from the caches:

       {"type": "data_visualization", "title": "Spend by client",
        "birdy_data": {"chart": "bar", "by": "client", "metrics": ["spend", "leads"]}}

       {"type": "data_visualization", "title": "Daily spend",
        "birdy_data": {"chart": "line", "by": "day", "metrics": ["spend"], "days": 14}}

       {"type": "data_table", "caption": "Clients",
        "birdy_data": {"metrics": ["spend", "leads", "cpl"], "sort": "spend", "total_row": true}}

       {"type": "carousel",
        "birdy_data": {"metrics": ["spend", "leads"], "limit": 5}}

Numbers only ever come from the caches; the model never writes one. The only
model-written text is {{birdy_take}}, given the rendered report and nothing else.

Stored at users.integrations.slack_bot.brief:
    {"version": 4, "frequency": "daily" | "weekly", "day": ..., "time": ..., "blocks": [...]}

Every earlier shape (checkboxes, v2 sections, v3 free-form text) converts on read.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services import report_template as core
from services.report_template import (  # re-exported: the report's public API lives here
    CATALOG, FREQUENCIES, LEGACY_ITEM_KEYS, PERIODS, PLACEHOLDER_GUIDE, SECTION_PERIODS, WEEKDAYS,
    TemplateError, catalog_for, periods_for_ui, previous_for, sources_connected,
)

VERSION = 4
MAX_BLOCKS = 50
MAX_CHARTS = 2
MAX_MARKDOWN = 12000
MAX_TABLE_CHARS = 10000
MAX_DATA_TABLE_CHARS = 20000

MESSAGE_BLOCKS = ("header", "section", "divider", "image", "context", "actions", "rich_text",
                  "markdown", "table", "data_table", "data_visualization", "card", "carousel")
NOT_IN_MESSAGES = {
    "alert": "alert blocks only work in modals, not channel messages — use a section or card instead",
    "input": "input blocks only work in modals",
}
DATA_BLOCKS = ("data_visualization", "table", "data_table", "carousel")
CHART_KINDS = ("line", "bar", "area", "pie")

# Daily series the caches hold per client, for charts plotted by day.
DAILY_METRICS = {
    "spend":         {"label": "Ad spend", "field": "meta_daily_spend", "key": "spend", "money": True},
    "impressions":   {"label": "Impressions", "field": "meta_daily_spend", "key": "impressions"},
    "clicks":        {"label": "Clicks", "field": "meta_daily_spend", "key": "clicks"},
    "crm_leads":     {"label": "CRM leads", "field": "ghl_daily_leads", "key": "leads"},
    "deals_won":     {"label": "Won (by lead day)", "field": "ghl_daily_leads", "key": "won"},
    "deals_lost":    {"label": "Lost (by lead day)", "field": "ghl_daily_leads", "key": "lost"},
    "calls":         {"label": "Calls", "field": "hp_daily_calls", "key": "calls"},
    "inbound_calls": {"label": "Inbound calls", "field": "hp_daily_calls", "key": "inbound"},
    "talk_time":     {"label": "Talk time (min)", "field": "hp_daily_calls", "key": "talk_min"},
}


# ── Shape ─────────────────────────────────────────────────────────────────────


def _md(text: str) -> dict:
    return {"type": "mrkdwn", "text": text}


def default_template(frequency: str = "daily", time: str = "9:00 AM", day: str | None = None) -> dict:
    return {
        "version": VERSION, "frequency": frequency, "time": time,
        "day": (day or "Monday") if frequency == "weekly" else None,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Birdy brief · {{period}}"}},
            {"type": "section", "fields": [
                _md("*Ad spend*\n{{spend}} {{spend show=change}}"),
                _md("*Leads*\n{{leads}} {{leads show=change}}"),
                _md("*Cost per lead*\n{{cpl}} {{cpl show=change}}"),
                _md("*Close rate*\n{{ghl_conversion}}"),
            ]},
            {"type": "data_visualization", "title": "Spend and leads by client",
             "birdy_data": {"chart": "bar", "by": "client", "metrics": ["spend", "leads"], "limit": 8}},
            {"type": "context", "elements": [_md("🔔 Alerts: {{alerts}}")]},
        ],
    }


def normalize(brief: dict | None) -> dict | None:
    """Any stored brief as a v4 template, or None when there is none."""
    if not brief:
        return None
    if brief.get("version") == VERSION:
        out = {k: brief.get(k) for k in ("version", "frequency", "time", "day", "blocks")}
        if brief.get("updated_at"):
            out["updated_at"] = brief["updated_at"]
        out["blocks"] = brief.get("blocks") or []
        return out
    v3 = core.normalize(brief)  # checkboxes and v2 become a v3 body first
    out = {"version": VERSION, "frequency": v3["frequency"], "time": v3["time"], "day": v3["day"],
           "blocks": core.to_blocks(v3.get("body") or "")}
    if v3.get("updated_at"):
        out["updated_at"] = v3["updated_at"]
    return out


def has_content(template: dict | None) -> bool:
    return bool(template and template.get("frequency") in FREQUENCIES and template.get("blocks"))


# ── Walking blocks ────────────────────────────────────────────────────────────


def _strings(node, path="blocks"):
    """Every string in a block tree, with a readable path, skipping birdy_data."""
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, dict):
        for k, v in node.items():
            if k != "birdy_data":
                yield from _strings(v, f"{path}.{k}")


def _map_strings(node, fn, parent_key=None, mrkdwn=False):
    """A copy of the tree with fn(string, is_mrkdwn) applied to every string value."""
    if isinstance(node, str):
        return fn(node, mrkdwn)
    if isinstance(node, list):
        return [_map_strings(v, fn, parent_key, mrkdwn) for v in node]
    if isinstance(node, dict):
        is_md = node.get("type") == "mrkdwn"
        return {k: (v if k in ("type", "birdy_data", "action_id", "block_id", "url", "image_url", "style")
                    else _map_strings(v, fn, k, is_md and k == "text"))
                for k, v in node.items()}
    return node


def all_tokens(blocks: list) -> list[dict]:
    return [tok for _, s in _strings(blocks) for tok in core.parse_tokens(s)]


def has_take(template: dict) -> dict | None:
    return next((t for t in all_tokens(template.get("blocks") or []) if t["name"] == "birdy_take"), None)


# ── Validation: Slack's rules for channel messages ────────────────────────────


def _text_obj(v, *, plain_only=False, mrkdwn_default=True):
    """Accept a text object or a bare string (coerced)."""
    if isinstance(v, str):
        if plain_only:  # plain text shows formatting marks literally
            return {"type": "plain_text", "text": re.sub(r"(\*\*|__|[*_~`])(.+?)\1", r"\2", v)}
        return {"type": "mrkdwn" if mrkdwn_default else "plain_text", "text": v}
    if isinstance(v, dict) and v.get("type") in ("plain_text", "mrkdwn") and isinstance(v.get("text"), str):
        if plain_only and v["type"] != "plain_text":
            return {"type": "plain_text", "text": re.sub(r"[*_~`]", "", v["text"])}
        out = {"type": v["type"], "text": v["text"]}
        if v["type"] == "plain_text" and isinstance(v.get("emoji"), bool):
            out["emoji"] = v["emoji"]
        return out
    return None


def _button(el, where, errs):
    if not isinstance(el, dict) or el.get("type") != "button":
        errs.append(f"{where}: only link buttons are supported")
        return None
    text = _text_obj(el.get("text"), plain_only=True)
    url = el.get("url")
    if not text or not text["text"].strip():
        errs.append(f"{where}: a button needs text")
        return None
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        errs.append(f"{where}: buttons in reports must open a link (url) — Birdy doesn't handle button clicks")
        return None
    out = {"type": "button", "text": {**text, "text": text["text"][:75]}, "url": url[:3000],
           "action_id": el.get("action_id") or f"link_{uuid.uuid4().hex[:8]}"}
    if el.get("style") in ("primary", "danger"):
        out["style"] = el["style"]
    return out


def _image_el(el, where, errs):
    if not isinstance(el, dict) or el.get("type") != "image":
        errs.append(f"{where}: must be an image element")
        return None
    if not isinstance(el.get("image_url"), str) or not re.match(r"^https://", el["image_url"]):
        errs.append(f"{where}: image_url must be an https URL")
        return None
    return {"type": "image", "image_url": el["image_url"][:3000], "alt_text": str(el.get("alt_text") or "image")[:2000]}


def _check_birdy_data(kind, bd, where, errs, clients):
    if not isinstance(bd, dict):
        errs.append(f"{where}.birdy_data: must be an object")
        return None
    out = {}
    if kind == "data_visualization":
        chart = bd.get("chart")
        if chart not in CHART_KINDS:
            errs.append(f"{where}.birdy_data.chart: one of {', '.join(CHART_KINDS)}")
            return None
        by = bd.get("by") or "client"
        if by not in ("client", "day"):
            errs.append(f"{where}.birdy_data.by: 'client' or 'day'")
            return None
        catalog = DAILY_METRICS if by == "day" else CATALOG
        metrics = bd.get("metrics") or ([bd["metric"]] if bd.get("metric") else [])
        if not metrics:
            errs.append(f"{where}.birdy_data.metrics: list the metrics to plot")
            return None
        bad = [m for m in metrics if m not in catalog]
        if bad:
            names = ", ".join(catalog)
            errs.append(f"{where}.birdy_data.metrics: unknown {bad} — by={by} can plot {names}")
            return None
        if chart == "pie" and len(metrics) != 1:
            errs.append(f"{where}.birdy_data: a pie chart shows exactly one metric")
            return None
        split = bd.get("split")
        if split not in (None, "client"):
            errs.append(f"{where}.birdy_data.split: only 'client'")
            return None
        if split and (by != "day" or len(metrics) != 1):
            errs.append(f"{where}.birdy_data.split='client' needs by='day' and exactly one metric")
            return None
        if len(metrics) > 12:
            errs.append(f"{where}.birdy_data.metrics: Slack charts take at most 12 series")
            return None
        out.update(chart=chart, by=by, metrics=list(dict.fromkeys(metrics)))
        if split:
            out["split"] = split
        if by == "day":
            try:
                days = int(bd.get("days") or 7)
            except (TypeError, ValueError):
                days = 7
            out["days"] = max(2, min(20, days))
        out["limit"] = max(1, min(20 if chart != "pie" else 12, int(bd.get("limit") or (12 if chart == "pie" else 10))))
    else:
        metrics = bd.get("metrics") or []
        cap = {"table": 19, "data_table": 19, "carousel": 3}[kind]
        if not metrics or not isinstance(metrics, list):
            errs.append(f"{where}.birdy_data.metrics: list the metrics to show")
            return None
        bad = [m for m in metrics if m not in CATALOG]
        if bad:
            errs.append(f"{where}.birdy_data.metrics: unknown {bad}; use list_report_metrics")
            return None
        if len(metrics) > cap:
            errs.append(f"{where}.birdy_data.metrics: at most {cap}")
            return None
        metrics = list(dict.fromkeys(metrics))
        sort = bd.get("sort") if bd.get("sort") in metrics else metrics[0]
        row_cap = {"table": 99, "data_table": 200, "carousel": 10}[kind]
        out.update(metrics=metrics, sort=sort, order="worst" if bd.get("order") == "worst" else "best",
                   limit=max(1, min(row_cap, int(bd.get("limit") or min(row_cap, 10)))))
        if kind != "carousel" and bd.get("total_row"):
            out["total_row"] = True
    if bd.get("period"):
        if bd["period"] not in SECTION_PERIODS or out.get("by") == "day":
            errs.append(f"{where}.birdy_data.period: one of {', '.join(SECTION_PERIODS)} (not for by='day')")
            return None
        out["period"] = bd["period"]
    refs = bd.get("clients")
    if refs and refs != "all":
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split(",") if r.strip()]
        ids = []
        for r in refs:
            cid = core._resolve_client(r, clients) if clients is not None else r
            if not cid:
                errs.append(f"{where}.birdy_data.clients: no client called {r!r}")
                return None
            ids.append(cid)
        out["clients"] = ids
    return out


def _check_block(b, i, errs, clients, counters):
    where = f"block {i + 1}"
    if not isinstance(b, dict):
        errs.append(f"{where}: must be an object")
        return None
    kind = b.get("type")
    if kind in NOT_IN_MESSAGES:
        errs.append(f"{where}: {NOT_IN_MESSAGES[kind]}")
        return None
    if kind not in MESSAGE_BLOCKS:
        errs.append(f"{where}: type {kind!r} isn't available in Slack channel messages "
                    f"(use one of {', '.join(MESSAGE_BLOCKS)})")
        return None
    out = {"type": kind}

    if kind == "header":
        t = _text_obj(b.get("text"), plain_only=True)
        if not t or not t["text"].strip():
            errs.append(f"{where}: header needs text")
            return None
        out["text"] = {**t, "text": t["text"]}  # 150-char cap applied after placeholders fill

    elif kind == "section":
        t = _text_obj(b.get("text")) if b.get("text") is not None else None
        fields = b.get("fields")
        if t:
            out["text"] = t
        if fields is not None:
            if not isinstance(fields, list) or not 1 <= len(fields) <= 10:
                errs.append(f"{where}: fields must be 1-10 text items")
                return None
            out["fields"] = [_text_obj(f) for f in fields]
            if any(f is None for f in out["fields"]):
                errs.append(f"{where}: every field must be text")
                return None
        if not t and fields is None:
            errs.append(f"{where}: a section needs text or fields")
            return None
        acc = b.get("accessory")
        if acc is not None:
            el = _image_el(acc, f"{where}.accessory", errs) if acc.get("type") == "image" else \
                _button(acc, f"{where}.accessory", errs)
            if el is None:
                return None
            out["accessory"] = el
        if isinstance(b.get("expand"), bool):
            out["expand"] = b["expand"]

    elif kind == "divider":
        pass

    elif kind == "image":
        el = _image_el({**b, "type": "image"}, where, errs)
        if el is None:
            return None
        out.update(image_url=el["image_url"], alt_text=el["alt_text"])
        title = _text_obj(b.get("title"), plain_only=True) if b.get("title") else None
        if title:
            out["title"] = title

    elif kind == "context":
        els = b.get("elements")
        if not isinstance(els, list) or not 1 <= len(els) <= 10:
            errs.append(f"{where}: context needs 1-10 elements")
            return None
        out["elements"] = []
        for j, el in enumerate(els):
            if isinstance(el, dict) and el.get("type") == "image":
                e = _image_el(el, f"{where}.elements[{j}]", errs)
            else:
                e = _text_obj(el)
                if e is None:
                    errs.append(f"{where}.elements[{j}]: must be text or an image")
            if e is None:
                return None
            out["elements"].append(e)

    elif kind == "actions":
        els = b.get("elements")
        if not isinstance(els, list) or not 1 <= len(els) <= 25:
            errs.append(f"{where}: actions needs 1-25 buttons")
            return None
        out["elements"] = [_button(el, f"{where}.elements[{j}]", errs) for j, el in enumerate(els)]
        if any(e is None for e in out["elements"]):
            return None

    elif kind == "rich_text":
        els = b.get("elements")
        ok = {"rich_text_section", "rich_text_list", "rich_text_preformatted", "rich_text_quote"}
        if not isinstance(els, list) or not els or any(not isinstance(e, dict) or e.get("type") not in ok for e in els):
            errs.append(f"{where}: rich_text elements must be rich_text_section / list / preformatted / quote")
            return None
        out["elements"] = els

    elif kind == "markdown":
        if not isinstance(b.get("text"), str) or not b["text"].strip():
            errs.append(f"{where}: markdown block needs text")
            return None
        out["text"] = b["text"]
        counters["markdown"] += len(b["text"])

    elif kind == "data_visualization":
        counters["charts"] += 1
        title = b.get("title")
        if isinstance(title, dict):
            title = title.get("text")
        if not isinstance(title, str) or not title.strip():
            errs.append(f"{where}: a chart needs a title")
            return None
        out["title"] = title
        if b.get("birdy_data") is not None:
            bd = _check_birdy_data(kind, b["birdy_data"], where, errs, clients)
            if bd is None:
                return None
            out["birdy_data"] = bd
        elif isinstance(b.get("chart"), dict) and b["chart"].get("type") in CHART_KINDS:
            out["chart"] = b["chart"]  # a static chart, written by hand: Slack validates its shape
        else:
            errs.append(f"{where}: a chart needs birdy_data (live) or a chart object")
            return None

    elif kind in ("table", "data_table"):
        if kind == "data_table":
            caption = b.get("caption")
            if not isinstance(caption, str) or not caption.strip():
                errs.append(f"{where}: a data_table needs a caption")
                return None
            out["caption"] = caption[:150]
            if b.get("page_size") is not None:
                out["page_size"] = max(1, min(100, int(b["page_size"])))
        if isinstance(b.get("column_settings"), list):
            out["column_settings"] = b["column_settings"][:20]
        if b.get("birdy_data") is not None:
            bd = _check_birdy_data(kind, b["birdy_data"], where, errs, clients)
            if bd is None:
                return None
            out["birdy_data"] = bd
        elif isinstance(b.get("rows"), list) and b["rows"]:
            rows = b["rows"]
            if len(rows) > (100 if kind == "table" else 201) or any(not isinstance(r, list) or len(r) > 20 for r in rows):
                errs.append(f"{where}: too many rows or columns for a {kind}")
                return None
            out["rows"] = rows
        else:
            errs.append(f"{where}: a {kind} needs birdy_data (live) or rows")
            return None

    elif kind == "card":
        for key, limit in (("title", 150), ("subtitle", 150), ("body", 200), ("subtext", 200)):
            if b.get(key) is not None:
                t = _text_obj(b[key])
                if t is None:
                    errs.append(f"{where}.{key}: must be text")
                    return None
                out[key] = t
        for key in ("hero_image", "icon"):
            if b.get(key) is not None:
                el = _image_el(b[key], f"{where}.{key}", errs)
                if el is None:
                    return None
                out[key] = el
        if b.get("actions"):
            acts = b["actions"] if isinstance(b["actions"], list) else []
            if len(acts) > 3:
                errs.append(f"{where}: a card takes at most 3 buttons")
                return None
            out["actions"] = [_button(a, f"{where}.actions[{j}]", errs) for j, a in enumerate(acts)]
            if any(a is None for a in out["actions"]):
                return None
        if not any(k in out for k in ("hero_image", "title", "actions", "body")):
            errs.append(f"{where}: a card needs a title, body, image or buttons")
            return None

    elif kind == "carousel":
        if b.get("birdy_data") is not None:
            bd = _check_birdy_data(kind, b["birdy_data"], where, errs, clients)
            if bd is None:
                return None
            out["birdy_data"] = bd
        else:
            cards = b.get("elements")
            if not isinstance(cards, list) or not 1 <= len(cards) <= 10:
                errs.append(f"{where}: a carousel needs 1-10 cards (or birdy_data)")
                return None
            sub = [_check_block({**c, "type": "card"}, i, errs, clients, counters) for c in cards]
            if any(c is None for c in sub):
                return None
            out["elements"] = sub
    return out


def fit_to_slack(raw: dict) -> tuple[dict, list[str]]:
    """
    Adjust a template that asks for more than Slack allows, instead of refusing it.
    Charts past Slack's two become a data table (when they compare clients) or
    are left out (day-by-day trends have no table form); blocks past 50 are cut.
    Returns the adjusted template and a note for each change, for the copilot to
    pass on. Anything else wrong is left for `validate` to report.
    """
    notes: list[str] = []
    blocks = raw.get("blocks")
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            return raw, notes
    if not isinstance(blocks, list):
        return raw, notes
    out, charts = [], 0
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "data_table" and not b.get("caption"):
            b = {**b, "caption": "Clients"}
        if isinstance(b, dict) and b.get("type") == "carousel" and isinstance(b.get("birdy_data"), dict):
            metrics = b["birdy_data"].get("metrics") or []
            if isinstance(metrics, list) and len(metrics) > 3:
                b = {**b, "birdy_data": {**b["birdy_data"], "metrics": metrics[:3]}}
                notes.append("Client cards fit 3 numbers each, so they show the first three you picked.")
        if isinstance(b, dict) and b.get("type") == "data_visualization":
            charts += 1
            if charts > MAX_CHARTS:
                bd = b.get("birdy_data") or {}
                title = b.get("title") if isinstance(b.get("title"), str) else "Chart"
                if bd.get("by", "client") == "client" and bd.get("metrics"):
                    out.append({"type": "data_table", "caption": title[:150],
                                "birdy_data": {"metrics": bd["metrics"][:19], "limit": bd.get("limit") or 10}})
                    notes.append(f"Slack allows {MAX_CHARTS} charts per message, so “{title}” became a sortable table.")
                else:
                    notes.append(f"Slack allows {MAX_CHARTS} charts per message, so “{title}” was left out.")
                continue
        out.append(b)
    if len(out) > MAX_BLOCKS:
        notes.append(f"Slack allows {MAX_BLOCKS} blocks per message, so the last {len(out) - MAX_BLOCKS} were left out.")
        out = out[:MAX_BLOCKS]
    return {**raw, "blocks": out}, notes


def validate(raw: dict, clients: dict[str, str] | None = None) -> dict:
    """
    A clean v4 template, or TemplateError listing everything wrong with it.
    `clients` maps the user's client ids to names (None skips that check).
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        raise TemplateError(["template must be an object"])
    frequency = raw.get("frequency")
    if frequency not in FREQUENCIES:
        errors.append("frequency must be 'daily' or 'weekly'")
    day = raw.get("day")
    if frequency == "weekly":
        day = (day or "Monday").strip().title() if isinstance(day, str) else "Monday"
        if day not in WEEKDAYS:
            errors.append(f"day must be one of {', '.join(WEEKDAYS)}")
    else:
        day = None

    blocks_in = raw.get("blocks")
    if isinstance(blocks_in, str):  # the model sometimes sends the array as a JSON string
        try:
            blocks_in = json.loads(blocks_in)
        except ValueError:
            errors.append("blocks must be a JSON array of Block Kit blocks")
            blocks_in = []
    if (not isinstance(blocks_in, list) or not blocks_in) and isinstance(raw.get("body"), str):
        blocks_in = core.to_blocks(core.slackify(raw["body"]))  # free-form text still works
    if not isinstance(blocks_in, list) or not blocks_in:
        errors.append("blocks must be a non-empty list")
        blocks_in = []
    if len(blocks_in) > MAX_BLOCKS:
        errors.append(f"{len(blocks_in)} blocks; Slack allows {MAX_BLOCKS} per message")

    counters = {"charts": 0, "markdown": 0}
    blocks = []
    for i, b in enumerate(blocks_in[:MAX_BLOCKS]):
        clean = _check_block(b, i, errors, clients, counters)
        if clean is not None:
            blocks.append(clean)
    if counters["charts"] > MAX_CHARTS:
        errors.append(f"{counters['charts']} charts; Slack allows {MAX_CHARTS} data_visualization blocks per message")
    if counters["markdown"] > MAX_MARKDOWN:
        errors.append(f"markdown blocks total {counters['markdown']:,} characters; Slack allows {MAX_MARKDOWN:,}")

    # Placeholders, wherever they are.
    blocks = _map_strings(blocks, lambda s, md: core.slackify(s) if md else s)
    tokens = all_tokens(blocks)
    for tok in tokens:
        errors.extend(core._check_token(tok, clients))
    if sum(1 for t in tokens if t["name"] == "birdy_take") > 1:
        errors.append("only one {{birdy_take}} per report")
    known = {t["raw"] for t in tokens}
    for _, s in _strings(blocks):
        for loose in core._LOOSE_RE.findall(s):
            if loose not in known:
                errors.append(f"{loose}: not a valid placeholder — use {{{{name key=value}}}}")

    if errors:
        raise TemplateError(list(dict.fromkeys(errors)))
    return {"version": VERSION, "frequency": frequency, "time": core._clean_time(raw.get("time")),
            "day": day, "blocks": blocks}


# ── Figures ───────────────────────────────────────────────────────────────────


def _presets_of(blocks) -> set[str]:
    out = set()
    for b in blocks:
        p = (b.get("birdy_data") or {}).get("period")
        if p:
            out.add(core._PRESET_KEY.get(p, p))
    return out


def _needs_daily(blocks) -> bool:
    return any((b.get("birdy_data") or {}).get("by") == "day" for b in blocks)


async def build_context(db, user_id: str, template: dict, now_utc: datetime, previous: dict | None = None,
                        allow_sample: bool = False, preview: bool = False) -> core.Context:
    blocks = template.get("blocks") or []
    tokens = all_tokens(blocks)
    return await core.load_context(
        db, user_id, template["frequency"], now_utc, tokens=tokens, extra_presets=_presets_of(blocks),
        daily=_needs_daily(blocks), previous=previous, allow_sample=allow_sample, preview=preview)


# ── Rendering ─────────────────────────────────────────────────────────────────


def _short(label: str, used: set, limit: int = 20) -> str:
    """A label within Slack's chart limit, unique within the chart."""
    base = (label or "—").strip()[:limit]
    out, n = base, 2
    while out in used:
        suffix = f" {n}"
        out = base[: limit - len(suffix)] + suffix
        n += 1
    used.add(out)
    return out


def _client_ids(ctx, bd) -> list[str]:
    return [c for c in bd.get("clients", list(ctx.groups)) if c in ctx.groups]


def _rank_clients(ctx, bd, preset) -> list[tuple[str, dict]]:
    """[(client_id, {metric: value})] sorted by bd['sort'] best-first (or worst), clients with no data dropped."""
    metrics = bd["metrics"]
    sort = bd.get("sort") or metrics[0]
    rows = []
    for gid in _client_ids(ctx, bd):
        vals = {m: ctx.value(m, gid, preset) for m in metrics}
        if any(v for v in vals.values()):
            rows.append((gid, vals))
    lower = CATALOG[sort].get("lower_is_better")
    rows.sort(key=lambda r: (r[1][sort] is None, (r[1][sort] or 0) * (1 if lower else -1)))
    if bd.get("order") == "worst":
        rows = [r for r in rows if r[1][sort] is not None][::-1] + [r for r in rows if r[1][sort] is None]
    return rows


def _local_today(ctx):
    try:
        zone = ZoneInfo(getattr(ctx, "timezone", None) or "Europe/London")
    except (ZoneInfoNotFoundError, ValueError):
        zone = ZoneInfo("Europe/London")
    return ctx.now_utc.astimezone(zone).date()


def _daily_series(ctx, metric, client_ids, days) -> tuple[list[str], list[float]]:
    """(day labels, totals) for the `days` days up to yesterday."""
    spec = DAILY_METRICS[metric]
    end = _local_today(ctx) - timedelta(days=1)
    dates = [end - timedelta(days=d) for d in range(days - 1, -1, -1)]
    totals = {d.isoformat(): 0.0 for d in dates}
    for gid in client_ids:
        if ctx.sample:
            for k, d in enumerate(dates):
                totals[d.isoformat()] += core._SAMPLES.get(metric, 20.0) / 7 * (0.8 + 0.05 * ((k * 7) % 9))
            continue
        for row in ctx.groups[gid].get(spec["field"]) or []:
            if row.get("date") in totals and isinstance(row.get(spec["key"]), (int, float)):
                totals[row["date"]] += float(row[spec["key"]])
    return [d.strftime("%a %-d") for d in dates], [round(totals[d.isoformat()], 2) for d in dates]


def _chart(ctx, bd) -> dict | None:
    """The `chart` object for a data_visualization block, or None when there's nothing to draw."""
    if bd["by"] == "day":
        ids = _client_ids(ctx, bd)
        labels, _ = _daily_series(ctx, bd["metrics"][0], [], bd["days"])
        used_cats: set = set()
        cats = [_short(c, used_cats) for c in labels]
        if bd.get("split") == "client":
            metric = bd["metrics"][0]
            per = [(ctx.names[g], _daily_series(ctx, metric, [g], bd["days"])[1]) for g in ids]
            per = [p for p in per if any(p[1])]
            per.sort(key=lambda p: -sum(p[1]))
            series_src = per[:12]
        else:
            series_src = [(DAILY_METRICS[m]["label"], _daily_series(ctx, m, ids, bd["days"])[1]) for m in bd["metrics"]]
        if not series_src or not any(any(v) for _, v in series_src):
            return None
        if bd["chart"] == "pie":
            name, values = series_src[0]
            segs = [{"label": c, "value": v} for c, v in zip(cats, values) if v > 0]
            return {"type": "pie", "segments": segs} if segs else None
        used: set = set()
        return {"type": bd["chart"],
                "series": [{"name": _short(n, used), "data": [{"label": c, "value": v} for c, v in zip(cats, vals)]}
                           for n, vals in series_src],
                "axis_config": {"categories": cats, "x_label": "Day"}}

    preset = ctx.preset({"period": bd.get("period")})
    rows = _rank_clients(ctx, {**bd, "sort": bd["metrics"][0]}, preset)[: bd["limit"]]
    if not rows:
        return None
    used_cats: set = set()
    cats = [_short(ctx.names[gid], used_cats) for gid, _ in rows]
    if bd["chart"] == "pie":
        m = bd["metrics"][0]
        segs = [{"label": c, "value": round(vals[m], 2)} for c, (_, vals) in zip(cats, rows) if (vals[m] or 0) > 0]
        return {"type": "pie", "segments": segs} if segs else None
    used: set = set()
    return {"type": bd["chart"],
            "series": [{"name": _short(CATALOG[m]["label"], used),
                        "data": [{"label": c, "value": round(vals[m] or 0, 2)} for c, (_, vals) in zip(cats, rows)]}
                       for m in bd["metrics"]],
            "axis_config": {"categories": cats, "x_label": "Client"}}


def _cell(metric, value, ctx):
    if value is None:
        return {"type": "raw_text", "text": "n/a"}
    return {"type": "raw_number", "value": round(value, 4), "text": core.fmt(metric, value, ctx.currency)}


def _table_rows(ctx, bd) -> list[list[dict]] | None:
    preset = ctx.preset({"period": bd.get("period")})
    rows = _rank_clients(ctx, bd, preset)[: bd["limit"]]
    if not rows:
        return None
    out = [[{"type": "raw_text", "text": "Client"}] +
           [{"type": "raw_text", "text": CATALOG[m]["label"]} for m in bd["metrics"]]]
    for gid, vals in rows:
        out.append([{"type": "raw_text", "text": ctx.names[gid]}] + [_cell(m, vals[m], ctx) for m in bd["metrics"]])
    if bd.get("total_row"):
        ids = [gid for gid, _ in rows]

        def total(m):
            if ctx.sample:
                return ctx.value(m, None, preset)
            return core._value(m, [ctx.views(preset)[g] for g in ids], preset)

        out.append([{"type": "raw_text", "text": "Total"}] + [_cell(m, total(m), ctx) for m in bd["metrics"]])
    return out


def _cards(ctx, bd) -> list[dict] | None:
    preset = ctx.preset({"period": bd.get("period")})
    rows = _rank_clients(ctx, bd, preset)[: bd["limit"]]
    if not rows:
        return None
    window = core.SECTION_PERIODS.get(bd.get("period"), ctx.period["label"].lower())
    cards = []
    for gid, vals in rows:
        body = " · ".join(f"{CATALOG[m]['label']} {core.fmt(m, vals[m], ctx.currency) if vals[m] is not None else 'n/a'}"
                          for m in bd["metrics"])
        cards.append({"type": "card", "title": {"type": "mrkdwn", "text": f"*{ctx.names[gid]}*"[:150]},
                      "subtitle": {"type": "plain_text", "text": window[:1].upper() + window[1:]},
                      "body": {"type": "mrkdwn", "text": body[:200]}})
    return cards


def _no_data(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": f"_{text}: no data for this period yet_"}]}


def _cap_text(b: dict) -> dict:
    """Slack's per-field limits, applied after placeholders have filled."""
    caps = {"header": {"text": 150}, "section": {"text": 3000}, "card": {"title": 150, "subtitle": 150, "body": 200, "subtext": 200}}
    for key, limit in caps.get(b["type"], {}).items():
        if isinstance(b.get(key), dict) and isinstance(b[key].get("text"), str):
            b[key]["text"] = b[key]["text"][:limit] or " "
    if b["type"] == "section" and b.get("fields"):
        for f in b["fields"]:
            f["text"] = f["text"][:2000] or " "
    if b["type"] == "data_visualization":
        b["title"] = b["title"][:50]
    return b


def render(template: dict, ctx: core.Context, take: str | None = None) -> tuple[list[dict], str]:
    """Slack blocks (live numbers filled, data blocks expanded) + notification fallback text."""
    def fill_string(s: str, md: bool) -> str:
        if "{{" not in s:
            return core._escape(s) if md else s
        out, last = [], 0
        for tok in core.parse_tokens(s):
            out.append(core._escape(s[last:tok["start"]]) if md else s[last:tok["start"]])
            value = (take or "") if tok["name"] == "birdy_take" else core._resolve(tok, ctx)
            if value.startswith("\n") and "".join(out).rstrip(" \t").endswith("\n"):
                value = value[1:]  # a list already on its own line: no extra break, no stray indent
                if out:
                    out[-1] = out[-1].rstrip(" \t")
            out.append(core._escape(value) if md else value)
            last = tok["end"]
        out.append(core._escape(s[last:]) if md else s[last:])
        text = "".join(out)
        # A placeholder with nothing to say yet (a change on the first report, a
        # missing target) leaves its wrapper behind: "(Change: )", "()", "· ".
        text = re.sub(r"[ \t]*\((?:[^()\n:]{0,30}:)?[ \t]*\)", "", text)
        text = re.sub(r"[ \t]*[·|][ \t]*(?=\n|$)", "", text)
        text = re.sub(r"[ \t]+(\n|$)", r"\1", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip() or " "

    blocks = []
    for b in copy.deepcopy(template.get("blocks") or []):
        bd = b.pop("birdy_data", None)
        if bd is not None:
            if b["type"] == "data_visualization":
                chart = _chart(ctx, bd)
                if not chart:
                    blocks.append(_no_data(b["title"]))
                    continue
                b["chart"] = chart
            elif b["type"] in ("table", "data_table"):
                rows = _table_rows(ctx, bd)
                if not rows:
                    blocks.append(_no_data(b.get("caption") or "Table"))
                    continue
                b["rows"] = rows
            elif b["type"] == "carousel":
                cards = _cards(ctx, bd)
                if not cards:
                    blocks.append(_no_data("Clients"))
                    continue
                b["elements"] = cards
        b = _map_strings(b, fill_string)
        blocks.append(_cap_text(b))
    blocks = blocks[:MAX_BLOCKS] or [{"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing to report._"}}]
    return blocks, fallback_text(blocks)


def fallback_text(blocks: list[dict]) -> str:
    """Plain text for notifications and for Birdy's take to read."""
    out = []
    for b in blocks:
        t = b["type"]
        if t in ("header", "section") and isinstance(b.get("text"), dict):
            out.append(b["text"]["text"])
        if t == "section" and b.get("fields"):
            out.append("\n".join(f["text"] for f in b["fields"]))
        if t == "markdown":
            out.append(b["text"])
        if t == "context":
            out.append(" ".join(e.get("text", "") for e in b["elements"] if isinstance(e, dict)))
        if t == "data_visualization":
            chart = b.get("chart") or {}
            if chart.get("type") == "pie":
                out.append(f"{b['title']}: " + ", ".join(f"{s['label']} {s['value']}" for s in chart.get("segments", [])))
            else:
                lines = [f"{s['name']}: " + ", ".join(f"{d['label']} {d['value']}" for d in s["data"])
                         for s in chart.get("series", [])]
                out.append(f"{b['title']}\n" + "\n".join(lines))
        if t in ("table", "data_table") and b.get("rows"):
            out.append("\n".join(" | ".join(c.get("text", "") for c in row if isinstance(c, dict)) for row in b["rows"]))
        if t == "carousel":
            out.append("\n".join(f"{c.get('title', {}).get('text', '')}: {c.get('body', {}).get('text', '')}"
                                 for c in b.get("elements", [])))
    return "\n".join(x for x in out if x and x.strip())[:3000]


def snapshot(ctx, period_key: str) -> dict:
    return core.snapshot(ctx, period_key)


async def write_take(db, user_id: str, template: dict, report_text: str) -> str | None:
    tok = has_take(template)
    if not tok:
        return None
    # The raw strings, not a JSON dump: JSON escapes the quotes in
    # {{birdy_take focus="..."}}, and the placeholder then no longer parses.
    body = "\n".join(text for _, text in _strings(template.get("blocks") or []))
    return await core.write_take(db, user_id, {"frequency": template["frequency"], "body": body}, report_text)


# ── For the copilot ───────────────────────────────────────────────────────────

BLOCKS_GUIDE = """The report is a Slack Block Kit message: a JSON array of blocks. Allowed in channel messages:
header {text plain ≤150} · section {text mrkdwn ≤3000 | fields: 1-10 mrkdwn ≤2000 (two-column grid), accessory: image or link button}
divider · image {image_url https, alt_text, title?} · context {elements: 1-10 mrkdwn text or images — small grey print}
actions {elements: link buttons {type:"button", text, url, style?: primary|danger}} · rich_text (Slack's rich text elements)
markdown {text: standard Markdown incl. tables, ≤12,000 chars total} · card {title, subtitle, body ≤200, hero_image, icon, actions ≤3 link buttons}
carousel {elements: 1-10 cards} · table {rows} · data_table {caption, rows, page_size — sortable, paginated}
data_visualization {title ≤50, chart} — native Slack charts: line, bar, area, pie. At most 2 per message.
NOT allowed: alert and input (modals only). Buttons must be links (url); Birdy doesn't handle button clicks.

Live data — two ways, never type a figure yourself:
1. {{placeholders}} inside any text (see below).
2. "birdy_data" on a chart/table/carousel, and Birdy fills it at send time:
   data_visualization: {"chart": "line|bar|area|pie", "by": "client"|"day", "metrics": [...], "days": 2-20 (by=day), "limit": max clients,
                        "split": "client" (by=day, one metric → one line per client), "clients": ["name", ...], "period": window (by=client)}
     by="client" metrics: any list_report_metrics id. by="day" metrics: spend, impressions, clicks, crm_leads, deals_won, deals_lost, calls, inbound_calls, talk_time.
     Pie = exactly one metric. Up to 12 series, up to 20 categories; negative values are fine.
   table / data_table: {"metrics": [...], "sort": metric, "order": "best"|"worst", "limit": n, "total_row": true, "clients": [...], "period": window}
   carousel: {"metrics": [1-3], "sort", "order", "limit": 1-10, "period"} → one card per client.
"""
