"""
services/report_owner.py
------------------------
Which agent a Slack report belongs to, read from what it shows (the owner's rule):

- Agency Ops (Birdy, "birdy"): top-level, client-level reports only: a client's or the agency's spend,
  leads, CPL, opportunities, deals won, revenue, alerts. Account and client totals.
- Marketing Guru ("media_buyer"): anything at campaign, ad set, ad or creative level.
- Sales Coach ("sales_coach"): anything about calls: booked, closed, speed to lead, call-centre stats,
  recordings, setters.

`report_area` reads a template (raw, as an agent sends it, or stored) and the report's name, and
returns the owning area with the reasons. It is pure (no database), so a tool can refuse a report
before it shows any preview or asks the owner to confirm anything.

Marketing Guru and Sales Coach may put client-level totals beside their own figures (an ads report
with the client's spend and leads is still an ads report), so client-level content is allowed for
every agent. A report that mixes Marketing Guru's area with Sales Coach's is refused for every
agent ("mixed"): a report has one owner, so it is split into one report per agent.

Only agent chat runs are held to this (ai/mcp/slack_reports_mcp.py, ai/team/delegation.py). The
owner in the report builder (routers/slack_reports.py) and an outside assistant pick any owner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from services import report_template as core

BIRDY, MEDIA_BUYER, SALES_COACH = "birdy", "media_buyer", "sales_coach"
MIXED = "mixed"

NAMES = {BIRDY: "Agency Ops (Birdy)", MEDIA_BUYER: "Marketing Guru", SALES_COACH: "Sales Coach"}
ASK = {BIRDY: "ask_birdy", MEDIA_BUYER: "ask_media_buyer", SALES_COACH: "ask_sales_coach"}
AREA = {MEDIA_BUYER: "campaign, ad set, ad or creative level", SALES_COACH: "calls"}

# Levels below the client. The report data has none today (birdy_data `by` is client or day), so a
# template asking for one would be cut back to client totals by validation: it is Marketing Guru's.
LEVELS = frozenset({"campaign", "campaigns", "adset", "adsets", "ad_set", "ad_sets", "ad set", "ad",
                    "ads", "creative", "creatives"})
AD_TOKENS = frozenset({"flagged_ads"})          # the open "pause these ads" suggestions, by ad
CALL_SOURCES = frozenset({"sales_coach", "hp_call"})
CALL_METRICS = frozenset(m for m, meta in core.CATALOG.items() if meta["source"] in CALL_SOURCES)

_ADS_WORDS = re.compile(
    r"\b(campaigns?|ad[\s_-]?sets?|creatives?|ad[\s-]level|per[\s-]ad|by[\s-]ad|each[\s-]ad|"
    r"(?:top|best|worst|winning|losing|underperforming)[\s-]ads?|marketing[\s-]guru|media[\s-]buyer)\b", re.I)
_CALL_WORDS = re.compile(
    r"\b(calls?|call[\s-]?(?:centre|center|stats?)|setters?|recordings?|speed[\s-]to[\s-]lead|dials?|"
    r"sales[\s-]coach|sales[\s-]team)\b", re.I)
# Block fields that are never words the owner reads.
_NOT_TEXT = frozenset({"type", "style", "url", "image_url", "action_id", "block_id", "value", "birdy_data"})


@dataclass(frozen=True)
class ReportArea:
    owner: str                                  # birdy | media_buyer | sales_coach | mixed
    ads: tuple[str, ...] = field(default=())    # why it is Marketing Guru's
    calls: tuple[str, ...] = field(default=())  # why it is Sales Coach's


def _blocks(template) -> list:
    if not isinstance(template, dict):
        return []
    blocks = template.get("blocks")
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            blocks = []
    out = list(blocks) if isinstance(blocks, list) else []
    if isinstance(template.get("body"), str):    # free-form text still works (report_blocks.validate)
        out.append({"type": "section", "text": template["body"]})
    return out


def _walk(node, texts: list, bindings: list, key=None):
    if isinstance(node, dict):
        if isinstance(node.get("birdy_data"), dict):
            bindings.append(node["birdy_data"])
        for k, v in node.items():
            if k not in _NOT_TEXT:
                _walk(v, texts, bindings, k)
    elif isinstance(node, list):
        for v in node:
            _walk(v, texts, bindings, key)
    elif isinstance(node, str):
        texts.append(node)


def _level(value) -> str | None:
    v = str(value or "").strip().lower()
    return v if v in LEVELS else None


def _metrics(value) -> list[str]:
    if isinstance(value, str):
        return [m.strip() for m in value.split(",") if m.strip()]
    return [str(m) for m in value] if isinstance(value, list) else []


def report_area(template: dict | None, name: str | None = None) -> ReportArea:
    """The area a report's content belongs to, with the reasons (see the module doc)."""
    texts, bindings = [], []
    _walk(_blocks(template), texts, bindings)
    ads, calls = [], []

    def add(bucket, why):
        if why not in bucket:
            bucket.append(why)

    words = [name] if name else []
    for s in texts:
        tokens = core.parse_tokens(s)
        for tok in tokens:
            opts = tok["opts"]
            if tok["name"] in AD_TOKENS:
                add(ads, f"{{{{{tok['name']}}}}} (ads)")
            for k in ("level", "by"):
                if _level(opts.get(k)):
                    add(ads, f"{tok['raw']} ({_level(opts.get(k))} level)")
            used = [tok["name"], opts.get("metric"), *_metrics(opts.get("metrics"))]
            for m in used:
                if m in CALL_METRICS:
                    add(calls, f"{m} (calls)")
        words.append(core._TOKEN_RE.sub(" ", s))
    for bd in bindings:
        for k in ("level", "by", "split"):
            if _level(bd.get(k)):
                add(ads, f"birdy_data {k}={_level(bd.get(k))}")
        for m in [*_metrics(bd.get("metrics")), bd.get("metric"), bd.get("sort")]:
            if m in CALL_METRICS:
                add(calls, f"{m} (calls)")
    text = "\n".join(w for w in words if w)
    for m in _ADS_WORDS.finditer(text):
        add(ads, f"\"{m.group(0)}\"")
    for m in _CALL_WORDS.finditer(text):
        add(calls, f"\"{m.group(0)}\"")

    owner = MIXED if ads and calls else MEDIA_BUYER if ads else SALES_COACH if calls else BIRDY
    return ReportArea(owner=owner, ads=tuple(ads), calls=tuple(calls))


def report_owner_for(template: dict | None, name: str | None = None) -> str:
    """birdy | media_buyer | sales_coach, or "mixed" when it covers both Marketing Guru's and Sales Coach's areas."""
    return report_area(template, name).owner


def _why(reasons) -> str:
    return ", ".join(reasons[:4])


def refusal(caller: str, area: ReportArea) -> str | None:
    """Why `caller` (an agent) may not make this report, with exactly what to do instead; None when it may.
    Client-level content is every agent's; campaign/ad/creative level Marketing Guru's; calls Sales Coach's."""
    if area.owner == BIRDY or area.owner == caller:
        return None
    keep = ("Don't make a client-level version of it yourself instead, and don't reword it to get past this check.")
    if area.owner == MIXED:
        parts = {MEDIA_BUYER: area.ads, SALES_COACH: area.calls}
        if caller == BIRDY:
            return (f"This report mixes Marketing Guru's area ({AREA[MEDIA_BUYER]}: {_why(area.ads)}) and Sales "
                    f"Coach's ({AREA[SALES_COACH]}: {_why(area.calls)}), so it can't be yours and a report has one "
                    "owner. Split it: ask Marketing Guru with ask_media_buyer for the ads part and Sales Coach with "
                    "ask_sales_coach for the calls part, passing on everything the owner said (what to show, when, "
                    "which channel). Each proposes its report for the owner to approve. Client-level totals "
                    "(spend, leads, CPL, opportunities, deals won) can stay in a report of your own. " + keep)
        other = SALES_COACH if caller == MEDIA_BUYER else MEDIA_BUYER
        return (f"This report mixes your area with {NAMES[other]}'s ({AREA[other]}: {_why(parts[other])}), and a "
                f"report has one owner. Keep only your own part (client-level totals can stay) and leave out the "
                f"{AREA[other]} part; for that part ask Birdy with ask_birdy to pass it to {NAMES[other]}, or tell "
                f"the owner it is quickest from {NAMES[other]}'s own chat.")
    owner, reasons = area.owner, (area.ads if area.owner == MEDIA_BUYER else area.calls)
    head = (f"This is a {NAMES[owner]} report ({AREA[owner]}: {_why(reasons)}), so only {NAMES[owner]} can "
            f"create or change it; nothing was saved.")
    if caller == BIRDY:
        return (f"{head} You make only client-level reports (a client's or the agency's spend, leads, CPL, "
                f"opportunities, deals won). Ask {NAMES[owner]} with {ASK[owner]}: pass on everything the owner "
                f"said (the report's name, what to show, when it sends, which channel). {NAMES[owner]} proposes the "
                f"report, and once the owner approves its card it makes it as its own report. Tell the owner it is "
                f"waiting for their approval, or that it is quickest from {NAMES[owner]}'s own chat. {keep}")
    return (f"{head} Leave out the {AREA[owner]} part (client-level totals can stay), or ask Birdy with ask_birdy "
            f"to pass the request to {NAMES[owner]}, or tell the owner it is quickest from {NAMES[owner]}'s own chat.")
