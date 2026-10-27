"""
services/report_template.py
---------------------------
The Slack report as a free-form Slack message with live placeholders.

The user (or the report copilot) writes the message exactly as they want it:
any wording, emojis, bullets, quotes, links, headings, dividers. Wherever a
live figure belongs they put a placeholder; at send time each placeholder is
replaced with the real number, read from the same caches, through the same
metric registry, that the app's pages use. Nothing else is computed, and the
only model-written text is the optional {{birdy_take}}, which is given the
rendered report and nothing else.

Stored at users.integrations.slack_bot.brief:

    {"version": 3, "frequency": "daily" | "weekly", "day": "Monday" | None,
     "time": "9:00 AM", "body": "<Slack mrkdwn with {{placeholders}}>"}

Placeholders: {{name key=value key="value with spaces"}}

    {{spend}}                                   value of a metric, all clients
    {{cpl client="Glow Aesthetics"}}            one client (name or id)
    {{leads period=this_month}}                 another cached window
    {{spend show=change}}                       ▲ 12% vs the period before
    {{cpl client="Glow" show=vs_target}}        on target (£12.00)
    {{cpl client="Glow" show=target}}           £12.00
    {{ranking metric=cpl order=best limit=3}}   numbered list of clients
    {{breakdown metrics="spend,leads,cpl" sort=spend limit=10}}  one bullet per client
    {{alerts}} {{alerts show=count}} {{alerts show=list limit=3}}
    {{flagged_ads}} (same options)
    {{birdy_take focus="what to do next"}}      at most one
    {{period}} {{date}} {{client_count}}

Slack rules the body is held to (see `to_blocks`): Markdown habits are
converted (**bold** → *bold*, [text](url) → <url|text>, "# Title" → header
block), "---" lines become dividers, sections are split under Slack's 3,000
character limit, a message has at most 50 blocks, headers at most 150 plain
characters.

Older briefs — the six checkboxes, and the v2 section lists — are converted on
read by `normalize`, so every account renders through this module.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta

from services.metric_orchestrator import METRIC_REGISTRY, get_metric_value

logger = logging.getLogger(__name__)

VERSION = 3
MAX_BODY = 8000
MAX_BLOCKS = 50
SECTION_LIMIT = 3000
HEADER_LIMIT = 150
MAX_RANKING = 10
MAX_BREAKDOWN_ROWS = 25
MAX_BREAKDOWN_METRICS = 4
FREQUENCIES = ("daily", "weekly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
LEGACY_ITEM_KEYS = ("spend", "leads", "conversion", "top", "alerts", "underperform")

PERIODS = {
    "daily": {"preset": "yesterday", "label": "Yesterday", "days": 1, "prev": "the day before"},
    "weekly": {"preset": "last_7d", "label": "Last 7 days", "days": 7, "prev": "the week before"},
}

# Windows a placeholder may report on instead of the report's own. Each is a
# preset the Meta / CRM / call-centre refreshes already cache.
SECTION_PERIODS = {
    "yesterday": "yesterday",
    "this_week": "this week",
    "last_7d": "last 7 days",
    "last_14d": "last 14 days",
    "last_30d": "last 30 days",
    "this_month": "this month",
    "last_month": "last month",
}
_PRESET_KEY = {"this_week": "this_week_mon_today"}

CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "AED": "AED ", "INR": "₹", "ZAR": "R", "JPY": "¥",
    "CHF": "CHF ", "CNY": "¥", "MXN": "MX$", "SAR": "SAR ",
}

# The metrics a report can carry: a curated slice of METRIC_REGISTRY plus a few
# combinations of it.
#   target:   (field, kind) — "ratio" compares directly, "total" pro-rates a
#             monthly target to the window, "ratio_fraction" is a stored 0-1.
#   per_unit: a cost per something, shown to two decimal places.
#   formula:  (numerator, denominator) over registry sum metrics.
CATALOG: dict[str, dict] = {
    "spend":           {"label": "Ad spend", "source": "meta", "lower_is_better": None, "target": ("monthly_spend", "total")},
    "leads":           {"label": "Leads", "source": "meta", "lower_is_better": False},
    "cpl":             {"label": "Cost per lead", "source": "meta", "lower_is_better": True, "per_unit": True, "target": ("cpl", "ratio")},
    "impressions":     {"label": "Impressions", "source": "meta", "lower_is_better": False},
    "reach":           {"label": "Reach", "source": "meta", "lower_is_better": False},
    "clicks":          {"label": "Clicks", "source": "meta", "lower_is_better": False},
    "ctr":             {"label": "Click-through rate", "source": "meta", "lower_is_better": False},
    "cpc":             {"label": "Cost per click", "source": "meta", "lower_is_better": True, "per_unit": True},
    "cpm":             {"label": "Cost per 1,000 impressions", "source": "meta", "lower_is_better": True, "per_unit": True},
    "frequency":       {"label": "Ad frequency", "source": "meta", "lower_is_better": None},
    "ghl_total_opps":  {"label": "Opportunities", "source": "ghl_opp", "lower_is_better": False},
    "ghl_open_opps":   {"label": "Open opportunities", "source": "ghl_opp", "lower_is_better": None},
    "ghl_won_opps":    {"label": "Deals won", "source": "ghl_opp", "lower_is_better": False, "target": ("monthly_wins", "total")},
    "ghl_lost_opps":   {"label": "Deals lost", "source": "ghl_opp", "lower_is_better": True},
    "ghl_revenue":     {"label": "Revenue won", "source": "ghl_opp", "lower_is_better": False, "target": ("monthly_revenue", "total")},
    "ghl_conversion":  {"label": "Close rate", "source": "ghl_opp", "lower_is_better": False, "target": ("conversion_rate", "ratio_fraction")},
    "hp_total_calls":  {"label": "Calls made", "source": "hp_call", "lower_is_better": False},
    "hp_inbound":      {"label": "Inbound calls", "source": "hp_call", "lower_is_better": False},
    "hp_outbound":     {"label": "Outbound calls", "source": "hp_call", "lower_is_better": False},
    "hp_answered_calls": {"label": "Calls answered", "source": "hp_call", "lower_is_better": False},
    "hp_transfers":    {"label": "Transfers", "source": "hp_call", "lower_is_better": False},
    "hp_talk_time":    {"label": "Talk time (min)", "source": "hp_call", "lower_is_better": False},
    "hp_answer_rate":  {"label": "Answer rate", "source": "hp_call", "lower_is_better": False},
    "hp_connect_rate": {"label": "Lead connect rate", "source": "hp_call", "lower_is_better": False},
    "cost_per_win":    {"label": "Cost per deal won", "source": "combined", "lower_is_better": True, "per_unit": True,
                        "format": "currency", "target": ("cpa", "ratio"), "formula": ("meta_spend", "ghl_won_opps"),
                        "sources": ("meta", "ghl_opp")},
    "cost_per_opp":    {"label": "Cost per opportunity", "source": "combined", "lower_is_better": True, "per_unit": True,
                        "format": "currency", "formula": ("meta_spend", "ghl_total_opps"), "sources": ("meta", "ghl_opp")},
    "roas":            {"label": "Return on ad spend", "source": "combined", "lower_is_better": False,
                        "format": "roas", "formula": ("ghl_revenue", "meta_spend"), "sources": ("meta", "ghl_opp")},
}

SOURCE_LABELS = {"meta": "Meta Ads", "ghl_opp": "CRM pipeline", "hp_call": "Call centre", "combined": "Meta + CRM"}

SPECIAL_TOKENS = ("ranking", "breakdown", "alerts", "flagged_ads", "birdy_take", "period", "date", "client_count")
# Filled from the Birdy event that sent the report (services/report_events.py);
# blank in a scheduled report.
EVENT_TOKENS = {"event": "the event", "alert_name": "alert name", "alert_message": "alert message",
                "account_name": "account name", "metric_name": "metric name"}

_TOKEN_RE = re.compile(r"\{\{\s*([a-z_]+)((?:\s+[a-z_]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s}]+))*)\s*\}\}")
_OPT_RE = re.compile(r"([a-z_]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s}]+))")
_LOOSE_RE = re.compile(r"\{\{[^}]*\}\}")


class TemplateError(ValueError):
    """A report that cannot be stored, with every reason."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _sources_of(metric: str) -> tuple[str, ...]:
    meta = CATALOG[metric]
    return tuple(meta.get("sources") or (meta["source"],))


# ── Placeholders ──────────────────────────────────────────────────────────────


def parse_tokens(body: str) -> list[dict]:
    """Every placeholder in the body: {raw, name, opts, start, end}."""
    out = []
    for m in _TOKEN_RE.finditer(body or ""):
        opts = {}
        for o in _OPT_RE.finditer(m.group(2) or ""):
            opts[o.group(1)] = next(v for v in o.groups()[1:] if v is not None)
        out.append({"raw": m.group(0), "name": m.group(1), "opts": opts, "start": m.start(), "end": m.end()})
    return out


def _resolve_client(ref: str | None, clients: dict[str, str]) -> str | None:
    """A client id from an id or a (case-insensitive) name. clients: id -> name."""
    if not ref:
        return None
    if ref in clients:
        return ref
    lowered = ref.strip().lower()
    for cid, name in clients.items():
        if (name or "").strip().lower() == lowered:
            return cid
    return None


def _check_token(tok: dict, clients: dict[str, str] | None) -> list[str]:
    name, opts = tok["name"], tok["opts"]
    where = tok["raw"]
    errs = []

    def period_ok():
        p = opts.get("period")
        if p and p not in SECTION_PERIODS:
            errs.append(f"{where}: period must be one of {', '.join(SECTION_PERIODS)}")

    def client_ok(ref):
        if clients is not None and ref and not _resolve_client(ref, clients):
            errs.append(f"{where}: no client called {ref!r}; use list_report_clients")

    if name in CATALOG:
        show = opts.get("show", "value")
        if show not in ("value", "change", "target", "vs_target"):
            errs.append(f"{where}: show must be value, change, target or vs_target")
        if show in ("target", "vs_target") and "target" not in CATALOG[name]:
            errs.append(f"{where}: {CATALOG[name]['label']} has no stored target")
        if show == "change" and opts.get("period"):
            errs.append(f"{where}: show=change compares with the last report, so it can't take a period")
        period_ok()
        client_ok(opts.get("client"))
    elif name == "ranking":
        if opts.get("metric") not in CATALOG:
            errs.append(f"{where}: ranking needs metric=<one of list_report_metrics>")
        if opts.get("order", "best") not in ("best", "worst"):
            errs.append(f"{where}: order must be best or worst")
        period_ok()
    elif name == "breakdown":
        metrics = [m.strip() for m in (opts.get("metrics") or "").split(",") if m.strip()]
        if not metrics:
            errs.append(f"{where}: breakdown needs metrics=\"a,b\"")
        elif len(metrics) > MAX_BREAKDOWN_METRICS:
            errs.append(f"{where}: at most {MAX_BREAKDOWN_METRICS} metrics")
        for m in metrics:
            if m not in CATALOG:
                errs.append(f"{where}: unknown metric {m!r}")
        if opts.get("sort") and opts["sort"] not in metrics:
            errs.append(f"{where}: sort must be one of its metrics")
        for ref in [c.strip() for c in (opts.get("clients") or "").split(",") if c.strip() and c.strip() != "all"]:
            client_ok(ref)
        period_ok()
    elif name in ("alerts", "flagged_ads"):
        if opts.get("show", "summary") not in ("summary", "count", "list"):
            errs.append(f"{where}: show must be summary, count or list")
    elif name in ("birdy_take", "period", "date", "client_count") or name in EVENT_TOKENS:
        pass
    else:
        errs.append(f"{where}: unknown placeholder; metrics are {', '.join(CATALOG)}, "
                    f"others are {', '.join((*SPECIAL_TOKENS, *EVENT_TOKENS))}")
    return errs


# ── Slack rules for the written body ──────────────────────────────────────────


def slackify(body: str) -> str:
    """Common Markdown habits rewritten as Slack mrkdwn."""
    text = body.replace("\r\n", "\n")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)                       # **bold** → *bold*
    text = re.sub(r"(?<![\w_])__(.+?)__(?![\w_])", r"_\1_", text)          # __it__ → _it_
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)  # [t](u) → <u|t>
    text = re.sub(r"^(\s*)[-*] (?=\S)", r"\1• ", text, flags=re.M)        # - item → • item
    return text


def _escape(text: str) -> str:
    """Escape &, <, > for Slack, leaving Slack's own <...> syntax (links, mentions) alone."""
    out, last = [], 0
    for m in re.finditer(r"<(?:https?://|mailto:|@|#|!)[^<>\n]*>", text):
        out.append(text[last:m.start()].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        out.append(m.group(0))
        last = m.end()
    out.append(text[last:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out).replace("&amp;amp;", "&amp;")


def _split_text(text: str, limit: int = SECTION_LIMIT - 100) -> list[str]:
    """Text in chunks Slack accepts, split at paragraph, then line, boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in re.split(r"(\n\n)", text):
        if len(cur) + len(para) <= limit:
            cur += para
            continue
        if cur.strip():
            chunks.append(cur.strip("\n"))
        cur = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            chunks.append(para[:cut])
            para = para[cut:].lstrip("\n")
        cur = para
    if cur.strip():
        chunks.append(cur.strip("\n"))
    return chunks


def to_blocks(text: str) -> list[dict]:
    """
    Rendered mrkdwn → Slack blocks.
      "# Title" / "## Title" lines → header blocks (plain text, 150 chars)
      "---" lines                    → dividers
      everything else                → mrkdwn sections, split under 3,000 chars
    """
    blocks: list[dict] = []
    para: list[str] = []

    def flush():
        joined = "\n".join(para).strip("\n")
        para.clear()
        if joined.strip():
            for chunk in _split_text(joined):
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})

    for line in text.split("\n"):
        stripped = line.strip()
        if re.fullmatch(r"-{3,}|_{3,}|\*{3,}", stripped):
            flush()
            blocks.append({"type": "divider"})
        elif re.match(r"^#{1,6}\s+\S", stripped):
            flush()
            title = re.sub(r"^#{1,6}\s+", "", stripped)
            title = re.sub(r"[*_~`]", "", title)  # header blocks are plain text
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": title[:HEADER_LIMIT], "emoji": True}})
        else:
            para.append(line)
    flush()
    return blocks


def plain_text(blocks: list[dict]) -> str:
    """Notification fallback text: the message without block structure."""
    out = []
    for b in blocks:
        if b["type"] in ("header", "section"):
            out.append(b["text"]["text"])
    return "\n".join(out)


# ── Shape ─────────────────────────────────────────────────────────────────────

DEFAULT_BODY = """# Birdy brief · {{period}}
💰 *Ad spend:* {{spend}} {{spend show=change}}
📈 *Leads:* {{leads}} {{leads show=change}}
🏷️ *Cost per lead:* {{cpl}} {{cpl show=change}}
🎯 *Close rate:* {{ghl_conversion}}
🏆 *Top by leads:* {{ranking metric=leads order=best limit=1}}
🔔 *Alerts:* {{alerts}}"""


def default_template(frequency: str = "daily", time: str = "9:00 AM", day: str | None = None) -> dict:
    return {"version": VERSION, "frequency": frequency, "time": time,
            "day": (day or "Monday") if frequency == "weekly" else None, "body": DEFAULT_BODY}


def _legacy_items_body(items: dict) -> str:
    lines = ["# Birdy brief · {{period}}"]
    if items.get("spend"):
        lines.append("💰 *Ad spend:* {{spend}}")
    if items.get("leads"):
        lines.append("📈 *Leads:* {{leads}}")
        lines.append("🏷️ *Cost per lead:* {{cpl}}")
    if items.get("conversion"):
        lines.append("🎯 *Close rate:* {{ghl_conversion}}")
    if items.get("top"):
        lines.append("🏆 *Top by leads:* {{ranking metric=leads order=best limit=1}}")
    if items.get("alerts"):
        lines.append("🔔 *Alerts:* {{alerts}}")
    if items.get("underperform"):
        lines.append("⚠️ *Underperforming ads:* {{flagged_ads}}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _q(v: str) -> str:
    return f'"{v}"' if re.search(r"[\s,]", v) else v


def _v2_body(t: dict) -> str:
    """A v2 section list as the equivalent free-form body."""
    style = t.get("style") or {}
    em = style.get("emojis", True)
    lines = []
    if style.get("header", True):
        lines.append("# " + (t.get("title") or "Birdy brief") + " · {{period}}")
    if t.get("intro"):
        lines.append(t["intro"])
    for s in t.get("sections") or []:
        kind = s.get("type")
        period = f" period={s['period']}" if s.get("period") else ""
        if kind == "metric" and s.get("metric") in CATALOG:
            m = s["metric"]
            client = s.get("scope", {}).get("client_id") if isinstance(s.get("scope"), dict) else None
            cl = f" client={_q(client)}" if client else ""
            label = s.get("label") or CATALOG[m]["label"]
            extra = {"previous": f" {{{{{m}{cl} show=change}}}}", "target": f" {{{{{m}{cl}{period} show=vs_target}}}}"}.get(s.get("compare"), "")
            lines.append(f"*{label}:* {{{{{m}{cl}{period}}}}}{extra}")
        elif kind == "ranking" and s.get("metric") in CATALOG:
            word = "Top" if s.get("direction") != "worst" else "Bottom"
            label = s.get("label") or f"{word} by {CATALOG[s['metric']]['label'].lower()}"
            lines.append(f"{'🏆 ' if em else ''}*{label}:*\n{{{{ranking metric={s['metric']} order={s.get('direction', 'best')} limit={s.get('limit', 3)}{period}}}}}")
        elif kind == "breakdown":
            ms = ",".join(s.get("metrics") or [])
            lines.append(f"*{s.get('label') or 'By client'}:*\n{{{{breakdown metrics=\"{ms}\" sort={s.get('sort_by')} limit={s.get('limit', 10)}{period}}}}}")
        elif kind == "heading":
            lines.append(f"*{s.get('text')}*")
        elif kind == "text":
            lines.append(s.get("text") or "")
        elif kind == "divider":
            lines.append("---")
        elif kind == "alerts":
            lines.append(f"{'🔔 ' if em else ''}*{s.get('label') or 'Alerts'}:* {{{{alerts}}}}")
        elif kind == "underperforming_ads":
            lines.append(f"{'⚠️ ' if em else ''}*{s.get('label') or 'Underperforming ads'}:* {{{{flagged_ads}}}}")
        elif kind == "birdy_take":
            focus = f' focus="{s["focus"]}"' if s.get("focus") else ""
            lines.append(f"\n{'🐦 ' if em else ''}*{s.get('label') or 'Birdy’s take'}:* {{{{birdy_take{focus}}}}}")
    return "\n".join(lines)


def normalize(brief: dict | None) -> dict | None:
    """Any stored brief as a v3 template, or None when there is none."""
    if not brief:
        return None
    frequency = brief.get("frequency") if brief.get("frequency") in FREQUENCIES else None
    base = {"version": VERSION, "frequency": frequency, "time": brief.get("time") or "9:00 AM",
            "day": (brief.get("day") or "Monday") if frequency == "weekly" else None}
    if brief.get("updated_at"):
        base["updated_at"] = brief["updated_at"]
    if brief.get("version") == VERSION:
        return {**base, "body": brief.get("body") or ""}
    if brief.get("version") == 2:
        return {**base, "body": _v2_body(brief)}
    return {**base, "body": _legacy_items_body(brief.get("items") or {})}


def _clean_time(raw) -> str:
    from services.slack_brief import parse_time
    t = parse_time(raw if isinstance(raw, str) else None)
    return datetime.combine(date.today(), t).strftime("%-I:%M %p")


def validate(raw: dict, clients: dict[str, str] | None = None) -> dict:
    """
    A clean v3 template, or TemplateError listing everything wrong with it.

    `clients` maps the user's client ids to names; placeholders naming any
    other client are rejected. None skips that check (unit tests only).
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

    body = raw.get("body")
    if not isinstance(body, str) or not body.strip():
        errors.append("body is empty: write the message")
        body = ""
    body = slackify(body.strip())
    if len(body) > MAX_BODY:
        errors.append(f"the message is {len(body):,} characters; keep it under {MAX_BODY:,}")

    tokens = parse_tokens(body)
    for tok in tokens:
        errors.extend(_check_token(tok, clients))
    if sum(1 for t in tokens if t["name"] == "birdy_take") > 1:
        errors.append("only one {{birdy_take}} per report")
    known = {t["raw"] for t in tokens}
    for loose in _LOOSE_RE.findall(body):
        if loose not in known:
            errors.append(f"{loose}: not a valid placeholder — use {{{{name key=value}}}}")

    if not errors:
        # Slack's block limits, measured on the message as written.
        blocks = to_blocks(body)
        if len(blocks) > MAX_BLOCKS:
            errors.append(f"the message would be {len(blocks)} Slack blocks; Slack allows {MAX_BLOCKS}. "
                          "Use fewer headings and dividers.")
        if not blocks:
            errors.append("the message has nothing to show")

    if errors:
        raise TemplateError(errors)
    return {"version": VERSION, "frequency": frequency, "time": _clean_time(raw.get("time")),
            "day": day, "body": body}


def has_content(template: dict | None) -> bool:
    return bool(template and template.get("frequency") in FREQUENCIES and (template.get("body") or "").strip())


def has_take(template: dict) -> dict | None:
    return next((t for t in parse_tokens(template.get("body") or "") if t["name"] == "birdy_take"), None)


# ── Figures ───────────────────────────────────────────────────────────────────

_SOURCE_KEY = {"meta": "facebook_cache", "ghl_opp": "ghl_opp_cache", "hp_call": "hotprospector_call_cache"}


def _window_view(group: dict, preset: str) -> dict:
    """
    The group with only this window's caches left in it. The registry's
    readers fall back to lifetime or legacy stats when a window is missing —
    right for a dashboard, wrong for a report that says "yesterday".
    """
    view = {"id": group.get("id"), "name": group.get("name")}
    fb = group.get("facebook_cache") or {}
    meta_doc = (fb.get("presets") or {}).get(preset) or fb.get(preset)
    if isinstance(meta_doc, dict):
        view["facebook_cache"] = {"presets": {preset: meta_doc}}
    opp = (group.get("ghl_opp_cache") or {}).get(preset)
    if isinstance(opp, dict):
        view["ghl_opp_cache"] = {preset: opp}
    calls = (group.get("hotprospector_call_cache") or {}).get(preset)
    if isinstance(calls, dict):
        view["hotprospector_call_cache"] = {preset: calls}
    return view


def _value(metric: str, views: list[dict], preset: str) -> float | None:
    meta = CATALOG[metric]
    views = [v for v in views if all(_SOURCE_KEY[s] in v for s in _sources_of(metric))]
    if not views:
        return None
    if "formula" in meta:
        num_id, den_id = meta["formula"]
    elif METRIC_REGISTRY[metric].get("aggregation") == "recompute":
        num_id, den_id = METRIC_REGISTRY[metric]["components"]
    else:
        return sum(get_metric_value(metric, v, preset) for v in views)
    num = sum(get_metric_value(num_id, v, preset) for v in views)
    den = sum(get_metric_value(den_id, v, preset) for v in views)
    scale = METRIC_REGISTRY.get(metric, {}).get("scale", 1) if "formula" not in meta else 1
    return (num / den) * scale if den else None


def period_days(preset: str, today: date) -> int:
    fixed = {"yesterday": 1, "last_7d": 7, "last_14d": 14, "last_30d": 30}
    if preset in fixed:
        return fixed[preset]
    if preset == "this_week_mon_today":
        return today.weekday() + 1
    if preset == "this_month":
        return today.day
    if preset == "last_month":
        last = today.replace(day=1) - timedelta(days=1)
        return calendar.monthrange(last.year, last.month)[1]
    return 30


def _target(metric: str, groups: list[dict], days: int) -> float | None:
    spec = CATALOG[metric].get("target")
    if not spec:
        return None
    field, kind = spec
    values = [float(v) for g in groups for v in [(g.get("targets") or {}).get(field)]
              if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    if not values:
        return None
    if kind == "total":
        return sum(values) * days / 30.0
    if len(groups) != 1:
        return None  # a ratio target belongs to one client; a portfolio has no single one
    return values[0] * 100 if kind == "ratio_fraction" else values[0]


def money(amount: float, currency: str, pence: bool = False) -> str:
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper(), f"{(currency or '').upper()} ")
    return f"{symbol}{amount:,.2f}" if pence else f"{symbol}{amount:,.0f}"


def fmt(metric: str, value: float, currency: str) -> str:
    meta = CATALOG.get(metric, {})
    kind = meta.get("format") or METRIC_REGISTRY.get(metric, {}).get("format")
    if kind == "currency":
        return money(value, currency, pence=bool(meta.get("per_unit")))
    if kind == "roas":
        return f"{value:,.2f}x"
    if kind == "percentage":
        return f"{value:.1f}%"
    if kind == "decimal":
        return f"{value:,.2f}"
    return f"{value:,.0f}"


_SAMPLES = {
    "spend": 142.0, "leads": 18.0, "cpl": 7.89, "impressions": 12750.0, "reach": 8400.0, "clicks": 169.0,
    "ctr": 1.9, "cpc": 0.84, "cpm": 11.2, "frequency": 1.52, "ghl_total_opps": 14.0, "ghl_open_opps": 9.0,
    "ghl_won_opps": 3.0, "ghl_lost_opps": 2.0, "ghl_revenue": 2400.0, "ghl_conversion": 21.4,
    "hp_total_calls": 46.0, "hp_inbound": 12.0, "hp_outbound": 34.0, "hp_answered_calls": 29.0,
    "hp_transfers": 4.0, "hp_talk_time": 182.0, "hp_answer_rate": 63.0, "hp_connect_rate": 72.0,
    "cost_per_win": 47.33, "cost_per_opp": 10.14, "roas": 16.9,
}


class Context:
    """Everything placeholders resolve against, for one user and one send."""

    def __init__(self, *, frequency, groups, currency, now_utc, alerts, flagged, previous=None, sample=False,
                 preview=False):
        self.frequency = frequency
        self.period = PERIODS[frequency]
        self.base = self.period["preset"]
        self.groups = {g["id"]: g for g in groups if g.get("id")}
        self.names = {gid: g.get("name") or "Client" for gid, g in self.groups.items()}
        self.currency = currency
        self.now_utc = now_utc
        self.alerts = alerts
        self.flagged = flagged
        self.previous = (previous or {}).get("values") or {}
        self.sample = sample
        self.preview = preview  # a preview, not a send: explain what can't be shown yet
        self.values: dict[str, float] = {}   # this send's figures, for the next send's show=change
        self.event: dict[str, str] = {}      # the event that sent it, if one did
        self._views: dict[str, dict] = {}

    def views(self, preset):
        if preset not in self._views:
            self._views[preset] = {gid: _window_view(g, preset) for gid, g in self.groups.items()}
        return self._views[preset]

    def preset(self, opts) -> str:
        p = opts.get("period")
        return _PRESET_KEY.get(p, p) if p else self.base

    def value(self, metric, client_id, preset) -> float | None:
        if self.sample:
            return _SAMPLES[metric] * (0.6 if client_id else 1)
        views = self.views(preset)
        ids = [client_id] if client_id else list(views)
        return _value(metric, [views[i] for i in ids if i in views], preset)


def _key(metric, client_id, preset):
    return f"{metric}:{client_id or 'portfolio'}@{preset}"


def _resolve(tok: dict, ctx: Context) -> str:
    name, opts = tok["name"], tok["opts"]
    cur = ctx.currency

    if name in CATALOG:
        client_id = _resolve_client(opts.get("client"), ctx.names) if opts.get("client") else None
        if opts.get("client") and not client_id:
            return "n/a"
        preset = ctx.preset(opts)
        value = ctx.value(name, client_id, preset)
        show = opts.get("show", "value")
        if value is not None:
            ctx.values[_key(name, client_id, preset)] = value
        if value is None:
            return "no data" if show == "value" else ""
        if show == "value":
            return fmt(name, value, cur)
        if show == "change":
            before = ctx.previous.get(_key(name, client_id, preset))
            if before in (None, 0):
                return f"_(change vs {ctx.period['prev']} shows from the 2nd report)_" if ctx.preview else ""
            pct = (value - before) / abs(before) * 100
            if abs(pct) < 0.5:
                return f"flat vs {ctx.period['prev']}"
            return f"{'▲' if pct > 0 else '▼'} {abs(pct):.0f}% vs {ctx.period['prev']}"
        groups = [ctx.groups[client_id]] if client_id else list(ctx.groups.values())
        target = (value * 1.1 if ctx.sample else
                  _target(name, groups, period_days(preset, ctx.now_utc.date())))
        if target is None:
            return "_(no target set)_" if ctx.preview else ""
        if show == "target":
            return fmt(name, target, cur)
        lower = CATALOG[name].get("lower_is_better")
        if lower is None:
            pct = (value - target) / target * 100
            return f"{abs(pct):.0f}% {'over' if pct > 0 else 'under'} the {fmt(name, target, cur)} pace"
        hit = value <= target if lower else value >= target
        return f"{'on' if hit else 'off'} target ({fmt(name, target, cur)})"

    if name == "ranking":
        metric, preset = opts["metric"], ctx.preset(opts)
        limit = max(1, min(MAX_RANKING, int(opts.get("limit") or 3) if str(opts.get("limit", "3")).isdigit() else 3))
        rows = [(ctx.names[gid], v) for gid in ctx.groups
                for v in [ctx.value(metric, gid, preset)] if v]
        if ctx.sample and not rows:
            rows = [(n, _SAMPLES[metric] * (1 - 0.2 * i)) for i, n in enumerate(list(ctx.names.values()) or ["Your first client"])]
        lower = CATALOG[metric].get("lower_is_better")
        rows.sort(key=lambda r: r[1], reverse=not lower)
        if opts.get("order") == "worst":
            rows.reverse()
        rows = rows[:limit]
        if not rows:
            return "no client has data for this period"
        if len(rows) == 1:
            return f"{rows[0][0]} ({fmt(metric, rows[0][1], cur)})"
        return "\n" + "\n".join(f"{i + 1}. {n} · {fmt(metric, v, cur)}" for i, (n, v) in enumerate(rows))

    if name == "breakdown":
        metrics = [m.strip() for m in opts["metrics"].split(",") if m.strip()]
        preset = ctx.preset(opts)
        refs = [c.strip() for c in (opts.get("clients") or "all").split(",") if c.strip()]
        ids = list(ctx.groups) if refs in (["all"], []) else [i for r in refs for i in [_resolve_client(r, ctx.names)] if i]
        sort = opts.get("sort") if opts.get("sort") in metrics else metrics[0]
        limit = int(opts["limit"]) if str(opts.get("limit", "")).isdigit() else 10
        limit = max(1, min(MAX_BREAKDOWN_ROWS, limit))
        rows = []
        for gid in ids:
            vals = {m: ctx.value(m, gid, preset) for m in metrics}
            if any(v for v in vals.values()):
                rows.append((ctx.names[gid], vals))
        lower = CATALOG[sort].get("lower_is_better")
        rows.sort(key=lambda r: (r[1][sort] is None, (r[1][sort] or 0) * (1 if lower else -1)))
        if opts.get("order") == "worst":
            rows = [r for r in rows if r[1][sort] is not None][::-1] + [r for r in rows if r[1][sort] is None]
        total = len(rows)
        rows = rows[:limit]
        if not rows:
            return "no client has data for this period"
        single = len(metrics) == 1

        def cell(m, v):
            shown = fmt(m, v, cur) if v is not None else "n/a"
            return shown if single else f"{shown} {CATALOG[m]['label'].lower()}"

        lines = [f"• {n} · " + " · ".join(cell(m, vals[m]) for m in metrics) for n, vals in rows]
        if total > limit:
            lines.append(f"_+{total - limit} more_")
        return "\n" + "\n".join(lines)

    if name in ("alerts", "flagged_ads"):
        items = ctx.alerts if name == "alerts" else ctx.flagged
        texts = [(i.get("message") if name == "alerts" else (i.get("title") or "").split("—")[-1].strip()) or ""
                 for i in items]
        show = opts.get("show", "summary")
        if show == "count":
            return str(len(texts))
        if not texts:
            return "none triggered" if name == "alerts" else "none flagged"
        if show == "list":
            limit = int(opts["limit"]) if str(opts.get("limit", "")).isdigit() else 5
            more = f"\n_+{len(texts) - limit} more_" if len(texts) > limit else ""
            return "\n" + "\n".join(f"• {t}" for t in texts[:limit]) + more
        word = "triggered" if name == "alerts" else "flagged"
        more = f" (+{len(texts) - 1} more)" if len(texts) > 1 else ""
        return f"{len(texts)} {word} · {texts[0]}{more}"

    if name == "period":
        return ctx.period["label"]
    if name == "date":
        return ctx.now_utc.strftime("%a %-d %b")
    if name == "client_count":
        return str(len(ctx.groups))
    if name in EVENT_TOKENS:
        return ctx.event.get(name) or (f"‹{EVENT_TOKENS[name]}›" if ctx.preview else "")
    return ""


async def build_context(db, user_id: str, template: dict, now_utc: datetime,
                        previous: dict | None = None, allow_sample: bool = False,
                        preview: bool = False) -> Context:
    """Read everything a v3 body's placeholders need. Pure reads."""
    return await load_context(db, user_id, template["frequency"], now_utc,
                              tokens=parse_tokens(template.get("body") or ""), previous=previous,
                              allow_sample=allow_sample, preview=preview)


DAILY_FIELDS = ("meta_daily_spend", "ghl_daily_leads", "hp_daily_calls")


async def load_context(db, user_id: str, frequency: str, now_utc: datetime, *, tokens: list[dict],
                       extra_presets: set[str] = frozenset(), daily: bool = False,
                       previous: dict | None = None, allow_sample: bool = False,
                       preview: bool = False) -> Context:
    """
    Read what a report needs: the windows its placeholders (and any data
    blocks, via `extra_presets`) name, the daily series when a chart plots
    days, and alerts / flagged ads only when something shows them. Pure reads.
    """
    base = PERIODS[frequency]["preset"]
    presets = {base} | set(extra_presets) | {_PRESET_KEY.get(t["opts"]["period"], t["opts"]["period"])
                                             for t in tokens if t["opts"].get("period") in SECTION_PERIODS}

    # Only the needed windows of each cache: they hold every preset (and, for
    # Meta, campaign/ad breakdowns), so reading them whole moved megabytes.
    projection = {"_id": 0, "id": 1, "name": 1, "targets": 1}
    for p in presets:
        projection[f"facebook_cache.presets.{p}.metrics.insights"] = 1
        projection[f"facebook_cache.{p}.metrics.insights"] = 1
        projection[f"ghl_opp_cache.{p}"] = 1
        projection[f"hotprospector_call_cache.{p}"] = 1
    if daily:
        for field in DAILY_FIELDS:
            projection[field] = 1
    groups = await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}}, projection).to_list(length=1000)
    user = await db["users"].find_one({"user_id": user_id}, {"default_currency": 1, "timezone": 1}) or {}

    names = {t["name"] for t in tokens}
    alerts = flagged = []
    if "alerts" in names:
        since = (now_utc - timedelta(days=PERIODS[frequency]["days"])).replace(tzinfo=None)
        alerts = await db["alert_notifications"].find(
            {"user_id": user_id, "triggered_at": {"$gte": since}}, {"_id": 0, "message": 1},
        ).sort("triggered_at", -1).to_list(length=50)
    if "flagged_ads" in names:
        flagged = await db["ai_suggestions"].find(
            {"user_id": user_id, "status": "open", "action.type": "pause_ads"},
            {"_id": 0, "title": 1, "client_name": 1},
        ).to_list(length=50)

    ctx = Context(frequency=frequency, groups=groups, currency=user.get("default_currency") or "GBP",
                  now_utc=now_utc, alerts=alerts, flagged=flagged, previous=previous, preview=preview)
    ctx.timezone = user.get("timezone")
    if allow_sample:
        has_data = any(ctx.value(m, None, base) is not None for m in ("spend", "ghl_total_opps", "hp_total_calls"))
        if not has_data:
            ctx.sample = True
            if not alerts and "alerts" in names:
                ctx.alerts = [{"message": f"[{next(iter(ctx.names.values()), 'Your first client')}] Cost per lead above target"}]
    return ctx


def fill(template: dict, ctx: Context, take: str | None = None) -> str:
    """The body with every placeholder replaced (the take by `take`, or removed)."""
    body = template.get("body") or ""
    out, last = [], 0
    for tok in parse_tokens(body):
        out.append(body[last:tok["start"]])
        if tok["name"] == "birdy_take":
            out.append(take or "")
        else:
            value = _resolve(tok, ctx)
            # A list placeholder already on its own line doesn't need its own line break.
            if value.startswith("\n") and "".join(out).endswith("\n"):
                value = value[1:]
            out.append(value)
        last = tok["end"]
    out.append(body[last:])
    text = "".join(out)
    text = re.sub(r"[ \t]+(\n|$)", r"\1", text)   # a placeholder that came out empty
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render(template: dict, ctx: Context, take: str | None = None) -> tuple[list[dict], str]:
    """Slack blocks + notification fallback text."""
    blocks = to_blocks(_escape(fill(template, ctx, take)))[:MAX_BLOCKS]
    if not blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "_Nothing to report for this period._"}}]
    return blocks, plain_text(blocks)


def snapshot(ctx: Context, period_key: str) -> dict:
    """This send's figures, for the next send's show=change."""
    return {"period": period_key, "frequency": ctx.frequency, "values": dict(ctx.values)}


def previous_for(stored: dict | None, period_key: str, frequency: str) -> dict | None:
    """Last send's figures, only if they cover the period just before this one."""
    if not stored or stored.get("frequency") != frequency:
        return None
    try:
        then = date.fromisoformat(stored.get("period") or "")
        now = date.fromisoformat(period_key)
    except ValueError:
        return None
    return stored if (now - then).days == PERIODS[frequency]["days"] else None


# ── Birdy's take ──────────────────────────────────────────────────────────────

TAKE_PROMPT = (
    "You are Birdy, an analyst for a marketing agency. Below is the agency's {cadence} Slack report. "
    "Write Birdy's take: at most 60 words, plain sentences, no bullet points, no greeting. "
    "Match the report's tone{emoji_rule}. Say what matters most and one concrete thing to do. "
    "Use ONLY the figures in the report; never introduce a number that is not in it. "
    "If the report has no data, say so in one sentence.{focus}\n\nReport:\n{report}"
)

_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


async def write_take(db, user_id: str, template: dict, report_text: str) -> str | None:
    """The model-written paragraph, or None when it can't or shouldn't run."""
    tok = has_take(template)
    if not tok or not report_text.strip():
        return None
    try:
        from fastapi import HTTPException

        from ai.provider_factory import get_provider_for_user
        from credits import record_usage
        from credits_middleware import check_credits
        from core.mongo_client import get_shared_mongo_client

        try:
            await check_credits(user_id, get_shared_mongo_client())
        except HTTPException:
            return None  # out of credits: the brief still goes, without the take

        provider = await get_provider_for_user(user_id, db)
        focus = f" Focus on: {tok['opts']['focus']}." if tok["opts"].get("focus") else ""
        uses_emoji = bool(_EMOJI_RE.search(template.get("body") or ""))
        prompt = TAKE_PROMPT.format(cadence=template["frequency"], focus=focus, report=report_text,
                                    emoji_rule="" if uses_emoji else ", and use no emojis")
        response = await provider.chat_completion(
            messages=[{"role": "user", "content": prompt}], tools=None, temperature=0.3, max_tokens=200)
        await record_usage(
            db, user_id, model=getattr(provider, "model", None),
            prompt_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            source="slack", feature="slack_brief_take",
        )
        return (response.content or "").strip()[:600] or None
    except Exception as e:
        logger.warning("slack brief: Birdy's take failed for %s: %s", user_id, e)
        return None


# ── For the copilot and the UI ────────────────────────────────────────────────


def catalog_for(available_sources: set[str] | None = None) -> list[dict]:
    """The metric catalog. An account with nothing linked yet gets no
    availability flags rather than every metric marked unavailable."""
    if not available_sources:
        available_sources = None
    return [{
        "id": mid, "label": meta["label"], "source": SOURCE_LABELS[meta["source"]],
        "has_target": "target" in meta, "lower_is_better": meta.get("lower_is_better"),
        **({"available": all(s in available_sources for s in _sources_of(mid))}
           if available_sources is not None else {}),
    } for mid, meta in CATALOG.items()]


def periods_for_ui() -> list[dict]:
    return [{"id": k, "label": v} for k, v in SECTION_PERIODS.items()]


def sources_connected(groups: list[dict]) -> set[str]:
    out = set()
    for g in groups:
        if g.get("meta_ad_account_id") or g.get("facebook_cache"):
            out.add("meta")
        if g.get("ghl_location_id") or g.get("ghl_opp_cache"):
            out.add("ghl_opp")
        if g.get("hotprospector_group_id") or g.get("hotprospector_call_cache"):
            out.add("hp_call")
    return out


PLACEHOLDER_GUIDE = """Placeholders: {{name key=value key="value with spaces"}}
- {{<metric>}} value for all clients. Options: client="<client name>", period=<yesterday|this_week|last_7d|last_14d|last_30d|this_month|last_month>, show=<value|change|target|vs_target>
  show=change → "▲ 12% vs the week before" (blank on the first report). show=vs_target → "on target (£12.00)". Targets only exist where has_target=true.
- {{ranking metric=<metric> order=<best|worst> limit=<1-10> [period=…]}} → one client inline, or a numbered list on new lines
- {{breakdown metrics="<m1>,<m2>" [clients="<name>,<name>"] [sort=<m1>] [order=worst] [limit=10] [period=…]}} → one bullet per client
- {{alerts}} / {{flagged_ads}} with show=<summary|count|list> [limit=5]
- {{birdy_take focus="…"}} → a short written read of the numbers (at most one)
- {{period}} → "Yesterday"/"Last 7 days", {{date}} → "Mon 13 Oct", {{client_count}}
- Reports sent by a Birdy event only: {{event}} → "Alert: CPL above £40", {{alert_name}}, {{alert_message}}, {{account_name}}, {{metric_name}} (blank in scheduled reports)"""
