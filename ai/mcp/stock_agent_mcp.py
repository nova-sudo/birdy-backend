"""
ai/mcp/stock_agent_mcp.py
--------------------------
Tools for the Birdy Media Buying Agent: what it did, what it found and what is waiting on the owner (read), and
the owner's decisions on it (write). Each wraps the logic behind the matching route in routers/agent_stock.py
(ai/agents/stock/reports.py, apply.py, agent_settings.py). Tools that change live ads or post to Slack
(apply_card, apply_all_cards, undo_card_line, post_weekly_report) are two-step: a preview and a confirm_code first.
"""

from typing import Literal

from fastmcp.exceptions import ToolError

from ai.agents import access as agent_access
from ai.agents.stock import agent_settings
from ai.agents.stock import apply as stock_apply
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock import reports
from ai.agents.stock import runner as stock_runner
from ai.agents.stock import templates as stock_templates
from ai.agents.stock.apply import APPLY_BUDGET_SECONDS
from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.agent_mcp import resolve_agent
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools import stock_card_tools
from ai.tools.stock_card_tools import card_view
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

SCOREBOARD_ROWS = 20
CARD_LIST_LIMIT = 50


async def _stock(agent: str | None):
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    return db, user_id, await resolve_agent(db, user_id, agent, kind="stock")


def _fail(e: reports.ReportError) -> ToolError:
    return ToolError(e.detail)


def _run_summary(run: dict | None) -> dict | None:
    """A run without its per-client working outcomes (get_agent_run has the detail)."""
    if run is None:
        return None
    out = {k: v for k, v in run.items() if k != "clients"}
    out["clients_count"] = len(run.get("clients") or {})
    return out


def _card_summary(card: dict) -> dict:
    return {"id": card["id"], "client_name": card.get("client_name"), "status": card.get("status"), "tag": card.get("tag"),
            "can_apply": card.get("can_apply"), "zone": card.get("zone"), "leads": card.get("leads"),
            "spend": card.get("spend"), "current_cpl": card.get("current_cpl"), "currency": card.get("currency"),
            "lines": len(card.get("lines") or []), "run_id": card.get("run_id"), "created_at": card.get("created_at")}


@mcp.tool(**READ)
async def get_agent_overview(agent: str | None = None) -> dict:
    """The Birdy Media Buying Agent at a glance: its status and settings, its latest report, and how many cards wait on the owner.

    Use this first when the owner asks what their media buying agent is doing or needs from them.

    Args:
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    latest = await reports.latest_run(db, found["_id"])
    return {"agent": {"id": found["_id"], "name": found.get("name"), "status": found.get("status"),
                      "settings": found.get("settings") or {}, "scope": found.get("scope") or {},
                      "last_run_at": found.get("last_run_at")},
            "latest_run": _run_summary(latest["run"]), "open_cards": latest["open_cards"]}


@mcp.tool(**READ)
async def get_agent_runs(limit: int = 10, agent: str | None = None) -> dict:
    """Past reports (runs) of the Birdy Media Buying Agent, newest first, without per-client detail.

    Use it to see what the agent did over time; pass a run's id to get_agent_run for its cards and findings.

    Args:
        limit: How many runs to return (1-50, default 10).
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    out = await reports.list_runs(db, found["_id"], max(1, min(limit, 50)))
    return {"runs": [_run_summary(r) for r in out["runs"]], "more": out["next_before"] is not None}


@mcp.tool(**READ)
async def get_agent_run(run_id: str, agent: str | None = None) -> dict:
    """One report in full: its cards (summarised), follow-ups, scoreboard summary, what the agent learned and open rule suggestions.

    Use it after get_agent_runs or get_agent_overview to explain what a specific report found. Use get_agent_card for a card's lines.

    Args:
        run_id: The run id from get_agent_runs or get_agent_overview.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    try:
        out = await reports.get_run(db, found["_id"], run_id)
    except reports.ReportError as e:
        raise _fail(e)
    board = out["scoreboard"]
    return {"run": _run_summary(out["run"]), "clients": out["run"]["clients"],
            "cards": [_card_summary(c) for c in out["cards"]], "follow_ups": out["follow_ups"],
            "scoreboard": {"won": board["won"], "lost": board["lost"], "moves": board["moves"]},
            "learned": out["learned"], "proposals": out["proposals"], "weekly": out["weekly"]}


@mcp.tool(**READ)
async def get_agent_month(month: str, agent: str | None = None) -> dict:
    """The whole-month report of the Birdy Media Buying Agent: per client the month's leads, spend, CPL and zone, and how many cards were decided.

    Args:
        month: The month as "YYYY-MM". Future months are refused.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    try:
        return await reports.month_report(db, found, month)
    except reports.ReportError as e:
        raise ToolError({"bad_month": "month must look like 2026-10", "future_month": "That month hasn't happened yet"}.get(e.detail, e.detail))


@mcp.tool(**READ)
async def get_agent_cards(status: Literal["open", "applied", "dismissed", "all"] = "open", agent: str | None = None) -> dict:
    """The agent's cards (proposed changes per client), newest first, summarised. Returns up to 50; `more` is true when there are older ones not shown.

    "open" is what is waiting on the owner to apply or dismiss; use get_agent_card for one card's lines and reasoning.

    Args:
        status: open (default), applied, dismissed or all.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    cards = await reports.list_cards(db, found["_id"], status, CARD_LIST_LIMIT + 1)
    return {"status": status, "count": min(len(cards), CARD_LIST_LIMIT), "more": len(cards) > CARD_LIST_LIMIT,
            "cards": [_card_summary(c) for c in cards[:CARD_LIST_LIMIT]]}


@mcp.tool(**READ)
async def get_agent_card(card_id: str, agent: str | None = None) -> dict:
    """One card in full: the client's numbers, every proposed line with its state, and the agent's reasoning and next steps.

    Use it to explain a card the owner is deciding on (get_agent_cards lists the ids).

    Args:
        card_id: The card id.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    card = await agent_access.find_card(db, user_id, found["_id"], card_id)
    if card is None:
        raise ToolError("Card not found")
    return card_view(card)


@mcp.tool(**READ)
async def get_agent_lessons(agent: str | None = None) -> list[dict]:
    """Everything the Birdy Media Buying Agent has learned from the owner (overruled lessons included), oldest first.

    Args:
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    return await reports.lessons(db, found["_id"])


@mcp.tool(**READ)
async def get_agent_outcomes(agent: str | None = None) -> dict:
    """The agent's scoreboard: moves won and lost, and its most recent scored changes (up to 20 rows; `more_rows` is true when older ones are cut; won/lost/moves cover all of them).

    Use it to answer "is the agent actually helping?".

    Args:
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, _, found = await _stock(agent)
    board = await reports.scoreboard(db, found["_id"])
    return {**board, "rows": board["rows"][:SCOREBOARD_ROWS], "more_rows": len(board["rows"]) > SCOREBOARD_ROWS}


@mcp.tool(**READ)
async def get_agent_slack_template(kind: str, agent: str | None = None) -> dict:
    """One of the agent's Slack message templates: the saved one (or default), the defaults and the placeholders it may use.

    Args:
        kind: Which message ("card", "summary" or "weekly").
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    _, _, found = await _stock(agent)
    try:
        return reports.template_view(reports.check_template_kind(kind), found)
    except reports.ReportError as e:
        raise _fail(e)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

SOURCE = "mcp"
NEXT_AFTER_PREVIEW = "Show the preview to the user and only call again with confirm_code after they say yes."
_OPEN_STATUSES = ("open", "partly_applied")


async def _owned_card(db, user_id: str, found: dict, card_id: str) -> dict:
    card = await agent_access.find_card(db, user_id, found["_id"], card_id)
    if card is None:
        raise ToolError("Card not found")
    return card


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _what_happened(res: dict, doing: str) -> str:
    """One sentence on what a decision service call did. `doing` is what was asked, e.g. "apply 2 lines"."""
    outcome, card = res.get("outcome"), res.get("card") or {}
    name = card.get("client_name")
    where = f" on {name}" if name else ""
    if res.get("ok") and outcome in ("applied", "dismissed", "undone", "partial", "queued", "flagged", "noop", "already"):
        done = {"applied": f"Applied it{where}.", "dismissed": f"Dismissed the card{where}.",
                "undone": f"Undid it{where}.", "queued": f"Queued it{where}; Birdy finishes it within a minute or two.",
                "partial": f"Applied some lines{where}; see lines for the rest.",
                "flagged": f"Nothing to change{where}: the lines are flags for a person.",
                "noop": f"Nothing was changed{where}: there was nothing waiting to {doing}.",
                "already": f"Nothing was changed{where}: it was already done."}[outcome]
        return done
    why = res.get("reason")
    return f"Could not {doing}{where}" + (f": {why}" if why else f" ({outcome}).")


def _with_summary(res: dict, doing: str) -> dict:
    return {**res, "summary": _what_happened(res, doing)}


def _lines_of(card: dict, ids: list[str] | None) -> list[dict]:
    """The lines a decision would touch: the named ones, else every line still proposed."""
    lines = [ln for ln in card.get("lines") or [] if isinstance(ln, dict)]
    return [ln for ln in lines if (ln.get("id") in ids if ids is not None else ln.get("state") == "proposed")]


def _card_preview(card: dict, ids: list[str] | None) -> dict:
    view = card_view(card)
    keep = {ln.get("id") for ln in _lines_of(card, ids)}
    view["lines"] = [ln for ln in view["lines"] if ln["id"] in keep]
    return {"card": view, "client": {"name": card.get("client_name"), "spend": card.get("spend"),
                                     "cpl": card.get("current_cpl"), "currency": card.get("currency")}}


@mcp.tool(**LIVE)
async def apply_card(card_id: str, line_ids: list[str] | None = None, agent: str | None = None,
                     confirm_code: str | None = None) -> dict:
    """Apply a card's proposed changes to the client's live Meta ads: pause, scale or swap the ads on the lines.

    Two steps. Called without confirm_code it changes nothing and returns a preview (the lines that would go live,
    plus the client's spend and CPL) and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. Naming a failed or blocked line in line_ids retries it.

    Args:
        card_id: The card id (get_agent_cards lists them).
        line_ids: Optional line ids to apply. Leave out to apply every proposed line.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _stock(agent)
    card = await _owned_card(db, user_id, found, card_id)
    ids = None
    if line_ids is not None:
        ids = sorted(set(str(i) for i in line_ids))
        if not ids:
            raise ToolError("line_ids is empty. Leave it out to apply every proposed line.")
        unknown = [i for i in ids if i not in {ln.get("id") for ln in card.get("lines") or []}]
        if unknown:
            raise ToolError(f"Those aren't lines of this card: {', '.join(unknown)}")
    elif not _lines_of(card, None):
        raise ToolError("No proposed lines on this card to apply.")

    async def preview():
        return _card_preview(card, ids)

    async def execute():
        res = await stock_apply.apply_card(db, get_shared_mongo_client(), user_id, card_id, by=user_id, source=SOURCE,
                                           line_ids=ids, budget_s=APPLY_BUDGET_SECONDS)
        return _with_summary(res, "apply it")

    return await two_step(db, user_id, "apply_card", {"agent_id": found["_id"], "card_id": card_id, "line_ids": ids},
                          confirm_code, preview, execute)


@mcp.tool(**LIVE)
async def apply_all_cards(run_id: str, agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Apply every open card in one report (run) to the clients' live Meta ads.

    Two steps. Called without confirm_code it changes nothing and returns a preview listing every open card in the
    run with its proposed lines, and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. What does not finish within about 20 seconds is finished in the background.

    Args:
        run_id: The run id from get_agent_runs.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _stock(agent)
    run = await db[stock_runner.RUNS].find_one({"_id": run_id, "agent_id": found["_id"], "user_id": user_id}, {"_id": 1})
    if run is None:
        raise ToolError("Run not found")

    async def preview():
        cards = await db[stock_runner.CARDS].find(
            {"run_id": run_id, "agent_id": found["_id"], "user_id": user_id, "status": {"$in": list(_OPEN_STATUSES)}}
        ).sort("created_at", 1).to_list(length=None)
        return {"count": len(cards), "cards": [_card_preview(c, None) for c in cards]}

    async def execute():
        res = await stock_apply.apply_run(db, get_shared_mongo_client(), user_id, run_id, by=user_id, source=SOURCE,
                                          budget_s=APPLY_BUDGET_SECONDS)
        return {**res, "summary": _apply_all_summary(res)}

    return await two_step(db, user_id, "apply_all_cards", {"agent_id": found["_id"], "run_id": run_id},
                          confirm_code, preview, execute)


def _apply_all_summary(res: dict) -> str:
    if not res.get("ok"):
        return f"Could not apply the run's cards: {res.get('reason') or res.get('outcome')}."
    outcome = res.get("outcome")
    if outcome == "queued":
        return (f"Applied {res.get('applied', 0)} line(s) so far; the rest are queued and finish in the background "
                "(check get_agent_run for progress).")
    if outcome == "applied":
        return f"Applied {res.get('applied', 0)} line(s) across the run's open cards."
    if outcome == "noop":
        return "Nothing to apply: the run has no open lines waiting."
    return f"Nothing was applied: {res.get('failed', 0)} failed, {res.get('blocked', 0)} blocked."


@mcp.tool(**LIVE)
async def undo_card_line(card_id: str, line_id: str, agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Reverse one applied line on the client's live Meta ads (only soon after it was applied).

    Two steps. Called without confirm_code it changes nothing and returns a preview of the line and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments.

    Args:
        card_id: The card id.
        line_id: The line id from get_agent_card.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _stock(agent)
    card = await _owned_card(db, user_id, found, card_id)
    if not _lines_of(card, [line_id]):
        raise ToolError("That isn't a line of this card.")

    async def preview():
        return _card_preview(card, [line_id])

    async def execute():
        res = await stock_apply.undo_line(db, get_shared_mongo_client(), user_id, card_id, line_id, by=user_id,
                                          source=SOURCE)
        return _with_summary(res, "undo it")

    return await two_step(db, user_id, "undo_card_line", {"agent_id": found["_id"], "card_id": card_id, "line_id": line_id},
                          confirm_code, preview, execute)


@mcp.tool(**LIVE)
async def post_weekly_report(run_id: str, agent: str | None = None, confirm_code: str | None = None) -> dict:
    """Post a report's weekly summary to the agency's Slack channel. It never posts twice.

    Two steps. Called without confirm_code it posts nothing and returns the weekly text and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments.

    Args:
        run_id: The run id from get_agent_runs (get_agent_run shows whether it has a weekly report).
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
        confirm_code: The code from the preview, once the user has agreed.
    """
    db, user_id, found = await _stock(agent)
    try:
        run = await reports.weekly_run(db, found["_id"], run_id)
    except reports.ReportError as e:
        raise _fail(e)

    async def preview():
        return {"text": reports.weekly_text(run)}

    async def execute():
        try:
            res = await reports.post_weekly_report(db, found, run_id, by=user_id)
        except reports.ReportError as e:
            raise _fail(e)
        said = {"posted": "Posted the weekly report to Slack.", "already_posted": "The weekly report was already posted; nothing was sent again."}
        return {**res, "summary": said.get(res["outcome"], f"Weekly report: {res['outcome']}.")}

    return await two_step(db, user_id, "post_weekly_report", {"agent_id": found["_id"], "run_id": run_id},
                          confirm_code, preview, execute)


@mcp.tool(**WRITE)
async def dismiss_card(card_id: str, reason: str | None = None, agent: str | None = None) -> dict:
    """Decline what is left on a card. Lines already applied stay applied. A reason becomes a lesson the agent learns from.

    Args:
        card_id: The card id.
        reason: Optional why the owner is declining (saved as a lesson).
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    await _owned_card(db, user_id, found, card_id)
    reason = (reason or "").strip() or None
    res = await stock_apply.dismiss_card(db, user_id, card_id, by=user_id, source=SOURCE, reason=reason)
    return _with_summary(res, "dismiss it")


async def _in_card_context(card_id: str, call):
    """stock_card_tools act on the card of the current chat; set that for one call."""
    cur, ro = stock_card_tools.current_stock_card.set(card_id), stock_card_tools.current_card_read_only.set(False)
    try:
        return await call()
    finally:
        stock_card_tools.current_stock_card.reset(cur)
        stock_card_tools.current_card_read_only.reset(ro)


@mcp.tool(**WRITE)
async def edit_card(card_id: str, keep_line_ids: list[str], additional_changes: str | None = None,
                    agent: str | None = None) -> dict:
    """Edit an open card before deciding: keep only the given lines (the others are removed) and optionally rewrite its additional changes text.

    Args:
        card_id: The card id.
        keep_line_ids: Ids of the lines to keep; every other line is dropped from the card.
        additional_changes: Optional new text for the card's additional changes.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    await _owned_card(db, user_id, found, card_id)
    res = await _in_card_context(card_id, lambda: stock_card_tools.edit_card(
        db, user_id, keep_line_ids=keep_line_ids, additional_changes=additional_changes))
    if not res.get("ok"):
        raise ToolError(res.get("reason") or res.get("error") or "Could not edit the card")
    return {**res, "summary": f"The card now has {_plural(len(res['card']['lines']), 'line')}."}


@mcp.tool(**WRITE)
async def add_agent_lesson(text: str, scope: Literal["client", "all"] = "client", card_id: str | None = None,
                           agent: str | None = None) -> dict:
    """Teach the agent something, in the owner's words (for example "never pause the brand-name ad").

    A "client" lesson applies to one client, taken from card_id; an "all" lesson applies to every client.

    Args:
        text: The lesson.
        scope: "client" (needs card_id) or "all".
        card_id: The card whose client the lesson is about. Required for scope "client".
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    card = await _owned_card(db, user_id, found, card_id) if card_id else None
    if card is None and scope == "client":
        raise ToolError('A lesson for one client needs card_id (the card of that client), or use scope "all".')
    gid = card.get("client_group_id") if card is not None and scope == "client" else None
    lesson = await stock_lessons.lesson_from_text(db, found, (text or "").strip(), source=SOURCE, author=user_id,
                                                  client_group_id=gid, source_ref=card_id)
    if lesson is None:
        raise ToolError("The lesson was empty.")
    res = {"ok": True, "changed": "lesson",
           "lesson": {"id": lesson["_id"], "text": lesson.get("text"), "client_group_id": gid}}
    return {**res, "summary": "Saved the lesson." if scope == "all" else "Saved the lesson for this client."}


def _agent_view(agent: dict) -> dict:
    return {"id": agent["_id"], "name": agent.get("name"), "status": agent.get("status"),
            "settings": agent.get("settings") or {}, "scope": agent.get("scope") or {}}


@mcp.tool(**WRITE)
async def update_agent_settings(settings: dict | None = None, client_group_ids: list[str] | None = None,
                                move_claims: bool = False, only_flag: list[str] | None = None,
                                agent: str | None = None) -> dict:
    """Change the Birdy Media Buying Agent's settings and which clients it watches. Same body as PATCH /settings.

    Only the fields given change; `settings` is merged into the current ones (get_agent_overview shows them).
    Invalid values are refused with the reasons and nothing is saved.

    Args:
        settings: Settings to merge, for example {"zones": {...}, "clients": {"<client_group_id>": {...}}, "weekly": {"auto_post": true}}.
        client_group_ids: The full list of client group ids the agent watches (replaces the current list).
        move_claims: When another agent already acts on one of those clients, take it over instead of refusing.
        only_flag: Watched clients the agent may only flag on, never change.
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    try:
        updated = await agent_settings.update_settings(db, user_id, found, settings=settings,
                                                       client_group_ids=client_group_ids, move_claims=move_claims,
                                                       only_flag=only_flag)
    except agent_settings.SettingsError as e:
        errors = "; ".join(f"{x.get('field')}: {x.get('message')}" for x in e.body.get("errors") or [])
        raise ToolError(f"{e.body.get('detail')}" + (f" ({errors})" if errors else ""))
    return {"agent": _agent_view(updated), "summary": "Updated the agent's settings."}


def _template_kind(kind: str) -> str:
    try:
        return reports.check_template_kind(kind)
    except reports.ReportError as e:
        raise _fail(e)


@mcp.tool(**WRITE)
async def set_agent_slack_template(kind: str, template: dict, agent: str | None = None) -> dict:
    """Replace one of the agent's Slack message templates. get_agent_slack_template shows the current one, the defaults and the placeholders allowed.

    Args:
        kind: Which message ("card", "summary" or "weekly").
        template: The template as {"blocks": [...]} (Slack-style blocks with {placeholders}).
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    kind = _template_kind(kind)
    try:
        clean = stock_templates.validate_template(kind, template)
    except stock_templates.TemplateError as e:
        raise ToolError("This template can't be used: " + "; ".join(str(m) for m in e.errors))
    updated = await reports.save_template(db, found, user_id, kind, clean)
    return {**reports.template_view(kind, updated), "summary": f"Saved your {kind} Slack template."}


@mcp.tool(**WRITE)
async def reset_agent_slack_template(kind: str, agent: str | None = None) -> dict:
    """Go back to the default for one of the agent's Slack message templates.

    Args:
        kind: Which message ("card", "summary" or "weekly").
        agent: Optional agent id or name. Defaults to the owner's Birdy Media Buying Agent.
    """
    db, user_id, found = await _stock(agent)
    kind = _template_kind(kind)
    updated = await reports.save_template(db, found, user_id, kind, None)
    return {**reports.template_view(kind, updated), "summary": f"Reset the {kind} Slack template to the default."}
