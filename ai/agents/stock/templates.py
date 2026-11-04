"""
ai/agents/stock/templates.py
----------------------------
The stock agent's Slack messages as editable Block Kit templates (spec section 8): the card, the
summary and the weekly message. They use the Slack Reports engine (services/report_blocks.py), so an
agency can use any block a channel message accepts, plus the agent placeholders below and the
ordinary report ones such as {{spend}}.

Buttons are never part of a template. `card_buttons` and `summary_buttons` build them from the card's
or run's state, and `assemble` puts them last, so an edited layout can't remove or change a decision.
Button values are ids only: Slack's handler (Task 15) re-checks the owner and the state on the server.

Values that come from AI text, ad names or client names are escaped at render, whatever the writer
stripped: `render_kind` hands them to the engine as agent values, which it escapes in full. Only
`{{tag}}`, built here from a validated Slack user id, stays a mention. The client table is a
`birdy_data: {"source": "agent_run"}` binding on a `table` block. It is kept in the stored template,
and `render_kind` swaps it for a normal table of the run's own rows before the engine sees it.
"""

import copy
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import services.report_blocks as rb
import services.report_template as rt
from ai.agents.outcomes import UNDO_AUTO_WINDOW
from ai.agents.stock.cards import (STATE_APPLIED, STATE_APPLYING, STATE_PROPOSED, STATE_QUEUED, STATE_UNDOING,
                                   STATUS_APPLIED, STATUS_CLOSING, STATUS_DISMISSED, STATUS_FLAGGED, STATUS_OPEN,
                                   STATUS_PARTLY_APPLIED)
from ai.agents.stock.settings import TEMPLATE_KINDS
from ai.agents.stock.writer import _fmt_money
from services.report_template import TemplateError

__all__ = ["render_kind_safe", "report_url", "DEFAULT_TEMPLATES", "TemplateError", "KIND_TOKENS", "validate_template", "render_kind", "make_context",
           "card_buttons", "summary_buttons", "assemble", "card_values", "summary_values", "rows_from_cards"]

logger = logging.getLogger(__name__)

KIND_TOKENS = {
    "card": ("client", "window", "previous_cpl", "current_cpl", "zone", "changes", "additional_changes",
             "thought_process", "next_steps", "follow_ups"),
    "summary": ("date", "batch", "zone_counts", "alerts", "cards_waiting", "tag"),
    "weekly": ("week", "comparison", "improved", "decreased", "status_check", "wins"),
}
FREQUENCY = {"card": "daily", "summary": "daily", "weekly": "weekly"}
BINDING = {"source": "agent_run"}
TABLE_COLUMNS = {"previous_cpl": "Previous CPL", "current_cpl": "Current CPL", "zone": "Zone"}
TABLE_BLOCKS = ("table", "data_table")
RESERVED_PREFIX = "agent_"          # action ids Birdy's own buttons use
ACTIONS_RESERVED = 1                # the room `assemble` keeps for the buttons block
BUSY_STATES = (STATE_QUEUED, STATE_APPLYING, STATE_UNDOING)
UNDOABLE_KINDS = ("remove", "revive", "add")   # apply.WRITABLE_KINDS; not imported to keep apply out of the import graph
ZONE_LABELS = {"green": "🟢 Green", "yellow": "🟡 Yellow", "red": "🔴 Red", "no_data": "⚪ No data"}
REPORT_PATH = "/hub/agents/media-buying"
# Slack's table limits (docs.slack.dev/reference/block-kit/blocks/table-block): 100 rows, 10,000 characters
# across all cells. The engine's data_table allows 200 rows and 20,000.
TABLE_LIMITS = {"table": (99, rb.MAX_TABLE_CHARS), "data_table": (200, rb.MAX_DATA_TABLE_CHARS)}
MAX_CELL = 200


def report_url(frontend_url: str, run_id) -> str:
    return f"{frontend_url.rstrip('/')}{REPORT_PATH}?run={run_id}"


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


DEFAULT_TEMPLATES: dict[str, dict] = {
    "card": {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "{{client}}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "{{window}}"}]},
        _section("*CPL* {{previous_cpl}} → {{current_cpl}}     *Zone* {{zone}}"),
        _section("*Changes*\n{{changes}}"),
        _section("*Additional changes*\n{{additional_changes}}"),
        _section("*Thought process*\n{{thought_process}}"),
        _section("*Next steps*\n{{next_steps}}"),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "*Follow-ups:* {{follow_ups}}"}]},
    ]},
    "summary": {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Birdy Media Buying · {{date}}", "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "{{batch}} · {{zone_counts}}"}]},
        _section("*Waiting for you:* {{cards_waiting}} {{tag}}"),
        _section("*Alerts*\n{{alerts}}"),
        {"type": "table", "birdy_data": dict(BINDING)},
    ]},
    "weekly": {"blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": "Weekly report · {{week}}", "emoji": True}},
        _section("*Comparison*\n{{comparison}}"),
        _section("*Improved*\n{{improved}}"),
        _section("*Decreased*\n{{decreased}}"),
        _section("*Status check*\n{{status_check}}"),
        _section("*Wins*\n{{wins}}"),
    ]},
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _kind(kind: str) -> str:
    if kind not in TEMPLATE_KINDS:
        raise TemplateError([f"unknown template {kind!r}; the templates are {', '.join(TEMPLATE_KINDS)}"])
    return kind


def _reserved_ids(node, errors: list[str], path="blocks") -> None:
    if isinstance(node, dict):
        aid = node.get("action_id")
        if isinstance(aid, str) and aid.startswith(RESERVED_PREFIX):
            errors.append(f"{path}: action ids starting with {RESERVED_PREFIX!r} are Birdy's own; "
                          "buttons in a template can only open a link")
        for k, v in node.items():
            _reserved_ids(v, errors, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reserved_ids(v, errors, f"{path}[{i}]")


def _blocks_of(raw) -> list:
    blocks = raw.get("blocks") if isinstance(raw, dict) else None
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError:
            raise TemplateError(["blocks must be a JSON array of Block Kit blocks"])
    if not isinstance(blocks, list) or not blocks:
        raise TemplateError(["blocks must be a non-empty list"])
    return blocks


def _binding_columns(bd, where: str, errors: list[str]) -> list[str] | None:
    if not isinstance(bd, dict) or bd.get("source") != BINDING["source"] or set(bd) - {"source", "columns"}:
        errors.append(f"{where}: the client table is {{\"source\": \"agent_run\"}}, optionally with \"columns\"")
        return None
    cols = bd.get("columns")
    if cols is None:
        return list(TABLE_COLUMNS)
    if not isinstance(cols, list) or not cols or any(c not in TABLE_COLUMNS for c in cols):
        errors.append(f"{where}: columns are some of {', '.join(TABLE_COLUMNS)}")
        return None
    return list(dict.fromkeys(cols))


METRIC_BLOCKS = ("data_visualization", "carousel")


def report_content_errors(kind: str, blocks: list) -> list[str]:
    """What in these blocks needs the user's report figures, which an agent message doesn't have: charts,
    carousels and tables bound to metrics, and report placeholders such as {{spend}}. They would preview
    with sample figures and post blank, so validation and sending both refuse them."""
    own = ", ".join("{{%s}}" % t for t in KIND_TOKENS[kind])
    errors = []
    for i, b in enumerate(blocks):
        bd = b.get("birdy_data") if isinstance(b, dict) else None
        if isinstance(b, dict) and "birdy_data" in b and (not isinstance(bd, dict) or not bd):
            errors.append(f"block {i + 1}: birdy_data is malformed; the client table is {{\"source\": \"agent_run\"}}")
        elif isinstance(b, dict) and (b.get("type") in METRIC_BLOCKS or (isinstance(bd, dict) and bd.get("source") != BINDING["source"])):
            errors.append(f"block {i + 1}: charts, carousels and tables of report figures aren't available in the {kind} "
                          f"message, which carries no report data; use text with {own}"
                          + (" or the client table" if kind == "summary" else ""))
    try:
        tokens = rb.all_tokens([b for b in blocks if isinstance(b, dict)])
    except Exception:  # noqa: BLE001 - malformed blocks are reported by the engine's own validation
        tokens = []
    for tok in tokens:
        # A name the report engine doesn't know is left to its own "unknown placeholder" error.
        if tok["name"] not in KIND_TOKENS[kind] and not rt._check_token(tok, None):
            errors.append(f"{tok['raw']}: report placeholders such as {{{{spend}}}} aren't available in the {kind} message, "
                          f"which carries no report data; the {kind} template has {own}")
    return list(dict.fromkeys(errors))


def validate_template(kind: str, raw: dict) -> dict:
    """A clean template `{"blocks": [...]}`, or TemplateError listing everything wrong with it.
    Placeholders are held to this template's own list: report ones such as {{spend}} and metric blocks are refused. Only the summary may
    carry the client table binding, which is kept as written."""
    _kind(kind)
    blocks_in = _blocks_of(raw)
    errors: list[str] = report_content_errors(kind, blocks_in)
    if sum(1 for b in blocks_in if isinstance(b, dict) and b.get("type") in TABLE_BLOCKS) > 1:
        errors.append("a message can have only one table: use the client table or a table of your own, not both")
    stubbed: dict[int, dict] = {}
    stand_in = []
    for i, b in enumerate(blocks_in):
        if isinstance(b, dict) and isinstance(b.get("birdy_data"), dict) and b["birdy_data"].get("source"):
            where = f"block {i + 1}"
            if kind != "summary":
                errors.append(f"{where}: only the summary has a client table")
            elif b.get("type") not in TABLE_BLOCKS:
                errors.append(f"{where}: the client table is a table or data_table block")
            else:
                cols = _binding_columns(b["birdy_data"], where, errors)
                if cols:
                    stubbed[i] = {**BINDING, **({"columns": cols} if "columns" in b["birdy_data"] else {})}
                    b = {**{k: v for k, v in b.items() if k != "birdy_data"},
                         "rows": [[{"type": "raw_text", "text": "Client"}]]}
        stand_in.append(b)
    _reserved_ids(blocks_in, errors)
    try:
        clean = rb.validate({"frequency": FREQUENCY[kind], "blocks": stand_in}, None,
                            allowed_tokens=frozenset(KIND_TOKENS[kind]))["blocks"]
    except TemplateError as e:
        own = ", ".join("{{%s}}" % t for t in KIND_TOKENS[kind])
        raise TemplateError(list(dict.fromkeys([*errors, *(
            f"{m.split(': unknown placeholder')[0]}: unknown placeholder; the {kind} template has {own}, "
            f"and report ones such as {{{{spend}}}}" if ": unknown placeholder" in m else m for m in e.errors)])))
    if errors:
        raise TemplateError(list(dict.fromkeys(errors)))
    for i, binding in stubbed.items():
        clean[i] = {**{k: v for k, v in clean[i].items() if k != "rows"}, "birdy_data": binding}
    return {"blocks": clean}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def make_context(frequency: str = "daily", *, now_utc: datetime, currency: str = "GBP", sample: bool = False,
                 preview: bool = False, groups: list | None = None, alerts: list | None = None) -> rt.Context:
    """The engine's context for a template. Report placeholders such as {{spend}} read `groups`
    (or sample figures); the agent's own values are set by `render_kind`."""
    return rt.Context(frequency=frequency, groups=groups or [], currency=currency, now_utc=now_utc,
                      alerts=alerts or [], flagged=[], sample=sample, preview=preview)


def _plain_cell(text) -> dict:
    """A table cell from a name. Raw text can't ping, so it is shown as written; it can't start a placeholder."""
    return {"type": "raw_text", "text": str(text if text is not None else "")[:MAX_CELL].replace("{{", "{\u200b{")}


def rows_from_cards(cards: list[dict]) -> list[dict]:
    """The summary's client table, from the run's own cards (not from any cache): one row per card."""
    return [{"client": c.get("client_name") or "Client",
             "previous_cpl": _fmt_money(c.get("previous_cpl"), c.get("currency")),
             "current_cpl": _fmt_money(c.get("current_cpl"), c.get("currency")),
             "zone": ZONE_LABELS.get(c.get("zone"), str(c.get("zone") or "n/a"))} for c in cards]


def _table_for(block: dict, rows: list[dict] | None) -> list[dict]:
    """The binding as blocks: a table kept inside Slack's row and character limits, then a note
    saying how many clients did not fit."""
    cols = (block.get("birdy_data") or {}).get("columns") or list(TABLE_COLUMNS)
    rest = {k: v for k, v in block.items() if k != "birdy_data"}
    if not rows:
        return [{"type": "context", "elements": [{"type": "mrkdwn", "text": "_Clients: nothing to show yet_"}]}]
    max_rows, max_chars = TABLE_LIMITS[block.get("type") if block.get("type") in TABLE_LIMITS else "table"]
    head = [{"type": "raw_text", "text": "Client"}] + [{"type": "raw_text", "text": TABLE_COLUMNS[c]} for c in cols]
    used = sum(len(c["text"]) for c in head)
    body = []
    for r in rows[:max_rows]:
        row = [_plain_cell(r.get("client"))] + [_plain_cell(r.get(c)) for c in cols]
        size = sum(len(c["text"]) for c in row)
        if used + size > max_chars:
            break
        used += size
        body.append(row)
    out = [{**rest, "rows": [head, *body]}]
    if len(rows) > len(body):
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"_and {len(rows) - len(body)} more clients, see the report_"}]})
    return out


def render_kind(kind: str, template: dict, values: dict, rows: list[dict] | None, ctx: rt.Context) -> tuple[list, str]:
    """(blocks, notification text) for one template. The binding becomes a real table first, the
    template is cut to leave room for the buttons, checked and rendered. Values are plain text:
    the engine escapes them. Raises TemplateError when the template no longer validates; the caller
    posts the default template instead."""
    _kind(kind)
    original = _blocks_of(template)
    if problems := report_content_errors(kind, original):
        raise TemplateError(problems)
    blocks = []
    for b in copy.deepcopy(original):
        if isinstance(b, dict) and isinstance(b.get("birdy_data"), dict) and b["birdy_data"].get("source") == BINDING["source"]:
            blocks.extend(_table_for(b, rows))
            continue
        blocks.append(b)
    fitted, _ = rb.fit_to_slack({"frequency": FREQUENCY[kind], "blocks": blocks}, max_blocks=rb.MAX_BLOCKS - ACTIONS_RESERVED)
    clean = rb.validate(fitted, None, allowed_tokens=frozenset(KIND_TOKENS[kind]))
    ctx.agent = {name: str(values.get(name) or "") for name in KIND_TOKENS[kind]}
    blocks, _ = rb.render(clean, ctx)
    return blocks, _notification_text(blocks)


def _escape_unparsed(node):
    """A copy with the text of every field the engine leaves unescaped (plain text, raw text, markdown
    block) escaped. Those can't ping inside a block, but a message's `text` is read as mrkdwn."""
    if isinstance(node, list):
        return [_escape_unparsed(v) for v in node]
    if not isinstance(node, dict):
        return node
    out = {k: _escape_unparsed(v) for k, v in node.items()}
    if (node.get("type") in ("plain_text", "raw_text", "markdown") and isinstance(node.get("text"), str)):
        out["text"] = rt._escape_all(node["text"])
    return out


def _notification_text(blocks: list) -> str:
    return rb.fallback_text(_escape_unparsed(blocks))


def render_kind_safe(kind: str, template: dict | None, values: dict, rows: list[dict] | None,
                     ctx: rt.Context) -> tuple[list, str, bool]:
    """(blocks, text, fell_back). A saved template that no longer renders (a placeholder or block
    removed since it was saved, or stored junk) is logged and the default is used instead, so a run
    never loses its report to a template. `fell_back` is for the notice in settings. No saved
    template is not a fall back."""
    if template is not None:
        try:
            return (*render_kind(kind, template, values, rows, ctx), False)
        except Exception as e:  # noqa: BLE001 - the report must go out whatever the template holds
            logger.warning("stock %s template can't be rendered, using the default: %s", kind, e, exc_info=True)
            blocks, text = render_kind(kind, DEFAULT_TEMPLATES[kind], values, rows, ctx)
            return blocks, text, True
    return (*render_kind(kind, DEFAULT_TEMPLATES[kind], values, rows, ctx), False)


# ---------------------------------------------------------------------------
# Values, built from stored cards and runs
# ---------------------------------------------------------------------------

def _day(d) -> str:
    try:
        d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    except ValueError:
        return str(d)
    return f"{d.day} {d:%b}"


def _window(w) -> str:
    if isinstance(w, dict) and w.get("start") and w.get("end"):
        return f"{_day(w['start'])} to {_day(w['end'])}"
    return str(w or "")


_VERBS = {"add": "Add", "remove": "Pause", "revive": "Bring back"}


def _line_text(line: dict, currency) -> str:
    bits = [f"• {_VERBS.get(line.get('kind'), str(line.get('kind') or '').title())} *{line.get('ad_name') or 'ad'}*"]
    if line.get("cpl") is not None:
        bits.append(f"CPL {_fmt_money(line['cpl'], line.get('currency') or currency)}")
    if line.get("mode") == "flag":
        bits.append("(flag only)")
    elif line.get("state") not in (None, STATE_PROPOSED):
        bits.append(f"({str(line['state']).replace('_', ' ')})")
    return " ".join(bits)


def _follow_ups(card: dict, frontend_url: str = "") -> str:
    out = []
    for ln in card.get("lines") or []:
        name = ln.get("ad_name") or "ad"
        for f in ln.get("flags") or []:
            if f.get("note"):
                out.append(f"• {name}: {f['note']}")
        if ln.get("state") == "needs_file":
            out.append(f"• {name}: upload the video")
        elif ln.get("state") in ("blocked", "failed") and ln.get("error"):
            out.append(f"• {name}: {ln['error']}")
    if any(ln.get("state") in ("needs_file", "blocked", "failed") for ln in card.get("lines") or []):
        link = f": {report_url(frontend_url, card['run_id'])}" if frontend_url and card.get("run_id") else ""
        out.append(f"Slack can't retry these. Open the report to retry or upload{link}")
    return "\n".join(out)


def card_values(card: dict, frontend_url: str = "") -> dict:
    """Values for the card template. Plain text; the engine escapes it. `frontend_url` gives the
    follow-ups a link to the report when a line needs a person."""
    cur = card.get("currency")
    lines = card.get("lines") or []
    return {"client": card.get("client_name") or "Client", "window": _window(card.get("window")),
            "previous_cpl": _fmt_money(card.get("previous_cpl"), cur), "current_cpl": _fmt_money(card.get("current_cpl"), cur),
            "zone": ZONE_LABELS.get(card.get("zone"), str(card.get("zone") or "n/a")),
            "changes": "\n".join(_line_text(ln, cur) for ln in lines) or "No changes",
            "additional_changes": card.get("additional_changes") or "None",
            "thought_process": card.get("thought_process") or "None",
            "next_steps": card.get("next_steps") or "None", "follow_ups": _follow_ups(card, frontend_url) or "None"}


_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,20}$")
_SEVERITY = {"critical": "🔴", "warning": "🟡"}


def summary_values(run: dict, cards: list[dict], *, tag_user_id: str | None = None, batch_name: str | None = None,
                   client_names: dict | None = None) -> dict:
    """Values for the summary template. `tag` is a mention only for a well-formed Slack user id. `client_names`
    names the clients with an alert and no card, as on an alert-only day."""
    names = {**(client_names or {}), **{c.get("client_group_id"): c.get("client_name") for c in cards}}
    alerts = [a for a in run.get("alerts") or [] if isinstance(a, dict)]
    lines = [f"{_SEVERITY.get(a.get('severity'), 'ℹ️')} {names.get(a.get('client_group_id')) + ': ' if names.get(a.get('client_group_id')) else ''}"
             f"{a.get('title') or a.get('code') or 'Alert'}" for a in alerts[:10]]
    if len(alerts) > 10:
        lines.append(f"and {len(alerts) - 10} more")
    zones = {z: sum(1 for c in cards if c.get("zone") == z) for z in ZONE_LABELS}
    waiting = sum(1 for c in cards if c.get("status") == STATUS_OPEN
                  and any(ln.get("state") == STATE_PROPOSED for ln in c.get("lines") or []))
    batch_id = run.get("batch_id")
    return {"date": _day(run.get("local_date")) if run.get("local_date") else "",
            "batch": batch_name or (f"Batch {str(batch_id).upper()}" if batch_id else "Alerts only"),
            "zone_counts": " · ".join(f"{ZONE_LABELS[z].split(' ')[0]} {n} {z.replace('_', ' ')}" for z, n in zones.items() if n) or "No cards",
            "alerts": "\n".join(lines) or "None", "cards_waiting": str(waiting),
            "tag": f"<@{tag_user_id}>" if isinstance(tag_user_id, str) and _USER_ID.match(tag_user_id) else ""}


# ---------------------------------------------------------------------------
# Buttons, added by code
# ---------------------------------------------------------------------------

def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict:
    out = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": action_id, "value": value}
    return {**out, "style": style} if style else out


def _undo_open(line: dict, now: datetime) -> bool:
    if line.get("state") != STATE_APPLIED or line.get("kind") not in UNDOABLE_KINDS:
        return False
    until = line.get("undo_until") or ((line.get("applied_at") or datetime.min) + UNDO_AUTO_WINDOW)
    return now <= until


def card_buttons(card: dict, now: datetime | None = None, *, has_newer: bool) -> dict | None:
    """The card's actions block, from its state alone, or None. A superseded, closing or preview card
    gets none. Apply needs a proposed line (a Flag line is never one) on a card nobody has replaced;
    Edit and Dismiss wait while any line is going out; Undo is per applied line, inside its window.
    Values are the card id, and for Undo `card_id:line_id`: the handler checks everything else."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    status, cid = card.get("status"), card.get("_id") or card.get("id")
    if card.get("preview") or status in (STATUS_CLOSING, "superseded") or not cid:
        return None
    lines = card.get("lines") or []
    busy = any(ln.get("state") in BUSY_STATES for ln in lines)
    proposed = any(ln.get("state") == STATE_PROPOSED and ln.get("mode") != "flag" for ln in lines)
    buttons = []
    if status in (STATUS_OPEN, STATUS_PARTLY_APPLIED) and proposed and not has_newer:
        buttons.append(_button("Apply", "agent_card_apply", cid, "primary"))
    if status in (STATUS_OPEN, STATUS_FLAGGED) and not busy:
        buttons.append(_button("Edit", "agent_card_edit", cid))
    if status in (STATUS_OPEN, STATUS_FLAGGED, STATUS_PARTLY_APPLIED) and not busy:
        buttons.append(_button("Dismiss", "agent_card_dismiss", cid))
    if status in (STATUS_OPEN, STATUS_PARTLY_APPLIED, STATUS_APPLIED, STATUS_DISMISSED):
        for ln in lines:
            if _undo_open(ln, now):
                buttons.append(_button(f"Undo {_VERBS.get(ln['kind'], '').lower()}".strip() if len(lines) > 1 else "Undo",
                                       f"agent_card_undo:{ln['id']}", f"{cid}:{ln['id']}"))
    return {"type": "actions", "block_id": f"agent_card_{cid}"[:255], "elements": buttons[:25]} if buttons else None


def summary_buttons(run: dict, frontend_url: str, *, waiting: int | None = None) -> dict:
    """Apply all (when cards are waiting, never on a preview) and Open report. `waiting` is the
    count of cards still to decide; without it the run's cards with changes are used."""
    rid = run.get("_id") or run.get("id")
    count = waiting if waiting is not None else (run.get("summary") or {}).get("changes") or 0
    elements = []
    if count and run.get("kind") != "preview":
        elements.append(_button("Apply all", "agent_run_apply_all", rid, "primary"))
    elements.append({"type": "button", "text": {"type": "plain_text", "text": "Open report", "emoji": True},
                     "action_id": "agent_open_report", "url": report_url(frontend_url, rid)})
    return {"type": "actions", "block_id": f"agent_run_{rid}"[:255], "elements": elements}


def assemble(blocks: list, buttons: dict | None) -> list:
    """The template's blocks with the buttons last. If the message is over 50 blocks, the template's
    last blocks go, never the buttons."""
    if not buttons:
        return list(blocks)[:rb.MAX_BLOCKS]
    return [*list(blocks)[:rb.MAX_BLOCKS - ACTIONS_RESERVED], buttons]


# ---------------------------------------------------------------------------
# Sample data, for the editor's preview
# ---------------------------------------------------------------------------

def _sample_cards(now: datetime, currency: str) -> list[dict]:
    window = {"start": (now - timedelta(days=7)).date().isoformat(), "end": (now - timedelta(days=1)).date().isoformat()}
    base = {"currency": currency, "window": window, "status": STATUS_OPEN, "additional_changes": "", "next_steps": "Review again on the next batch day."}
    return [
        {**base, "_id": "sample-1", "client_group_id": "s1", "client_name": "Glow Aesthetics", "zone": "red", "previous_cpl": 9.4,
         "current_cpl": 12.8, "thought_process": "CPL rose for three days while one ad took most of the spend.",
         "lines": [{"id": "l1", "kind": "remove", "ad_name": "Spring offer, video 2", "mode": "ask", "state": STATE_PROPOSED, "cpl": 21.3, "flags": []},
                   {"id": "l2", "kind": "add", "ad_name": "Smile makeover, testimonial", "mode": "ask", "state": STATE_PROPOSED, "cpl": 6.2,
                    "flags": [{"kind": "client_name", "note": "Mentions another client: Bright Smiles"}]}]},
        {**base, "_id": "sample-2", "client_group_id": "s2", "client_name": "Bright Smiles", "zone": "green", "previous_cpl": 7.1,
         "current_cpl": 6.4, "thought_process": "CPL is in the green zone. No changes.", "lines": [], "tag": "green"},
    ]


def sample_preview(kind: str, template: dict, *, now: datetime, currency: str, frontend_url: str) -> tuple[list, str]:
    """A template rendered with sample values and sample report figures, buttons included. Reads and
    sends nothing."""
    _kind(kind)
    ctx = make_context(FREQUENCY[kind], now_utc=now, currency=currency, sample=True, preview=True)
    cards = _sample_cards(now, currency)
    run = {"_id": "sample-run", "kind": "batch", "batch_id": "b", "local_date": now.date().isoformat(), "summary": {"changes": 1},
           "alerts": [{"client_group_id": "s2", "severity": "warning", "title": "Spend dropped by half"}]}
    if kind == "card":
        blocks, text = render_kind(kind, template, card_values(cards[0]), None, ctx)
        return assemble(blocks, card_buttons(cards[0], now.replace(tzinfo=None), has_newer=False)), text
    if kind == "summary":
        blocks, text = render_kind(kind, template, summary_values(run, cards, tag_user_id="U0SAMPLE"), rows_from_cards(cards), ctx)
        return assemble(blocks, summary_buttons(run, frontend_url, waiting=1)), text
    week = f"{_day(now - timedelta(days=7))} to {_day(now - timedelta(days=1))}"
    sample = {"week": week, "comparison": "2 clients improved, 1 held, 1 decreased.", "improved": "• Bright Smiles: CPL 7.10 to 6.40",
              "decreased": "• Glow Aesthetics: CPL 9.40 to 12.80", "status_check": "3 of 4 clients are in the green zone.",
              "wins": "• Smile makeover, testimonial: 9 leads at 6.20"}
    blocks, text = render_kind(kind, template, sample, None, ctx)
    return assemble(blocks, None), text
