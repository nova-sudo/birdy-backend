"""
ai/tools/stock_card_tools.py
----------------------------
Chat tools for one stock agent card (page "stock_card", BirdyHub redesign spec section 3.2). The card
comes from `current_stock_card`, which routers/chat.py sets per request after an owner check. Every
tool also matches the card on `user_id`, so a card id alone never reaches another user's card.

Each tool goes through the same code as the dashboard and Slack (stock.apply.apply_card / dismiss_card,
stock.cards.edit_card, stock.lessons.lesson_from_text), so guardrails, modes, write leases, the version
lock and Slack card updates all still apply. Tools return {ok, changed, ...} or {ok: false, error}
and never raise for a person's mistake:

  card_closed  the card isn't open or flagged any more (applied, dismissed, superseded), or is a preview
  conflict     it changed while the chat was open, or a line is being applied right now
  expired      too late for that action
  not_found    no such card for this user
  nothing_to_apply  apply found no line it could send (Flag lines, or lines already decided)
"""

from contextvars import ContextVar

from ai.agents.stock import apply as stock_apply
from ai.agents.stock import cards as stock_cards
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock.cards import (STATE_APPLIED, STATE_APPLYING, STATE_QUEUED, STATUS_CLOSING, STATUS_FLAGGED,
                                   STATUS_OPEN, STATUS_PARTLY_APPLIED)
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_zones
from ai.agents.store import AGENTS, CARDS, RUNS, TYPE_STOCK
from ai.prompts import stock_card_chat
from ai.tools.registry import registry

CHAT_BUDGET_S = 20      # how long "do it" waits for Meta; the cron finishes what doesn't fit
SOURCE = "chat"
CARD_CLOSED, CONFLICT, EXPIRED, NOT_FOUND = "card_closed", "conflict", "expired", "not_found"
NOTHING_TO_APPLY = "nothing_to_apply"
_ACTS_ON = (STATUS_OPEN, STATUS_FLAGGED)            # what a card chat can apply, edit or dismiss
_STILL_DECIDING = _ACTS_ON + (STATUS_PARTLY_APPLIED,)   # a partly applied card can retry or dismiss what's left
_GOING_OUT = (STATE_APPLIED, STATE_QUEUED, STATE_APPLYING)

# The card the chat is about. routers/chat.py sets it for one request on page "stock_card" and
# resets it afterwards; None means "no card", never a guess.
current_stock_card: ContextVar[str | None] = ContextVar("current_stock_card", default=None)
# True when the person opened the card read-only (a past run, a month, a client outside today's batch).
# routers/chat.py sets it with current_stock_card; every action tool then refuses with card_closed.
current_card_read_only: ContextVar[bool] = ContextVar("current_card_read_only", default=False)


def _fail(error: str, **extra) -> dict:
    return {"ok": False, "error": error, **extra}


async def load_chat_card(db, user_id: str, card_id: str | None, agent_id: str | None = None) -> dict | None:
    """The card a stock_card chat may open: this user's, on one of their stock agents, and stored
    (not mid-save). With `agent_id`, it must be that agent's too. None otherwise."""
    if not card_id:
        return None
    card = await db[CARDS].find_one({"_id": card_id, "user_id": user_id, "status": {"$ne": STATUS_CLOSING}})
    if card is None or (agent_id and card.get("agent_id") != agent_id):
        return None
    agent = await db[AGENTS].find_one({"_id": card.get("agent_id"), "user_id": user_id, "type": TYPE_STOCK}, {"_id": 1})
    return card if agent is not None else None


async def _card(db, user_id) -> dict | None:
    return await load_chat_card(db, user_id, current_stock_card.get())


async def _agent(db, user_id, card: dict) -> dict | None:
    return await db[AGENTS].find_one({"_id": card.get("agent_id"), "user_id": user_id})


def _closed(card: dict, statuses=_ACTS_ON) -> bool:
    return current_card_read_only.get() or bool(card.get("preview")) or card.get("status") not in statuses


def _refuse_closed(card: dict) -> dict:
    extra = {"read_only": True} if current_card_read_only.get() else {}
    return _fail(CARD_CLOSED, status=card.get("status"), preview=bool(card.get("preview")), **extra)


_LINE_KEYS = ("id", "kind", "ad_name", "mode", "state", "error", "spend", "leads", "cpl", "currency", "source", "flags")


def card_view(card: dict) -> dict:
    """What Birdy needs to talk about a card: facts, lines with their ids and state, and the words on it."""
    return {
        "id": card["_id"], "client_name": card.get("client_name"), "client_group_id": card.get("client_group_id"),
        "status": card.get("status"), "preview": bool(card.get("preview")), "can_act": not _closed(card),
        "read_only": current_card_read_only.get(),
        "tag": card.get("tag"), "zone": card.get("zone"), "window": card.get("window"),
        "currency": card.get("currency"), "previous_cpl": card.get("previous_cpl"), "current_cpl": card.get("current_cpl"),
        "leads": card.get("leads"), "spend": card.get("spend"), "ads_live": card.get("ads_live"),
        "lines": [{k: ln.get(k) for k in _LINE_KEYS} for ln in card.get("lines") or [] if isinstance(ln, dict)],
        "additional_changes": card.get("additional_changes"), "thought_process": card.get("thought_process"),
        "next_steps": card.get("next_steps"), "follow_ups": card.get("follow_ups") or [],
        "decided_by": card.get("decided_by"), "dismiss_reason": card.get("dismiss_reason"),
    }


def _decision_error(outcome: str) -> str:
    return {"not_found": NOT_FOUND, "refused": CARD_CLOSED, "busy": CONFLICT, "expired": EXPIRED}.get(outcome, outcome)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def get_card(db, user_id, mongo_client=None, **_):
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    return {"ok": True, "card": card_view(card)}


async def apply_card_lines(db, user_id, mongo_client=None, line_ids=None, **_):
    """Apply the card's proposed lines, or only the named ones. A Flag line is never applied: the
    result's `lines` say so. `ok` is true only when apply_card said so and a line moved; when nothing
    could go out (all Flag, or already decided) it is false with nothing_to_apply, so the result never
    reads as something Birdy applied."""
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    if _closed(card, _STILL_DECIDING):
        return _refuse_closed(card)
    ids = [str(i) for i in line_ids] if isinstance(line_ids, list) and line_ids else None
    res = await stock_apply.apply_card(db, mongo_client, user_id, card["_id"], by=user_id, source=SOURCE,
                                       line_ids=ids, budget_s=CHAT_BUDGET_S)
    outcome = res.get("outcome")
    before = {ln.get("id"): ln.get("state") for ln in card.get("lines") or []}
    moved = [ln for ln in res.get("lines") or [] if ln.get("state") != before.get(ln.get("id"))]
    out = {"ok": bool(res.get("ok")), "outcome": outcome, "reason": res.get("reason"), "lines": res.get("lines") or [],
           "count": sum(1 for ln in moved if ln.get("state") in _GOING_OUT)}
    if outcome in ("not_found", "refused", "bad_lines"):
        return {**out, "ok": False, "error": _decision_error(outcome)}
    if outcome in ("noop", "already"):
        return {**out, "ok": False, "error": NOTHING_TO_APPLY}
    if outcome in ("applied", "partial", "queued"):
        out["changed"] = "applied"
    elif outcome in ("failed", "blocked", "needs_file"):
        out["changed"] = "apply_failed"   # lines moved even though none got through: the sheet must re-read it
    if not out["ok"]:
        out["error"] = outcome
    return out


async def dismiss_card(db, user_id, mongo_client=None, reason=None, **_):
    """Skip what's left on the card. A reason becomes a lesson, the same as a dismiss anywhere else."""
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    if _closed(card, _STILL_DECIDING):
        return _refuse_closed(card)
    reason = str(reason).strip() if reason else None
    res = await stock_apply.dismiss_card(db, user_id, card["_id"], by=user_id, source=SOURCE, reason=reason or None)
    outcome = res.get("outcome")
    if outcome == "dismissed":
        return {"ok": True, "changed": "dismissed", "lesson_saved": bool(reason)}
    if outcome == "already" and res.get("ok"):
        return {"ok": True, "outcome": "already"}
    if outcome == "already":
        return _fail(CARD_CLOSED, reason=res.get("reason"))
    return _fail(_decision_error(outcome), reason=res.get("reason"))


async def edit_card(db, user_id, mongo_client=None, keep_line_ids=None, additional_changes=None,
                    thought_process=None, next_steps=None, **_):
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    if _closed(card):
        return _refuse_closed(card)
    keep = [str(i) for i in keep_line_ids] if isinstance(keep_line_ids, list) else None
    res = await stock_cards.edit_card(db, user_id, card["_id"], lines=keep, additional_changes=additional_changes,
                                      thought_process=thought_process, next_steps=next_steps, by=user_id,
                                      agent_id=card.get("agent_id"))
    if not res["ok"]:
        error = CONFLICT if res["error"] in (stock_cards.EDIT_CONFLICT, stock_cards.EDIT_LINE_BUSY) else res["error"]
        return _fail(error, reason=res.get("reason"))
    return {"ok": True, "changed": "edited", "card": card_view(res["card"])}


async def add_lesson(db, user_id, mongo_client=None, text=None, scope="client", **_):
    """Save what the person said as a lesson, for this client or for every client."""
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    text = str(text or "").strip()
    if not text:
        return _fail("empty_lesson")
    if scope not in ("client", "all"):
        return _fail("bad_scope")
    agent = await _agent(db, user_id, card)
    if agent is None:
        return _fail(NOT_FOUND)
    gid = card.get("client_group_id") if scope == "client" else None
    lesson = await stock_lessons.lesson_from_text(db, agent, text, source=SOURCE, author=user_id,
                                                  client_group_id=gid, source_ref=card["_id"])
    if lesson is None:
        return _fail("empty_lesson")
    return {"ok": True, "changed": "lesson",
            "lesson": {"id": lesson["_id"], "text": lesson.get("text"), "client_group_id": lesson.get("client_group_id")}}


async def draft_client_message(db, user_id, mongo_client=None, tone=None, **_):
    """The facts a message to the client can be written from. Writes and sends nothing."""
    card = await _card(db, user_id)
    if card is None:
        return _fail(NOT_FOUND)
    view = card_view(card)
    done = [{"kind": ln["kind"], "ad_name": ln["ad_name"], "state": ln["state"]} for ln in view["lines"]]
    keys = ("client_name", "window", "currency", "previous_cpl", "current_cpl", "leads", "spend", "ads_live", "zone",
            "next_steps")
    return {"ok": True, "facts": {**{k: view[k] for k in keys}, "changes": done, "tone": tone or "friendly"}}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

async def prompt_context(db, user_id) -> dict:
    """What the card chat's system prompt is built from: the card, the client's zones and the lessons."""
    card = await _card(db, user_id)
    if card is None:
        return {"card": None, "zones": None, "lessons": []}
    agent = await _agent(db, user_id, card) or {}
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    zones = resolve_zones(settings, card.get("client_group_id"), card.get("currency"))
    lessons = await stock_lessons.lessons_for_prompt(db, card.get("agent_id"), card.get("client_group_id"))
    run = await db[RUNS].find_one({"_id": card.get("run_id"), "user_id": user_id}, {"alerts": 1}) or {}
    alerts = [{k: a.get(k) for k in ("code", "severity", "title", "detail")} for a in run.get("alerts") or []
              if a.get("client_group_id") == card.get("client_group_id")]
    return {"card": card_view(card), "zones": None if zones is None else {"green_under": zones.green_under,
                                                                          "red_over": zones.red_over},
            "lessons": lessons, "alerts": alerts}


def change_entry(tool: str, result: dict) -> dict | None:
    """The `agent_changes` entry for one tool result, or None when nothing changed. Carries the count of
    lines that went out, so the chat can say "2 applied"."""
    if not isinstance(result, dict) or not result.get("changed"):
        return None
    entry = {"tool": tool, "changed": result["changed"]}
    if "count" in result:
        entry["count"] = result["count"]
    return entry


async def build_system_prompt(db, user_id) -> str:
    return stock_card_chat.system_prompt(await prompt_context(db, user_id))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOOL_DEFS = {
    "get_card": (
        get_card,
        "Read this client's card as it is now: figures, each line with its id, mode and state, the reasoning and "
        "next steps. Call it again before acting if the card may have changed.",
        {"type": "object", "properties": {}, "required": []},
    ),
    "apply_card_lines": (
        apply_card_lines,
        "Make the card's proposed changes in Meta. With no line_ids, every proposed line; otherwise only those "
        "lines (naming a failed one retries it). Flag lines are never made. Only after the person said yes.",
        {"type": "object", "properties": {
            "line_ids": {"type": "array", "items": {"type": "string"}, "description": "Line ids from get_card."}},
         "required": []},
    ),
    "dismiss_card": (
        dismiss_card,
        "Skip what's left on this card. A reason the person gave is saved as a lesson.",
        {"type": "object", "properties": {"reason": {"type": "string"}}, "required": []},
    ),
    "edit_card": (
        edit_card,
        "Edit the card: keep only some lines (by id, in order) and/or rewrite the additional changes, the "
        "reasoning or the next steps.",
        {"type": "object", "properties": {
            "keep_line_ids": {"type": "array", "items": {"type": "string"}},
            "additional_changes": {"type": "string"}, "thought_process": {"type": "string"},
            "next_steps": {"type": "string"}},
         "required": []},
    ),
    "add_lesson": (
        add_lesson,
        "Save a preference the person stated as a lesson Birdy uses on later runs. scope 'client' is for this "
        "client only, 'all' for every client.",
        {"type": "object", "properties": {
            "text": {"type": "string"}, "scope": {"type": "string", "enum": ["client", "all"]}},
         "required": ["text"]},
    ),
    "draft_client_message": (
        draft_client_message,
        "Get the facts to write a short message to the client about this card. Sends nothing; you write the "
        "message from what it returns.",
        {"type": "object", "properties": {"tone": {"type": "string"}}, "required": []},
    ),
}

STOCK_CARD_TOOL_NAMES = tuple(_TOOL_DEFS)


def register_stock_card_tools(tool_registry=None):
    target = tool_registry or registry
    for name, (executor, description, parameters) in _TOOL_DEFS.items():
        target.register(name=name, description=description, parameters=parameters, executor=executor)
