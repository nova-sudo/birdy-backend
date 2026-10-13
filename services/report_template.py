"""
services/report_template.py
---------------------------
The Slack report as a template: what the report copilot designs with the user,
what Settings shows, and what the scheduled send renders.

Stored at users.integrations.slack_bot.brief:

    {
      "version": 2,
      "frequency": "daily" | "weekly", "day": "Monday" | None, "time": "9:00 AM",
      "title": "Weekly pulse" | None,
      "sections": [
        {"type": "metric", "metric": "spend", "scope": "portfolio", "compare": "previous"},
        {"type": "metric", "metric": "cpl", "scope": {"client_id": "g1"}, "compare": "target"},
        {"type": "ranking", "metric": "leads", "direction": "best", "limit": 3},
        {"type": "alerts"},
        {"type": "underperforming_ads"},
        {"type": "birdy_take", "focus": "what changed and what to do"},
      ],
    }

Older briefs ({frequency, time, day, items: {spend, leads, ...}}) are converted
on read by `normalize`, so there is one renderer for every account.

Every number is read from the same caches, through the same metric registry,
that the app's pages use. The only model-written text is the optional
"Birdy's take", which is given the rendered lines and nothing else.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime, timedelta

from services.metric_orchestrator import METRIC_REGISTRY, get_metric_value

logger = logging.getLogger(__name__)

VERSION = 2
MAX_SECTIONS = 12
MAX_RANKING = 5
FREQUENCIES = ("daily", "weekly")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SECTION_TYPES = ("metric", "ranking", "alerts", "underperforming_ads", "birdy_take")
COMPARES = ("none", "previous", "target")
DIRECTIONS = ("best", "worst")
LEGACY_ITEM_KEYS = ("spend", "leads", "conversion", "top", "alerts", "underperform")

PERIODS = {
    "daily": {"preset": "yesterday", "label": "Yesterday", "days": 1, "prev": "the day before"},
    "weekly": {"preset": "last_7d", "label": "Last 7 days", "days": 7, "prev": "the week before"},
}

CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "AED": "AED ", "INR": "₹", "ZAR": "R", "JPY": "¥",
    "CHF": "CHF ", "CNY": "¥", "MXN": "MX$", "SAR": "SAR ",
}

# The metrics a report can carry. A curated slice of METRIC_REGISTRY: each one
# has a plain label, the source it needs, which way is good, and, where a
# client target exists for it, how to compare against that target.
#
#   target: (field, kind) — kind "ratio" compares directly, "total" pro-rates
#           a monthly target to the report's window.
#   per_unit: a cost per something, shown to two decimal places.
CATALOG: dict[str, dict] = {
    "spend":           {"label": "Ad spend", "emoji": "💰", "source": "meta", "lower_is_better": None,
                        "target": ("monthly_spend", "total")},
    "leads":           {"label": "Leads", "emoji": "📈", "source": "meta", "lower_is_better": False},
    "cpl":             {"label": "Cost per lead", "emoji": "🏷️", "source": "meta", "lower_is_better": True,
                        "per_unit": True, "target": ("cpl", "ratio")},
    "ctr":             {"label": "Click-through rate", "emoji": "👆", "source": "meta", "lower_is_better": False},
    "cpc":             {"label": "Cost per click", "emoji": "🖱️", "source": "meta", "lower_is_better": True,
                        "per_unit": True},
    "cpm":             {"label": "Cost per 1,000 impressions", "emoji": "👀", "source": "meta",
                        "lower_is_better": True, "per_unit": True},
    "impressions":     {"label": "Impressions", "emoji": "👀", "source": "meta", "lower_is_better": False},
    "clicks":          {"label": "Clicks", "emoji": "🖱️", "source": "meta", "lower_is_better": False},
    "ghl_total_opps":  {"label": "Opportunities", "emoji": "📋", "source": "ghl_opp", "lower_is_better": False},
    "ghl_won_opps":    {"label": "Deals won", "emoji": "🎉", "source": "ghl_opp", "lower_is_better": False,
                        "target": ("monthly_wins", "total")},
    "ghl_revenue":     {"label": "Revenue won", "emoji": "💷", "source": "ghl_opp", "lower_is_better": False,
                        "target": ("monthly_revenue", "total")},
    "ghl_conversion":  {"label": "Close rate", "emoji": "🎯", "source": "ghl_opp", "lower_is_better": False,
                        "target": ("conversion_rate", "ratio_fraction")},
    "hp_total_calls":  {"label": "Calls made", "emoji": "📞", "source": "hp_call", "lower_is_better": False},
    "hp_answer_rate":  {"label": "Answer rate", "emoji": "☎️", "source": "hp_call", "lower_is_better": False},
    "hp_connect_rate": {"label": "Lead connect rate", "emoji": "🔗", "source": "hp_call", "lower_is_better": False},
}

SOURCE_LABELS = {"meta": "Meta Ads", "ghl_opp": "CRM pipeline", "hp_call": "Call centre"}


class TemplateError(ValueError):
    """A template the copilot or a client sent that cannot be stored."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ── Shape ─────────────────────────────────────────────────────────────────────


def default_template(frequency: str = "daily", time: str = "9:00 AM", day: str | None = None) -> dict:
    """What a user gets if they skip the copilot: the old recommended set."""
    return {
        "version": VERSION,
        "frequency": frequency,
        "time": time,
        "day": (day or "Monday") if frequency == "weekly" else None,
        "title": None,
        "sections": [
            {"type": "metric", "metric": "spend", "scope": "portfolio", "compare": "previous"},
            {"type": "metric", "metric": "leads", "scope": "portfolio", "compare": "previous"},
            {"type": "metric", "metric": "cpl", "scope": "portfolio", "compare": "previous"},
            {"type": "metric", "metric": "ghl_conversion", "scope": "portfolio", "compare": "none"},
            {"type": "ranking", "metric": "leads", "direction": "best", "limit": 1},
            {"type": "alerts"},
        ],
    }


def from_legacy(brief: dict) -> dict:
    """The six-checkbox brief as a v2 template that says the same things."""
    items = brief.get("items") or {}
    sections: list[dict] = []
    if items.get("spend"):
        sections.append({"type": "metric", "metric": "spend", "scope": "portfolio", "compare": "none"})
    if items.get("leads"):
        sections.append({"type": "metric", "metric": "leads", "scope": "portfolio", "compare": "none"})
        sections.append({"type": "metric", "metric": "cpl", "scope": "portfolio", "compare": "none"})
    if items.get("conversion"):
        sections.append({"type": "metric", "metric": "ghl_conversion", "scope": "portfolio", "compare": "none"})
    if items.get("top"):
        sections.append({"type": "ranking", "metric": "leads", "direction": "best", "limit": 1})
    if items.get("alerts"):
        sections.append({"type": "alerts"})
    if items.get("underperform"):
        sections.append({"type": "underperforming_ads"})
    frequency = brief.get("frequency") if brief.get("frequency") in FREQUENCIES else None
    return {
        "version": VERSION,
        "frequency": frequency,
        "time": brief.get("time") or "9:00 AM",
        "day": (brief.get("day") or "Monday") if frequency == "weekly" else None,
        "title": None,
        "sections": sections,
    }


def normalize(brief: dict | None) -> dict | None:
    """Any stored brief as a v2 template, or None when there is none."""
    if not brief:
        return None
    if brief.get("version") == VERSION:
        return brief
    return from_legacy(brief)


def _clean_time(raw) -> str:
    from services.slack_brief import parse_time
    t = parse_time(raw if isinstance(raw, str) else None)
    return datetime.combine(date.today(), t).strftime("%-I:%M %p")


def validate(raw: dict, client_ids: set[str] | None = None) -> dict:
    """
    A clean v2 template, or TemplateError listing everything wrong with it.

    `client_ids` is the user's own clients; a client-scoped section naming any
    other id is rejected. Pass None to skip that check (unit tests only).
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

    title = raw.get("title")
    title = title.strip()[:60] if isinstance(title, str) and title.strip() else None

    sections_in = raw.get("sections")
    if not isinstance(sections_in, list) or not sections_in:
        errors.append("sections must be a non-empty list")
        sections_in = []
    if len(sections_in) > MAX_SECTIONS:
        errors.append(f"at most {MAX_SECTIONS} sections")

    sections: list[dict] = []
    take = None
    for i, s in enumerate(sections_in[:MAX_SECTIONS]):
        where = f"section {i + 1}"
        if not isinstance(s, dict):
            errors.append(f"{where}: must be an object")
            continue
        kind = s.get("type")
        if kind not in SECTION_TYPES:
            errors.append(f"{where}: type must be one of {', '.join(SECTION_TYPES)}")
            continue

        if kind == "metric":
            metric = s.get("metric")
            if metric not in CATALOG:
                errors.append(f"{where}: unknown metric {metric!r}; use list_report_metrics")
                continue
            scope = s.get("scope") or "portfolio"
            if isinstance(scope, dict):
                cid = scope.get("client_id")
                if not isinstance(cid, str) or not cid:
                    errors.append(f"{where}: scope.client_id is required")
                    continue
                if client_ids is not None and cid not in client_ids:
                    errors.append(f"{where}: client {cid!r} is not one of this account's clients")
                    continue
                scope = {"client_id": cid}
            elif scope != "portfolio":
                errors.append(f"{where}: scope must be 'portfolio' or {{'client_id': ...}}")
                continue
            compare = s.get("compare") or "none"
            if compare not in COMPARES:
                errors.append(f"{where}: compare must be one of {', '.join(COMPARES)}")
                continue
            if compare == "target" and "target" not in CATALOG[metric]:
                compare = "none"  # no stored target exists for this metric
            sections.append({"type": "metric", "metric": metric, "scope": scope, "compare": compare})

        elif kind == "ranking":
            metric = s.get("metric")
            if metric not in CATALOG:
                errors.append(f"{where}: unknown metric {metric!r}; use list_report_metrics")
                continue
            direction = s.get("direction") or "best"
            if direction not in DIRECTIONS:
                errors.append(f"{where}: direction must be 'best' or 'worst'")
                continue
            try:
                limit = int(s.get("limit") or 3)
            except (TypeError, ValueError):
                limit = 3
            sections.append({"type": "ranking", "metric": metric, "direction": direction,
                             "limit": max(1, min(MAX_RANKING, limit))})

        elif kind == "birdy_take":
            if take is not None:
                errors.append(f"{where}: only one birdy_take section is allowed")
                continue
            focus = s.get("focus")
            take = {"type": "birdy_take",
                    "focus": focus.strip()[:140] if isinstance(focus, str) and focus.strip() else None}

        else:
            sections.append({"type": kind})

    if take:
        sections.append(take)  # always last: it reads everything above it

    if errors:
        raise TemplateError(errors)

    return {
        "version": VERSION,
        "frequency": frequency,
        "time": _clean_time(raw.get("time")),
        "day": day,
        "title": title,
        "sections": sections,
    }


def has_content(template: dict | None) -> bool:
    return bool(template and template.get("frequency") in FREQUENCIES and template.get("sections"))


# ── Figures ───────────────────────────────────────────────────────────────────


def _window_view(group: dict, preset: str) -> dict:
    """
    The group with only this window's caches left in it.

    The registry's readers fall back to lifetime ("maximum") or legacy flat
    stats when a window is missing — right for a dashboard, wrong for a report
    that says "yesterday". Reading from this view, a missing window reads as 0.
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


def _has_source(view: dict, source: str, preset: str) -> bool:
    if source == "meta":
        return "facebook_cache" in view
    if source == "ghl_opp":
        return "ghl_opp_cache" in view
    if source == "hp_call":
        return "hotprospector_call_cache" in view
    return False


def _value(metric: str, views: list[dict], preset: str) -> float | None:
    """The metric over these clients, or None when none of them hold the window."""
    source = CATALOG[metric]["source"]
    views = [v for v in views if _has_source(v, source, preset)]
    if not views:
        return None
    entry = METRIC_REGISTRY[metric]
    if entry.get("aggregation") == "recompute":
        num_id, den_id = entry["components"]
        num = sum(get_metric_value(num_id, v, preset) for v in views)
        den = sum(get_metric_value(den_id, v, preset) for v in views)
        return (num / den) * entry.get("scale", 1) if den else None
    return sum(get_metric_value(metric, v, preset) for v in views)


def _target(metric: str, groups: list[dict], days: int) -> float | None:
    """The target this window should be held to, from the clients' monthly goals."""
    spec = CATALOG[metric].get("target")
    if not spec:
        return None
    field, kind = spec
    values = []
    for g in groups:
        v = (g.get("targets") or {}).get(field)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            values.append(float(v))
    if not values:
        return None
    if kind == "total":
        return sum(values) * days / 30.0
    if len(groups) != 1:
        return None  # a ratio target belongs to one client; a portfolio has no single one
    return values[0] * 100 if kind == "ratio_fraction" else values[0]


def section_key(section: dict) -> str | None:
    """Stable id for a metric section, used to match it with last period's figure."""
    if section.get("type") != "metric":
        return None
    scope = section.get("scope")
    where = scope.get("client_id") if isinstance(scope, dict) else "portfolio"
    return f"{section['metric']}:{where}"


async def gather(db, user_id: str, template: dict, now_utc: datetime) -> dict:
    """Everything the renderer needs for this template. Pure reads."""
    period = PERIODS[template["frequency"]]
    preset = period["preset"]

    # Only this window's slice of each cache. The caches hold every preset
    # (and, for Meta, campaign/ad breakdowns), so reading them whole made a
    # preview for a large account move megabytes for a handful of numbers.
    groups = await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        {"_id": 0, "id": 1, "name": 1, "targets": 1,
         f"facebook_cache.presets.{preset}.metrics.insights": 1,
         f"facebook_cache.{preset}.metrics.insights": 1,
         f"ghl_opp_cache.{preset}": 1,
         f"hotprospector_call_cache.{preset}": 1},
    ).to_list(length=1000)
    user = await db["users"].find_one({"user_id": user_id}, {"default_currency": 1}) or {}

    views = {g["id"]: _window_view(g, preset) for g in groups if g.get("id")}
    by_id = {g["id"]: g for g in groups if g.get("id")}

    results: list[dict] = []
    for s in template.get("sections") or []:
        kind = s["type"]
        if kind == "metric":
            scope = s.get("scope")
            if isinstance(scope, dict):
                g = by_id.get(scope["client_id"])
                if not g:
                    results.append({**s, "missing_client": True})
                    continue
                scope_groups, scope_name = [g], g.get("name") or "Client"
            else:
                scope_groups, scope_name = groups, None
            value = _value(s["metric"], [views[g["id"]] for g in scope_groups], preset)
            target = _target(s["metric"], scope_groups, period["days"]) if s.get("compare") == "target" else None
            results.append({**s, "value": value, "target": target, "scope_name": scope_name,
                            "key": section_key(s)})

        elif kind == "ranking":
            rows = []
            for gid, view in views.items():
                v = _value(s["metric"], [view], preset)
                if v:  # no data and zero both mean "nothing to rank on"
                    rows.append({"name": by_id[gid].get("name") or "Client", "value": v})
            lower_better = CATALOG[s["metric"]].get("lower_is_better")
            best_first = sorted(rows, key=lambda r: r["value"], reverse=not lower_better)
            ordered = best_first if s["direction"] == "best" else list(reversed(best_first))
            results.append({**s, "rows": ordered[: s["limit"]]})

        elif kind == "alerts":
            since = (now_utc - timedelta(days=period["days"])).replace(tzinfo=None)
            alerts = await db["alert_notifications"].find(
                {"user_id": user_id, "triggered_at": {"$gte": since}},
                {"_id": 0, "message": 1},
            ).sort("triggered_at", -1).to_list(length=50)
            results.append({**s, "alerts": alerts})

        elif kind == "underperforming_ads":
            flagged = await db["ai_suggestions"].find(
                {"user_id": user_id, "status": "open", "action.type": "pause_ads"},
                {"_id": 0, "title": 1, "client_name": 1},
            ).to_list(length=50)
            results.append({**s, "flagged": flagged})

        else:
            results.append(dict(s))

    return {
        "label": period["label"],
        "prev_label": period["prev"],
        "currency": user.get("default_currency") or "GBP",
        "clients": len(groups),
        "sections": results,
    }


# ── Rendering ─────────────────────────────────────────────────────────────────


def money(amount: float, currency: str, pence: bool = False) -> str:
    symbol = CURRENCY_SYMBOLS.get((currency or "").upper(), f"{(currency or '').upper()} ")
    return f"{symbol}{amount:,.2f}" if pence else f"{symbol}{amount:,.0f}"


def fmt(metric: str, value: float, currency: str) -> str:
    kind = METRIC_REGISTRY[metric].get("format")
    if kind == "currency":
        return money(value, currency, pence=bool(CATALOG[metric].get("per_unit")))
    if kind == "percentage":
        return f"{value:.1f}%"
    if kind == "decimal":
        return f"{value:,.2f}"
    return f"{value:,.0f}"


def _change(metric: str, now: float, before: float | None, prev_label: str) -> str:
    if before is None or before == 0:
        return ""
    pct = (now - before) / abs(before) * 100
    if abs(pct) < 0.5:
        return f" · flat vs {prev_label}"
    arrow = "▲" if pct > 0 else "▼"
    good = CATALOG[metric].get("lower_is_better")
    mark = "" if good is None else (" ✅" if (pct < 0) == good else " ⚠️")
    return f" · {arrow} {abs(pct):.0f}% vs {prev_label}{mark}"


def _vs_target(metric: str, value: float, target: float, currency: str) -> str:
    lower_better = CATALOG[metric].get("lower_is_better")
    shown = fmt(metric, target, currency)
    if lower_better is None:  # spend: neither side of the target is "good"
        pct = (value - target) / target * 100
        return f" · {abs(pct):.0f}% {'over' if pct > 0 else 'under'} the {shown} pace"
    hit = value <= target if lower_better else value >= target
    return f" · {'✅ on' if hit else '⚠️ off'} target ({shown})"


def section_lines(data: dict, previous: dict | None = None) -> list[str]:
    """One mrkdwn line (or a few, for rankings) per section, take excluded."""
    cur = data["currency"]
    prev_values = (previous or {}).get("values") or {}
    lines: list[str] = []
    for s in data["sections"]:
        kind = s["type"]
        if kind == "metric":
            meta = CATALOG[s["metric"]]
            label = meta["label"] + (f" · {s['scope_name']}" if s.get("scope_name") else "")
            if s.get("missing_client"):
                continue  # client archived since the template was made
            if s.get("value") is None:
                lines.append(f"{meta['emoji']} *{label}:* no data for this period")
                continue
            line = f"{meta['emoji']} *{label}:* {fmt(s['metric'], s['value'], cur)}"
            if s.get("compare") == "previous":
                line += _change(s["metric"], s["value"], prev_values.get(s["key"]), data["prev_label"])
            elif s.get("compare") == "target" and s.get("target"):
                line += _vs_target(s["metric"], s["value"], s["target"], cur)
            lines.append(line)

        elif kind == "ranking":
            meta = CATALOG[s["metric"]]
            head = f"{'🏆 Top' if s['direction'] == 'best' else '🔻 Bottom'} {s['limit'] if s['limit'] > 1 else ''}".rstrip()
            title = f"{head} by {meta['label'].lower()}"
            rows = s.get("rows") or []
            if not rows:
                lines.append(f"*{title}:* no client has data for this period")
            elif len(rows) == 1:
                lines.append(f"*{title}:* {rows[0]['name']} ({fmt(s['metric'], rows[0]['value'], cur)})")
            else:
                listed = "\n".join(f"   {i + 1}. {r['name']} · {fmt(s['metric'], r['value'], cur)}"
                                   for i, r in enumerate(rows))
                lines.append(f"*{title}:*\n{listed}")

        elif kind == "alerts":
            alerts = s.get("alerts") or []
            if alerts:
                first = alerts[0].get("message") or ""
                more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
                lines.append(f"🔔 *Alerts:* {len(alerts)} triggered · {first}{more}")
            else:
                lines.append("🔔 *Alerts:* none triggered")

        elif kind == "underperforming_ads":
            flagged = s.get("flagged") or []
            if flagged:
                names = ", ".join((f.get("title") or "").split("—")[-1].strip() for f in flagged[:3])
                more = f" +{len(flagged) - 3} more" if len(flagged) > 3 else ""
                lines.append(f"⚠️ *Underperforming ads:* {len(flagged)} flagged · {names}{more}")
            else:
                lines.append("⚠️ *Underperforming ads:* none flagged")
    return lines


def snapshot(data: dict, period_key: str, frequency: str) -> dict:
    """This send's metric figures, for the next send to compare against."""
    values = {s["key"]: s["value"] for s in data["sections"]
              if s["type"] == "metric" and s.get("key") and s.get("value") is not None}
    return {"period": period_key, "frequency": frequency, "values": values}


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


def has_take(template: dict) -> dict | None:
    return next((s for s in template.get("sections") or [] if s["type"] == "birdy_take"), None)


def render(template: dict, data: dict, previous: dict | None = None,
           take: str | None = None, take_placeholder: str | None = None) -> tuple[list[dict], str]:
    """Slack blocks + fallback text."""
    lines = section_lines(data, previous)
    name = template.get("title") or f"Birdy {'daily' if template['frequency'] == 'daily' else 'weekly'} brief"
    heading = f"{name} · {data['label']}"
    body = "\n".join(lines) or "_Nothing to report for this period._"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": heading[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
    ]
    take_text = take or take_placeholder
    if take_text:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"🐦 *Birdy's take:* {take_text}"[:2900]}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": f"{data['clients']} active client{'s' if data['clients'] != 1 else ''} · change this report in Birdy → Settings"}]})

    text = f"{heading}\n{body}" + (f"\n\nBirdy's take: {take}" if take else "")
    return blocks, text


# ── Birdy's take ──────────────────────────────────────────────────────────────

TAKE_PROMPT = (
    "You are Birdy, an analyst for a marketing agency. Below is the agency's {cadence} Slack report. "
    "Write Birdy's take: at most 60 words, plain sentences, no bullet points, no greeting. "
    "Say what matters most and one concrete thing to do. Use ONLY the figures in the report; "
    "never introduce a number that is not in it. If the report has no data, say so in one sentence."
    "{focus}\n\nReport:\n{report}"
)


async def write_take(db, user_id: str, template: dict, lines: list[str]) -> str | None:
    """The model-written paragraph, or None when it can't or shouldn't run."""
    section = has_take(template)
    if not section or not lines:
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
        focus = f" Focus on: {section['focus']}." if section.get("focus") else ""
        prompt = TAKE_PROMPT.format(
            cadence=template["frequency"], focus=focus, report="\n".join(lines))
        response = await provider.chat_completion(
            messages=[{"role": "user", "content": prompt}], tools=None, temperature=0.3, max_tokens=200)
        await record_usage(
            db, user_id, model=getattr(provider, "model", None),
            prompt_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(response.usage, "output_tokens", 0) or 0,
            source="slack", feature="slack_brief_take",
        )
        text = (response.content or "").strip()
        return text[:600] or None
    except Exception as e:
        logger.warning("slack brief: Birdy's take failed for %s: %s", user_id, e)
        return None


# ── For the copilot and the UI ────────────────────────────────────────────────


def catalog_for(available_sources: set[str] | None = None) -> list[dict]:
    """The metric catalog. With `available_sources`, each row says whether the
    account has that data. An account with nothing linked yet (mid-onboarding,
    before the first client's ids are stored) gets no availability flags rather
    than every metric marked unavailable, which would leave nothing to offer."""
    if not available_sources:
        available_sources = None
    rows = []
    for mid, meta in CATALOG.items():
        rows.append({
            "id": mid,
            "label": meta["label"],
            "source": SOURCE_LABELS[meta["source"]],
            "has_target": "target" in meta,
            "lower_is_better": meta.get("lower_is_better"),
            **({"available": meta["source"] in available_sources} if available_sources is not None else {}),
        })
    return rows


def sources_connected(groups: list[dict]) -> set[str]:
    """Which data sources at least one client is connected to."""
    out = set()
    for g in groups:
        if g.get("meta_ad_account_id") or g.get("facebook_cache"):
            out.add("meta")
        if g.get("ghl_location_id") or g.get("ghl_opp_cache"):
            out.add("ghl_opp")
        if g.get("hotprospector_group_id") or g.get("hotprospector_call_cache"):
            out.add("hp_call")
    return out


def sample_data(template: dict, currency: str, client_names: dict[str, str]) -> dict:
    """Plausible figures for a preview when nothing has synced yet. Labelled as such by the caller."""
    samples = {"spend": 142.0, "leads": 18.0, "cpl": 7.89, "ctr": 1.9, "cpc": 0.84, "cpm": 11.2,
               "impressions": 12750.0, "clicks": 169.0, "ghl_total_opps": 14.0, "ghl_won_opps": 3.0,
               "ghl_revenue": 2400.0, "ghl_conversion": 21.4, "hp_total_calls": 46.0,
               "hp_answer_rate": 63.0, "hp_connect_rate": 72.0}
    names = list(client_names.values()) or ["Your first client"]
    period = PERIODS[template["frequency"]]
    sections = []
    for s in copy.deepcopy(template["sections"]):
        if s["type"] == "metric":
            scope = s.get("scope")
            s["scope_name"] = client_names.get(scope["client_id"], "Client") if isinstance(scope, dict) else None
            s["value"] = samples[s["metric"]]
            s["target"] = samples[s["metric"]] * 1.1 if s.get("compare") == "target" else None
            s["key"] = section_key(s)
        elif s["type"] == "ranking":
            base = samples[s["metric"]]
            s["rows"] = [{"name": n, "value": base * (1 - 0.25 * i)} for i, n in enumerate(names[: s["limit"]])]
        elif s["type"] == "alerts":
            s["alerts"] = [{"message": f"[{names[0]}] Cost per lead above target"}]
        elif s["type"] == "underperforming_ads":
            s["flagged"] = [{"title": "Pause underperforming ad — Spring offer"}]
        sections.append(s)
    return {"label": period["label"], "prev_label": period["prev"], "currency": currency,
            "clients": max(1, len(client_names)), "sections": sections}
