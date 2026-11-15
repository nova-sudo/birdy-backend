"""
ai/mcp/stock_agent_mcp.py
--------------------------
Read-only tools for the Birdy Media Buying Agent: what it did, what it found and what is waiting on the owner.
Each wraps the logic behind the matching GET route in routers/agent_stock.py (ai/agents/stock/reports.py).
"""

from typing import Literal

from fastmcp.exceptions import ToolError

from ai.agents import access as agent_access
from ai.agents.stock import reports
from ai.mcp.access import READ
from ai.mcp.agent_mcp import resolve_agent
from ai.mcp.server import mcp, current_user_id as _current_user_id
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
