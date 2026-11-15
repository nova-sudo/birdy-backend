"""
ai/agents/stock/reports.py
--------------------------
What the Birdy Media Buying Agent did and found: runs and reports, cards, scoreboard, lessons, months and Slack
templates. The HTTP routes (routers/agent_stock.py) and the MCP tools (ai/mcp/stock_agent_mcp.py) both read through
these functions, so there is one implementation (list_cards has no route: it serves the MCP tool only). A missing thing raises ReportError, which each caller maps to its own error.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from ai.agents import learning
from ai.agents import store as agent_store
from ai.agents.stock import apply as stock_apply
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock import months as stock_months
from ai.agents.stock import runner as stock_runner
from ai.agents.stock import settings as stock_settings
from ai.agents.stock import slack as stock_slack
from ai.agents.stock import templates as stock_templates
from ai.agents.stock import weekly as stock_weekly
from ai.agents.stock.cards import STATE_PROPOSED, STATUS_CLOSING, STATUS_OPEN, STATUS_PARTLY_APPLIED
from ai.agents.stock.scoreboard import CLEARED, LOST, OUTCOMES, UNSCORED, WON

OUTCOME_ROWS = 100
CLIENT_FIELDS = ("state", "error", "reason", "carded", "stale", "card_id", "wait_for", "health_due")
OUTCOME_FIELDS = ("client_group_id", "creative_key", "ad_name", "treatment_ids", "result", "spend", "leads",
                  "cpl", "currency", "card_id", "line_id", "ad_id", "baseline_cpl", "live_since", "flagged",
                  "unscored_reason", "cleared_by", "cleared_at", "cleared_from", "created_at", "scored_at")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class ReportError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def _utcnow() -> datetime:   # the one clock for learned() and month_report(); patch it in tests
    return datetime.now(timezone.utc)


def with_id(doc: dict, drop: tuple = ()) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id" and k not in drop}
    out["id"] = doc["_id"]
    return out


def memory_to_api(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


def proposal_to_api(doc: dict) -> dict:
    out = memory_to_api(doc)
    out["rule_label"] = learning.RULE_LABELS.get(doc.get("rule_kind"), doc.get("rule_kind"))
    return out


def run_to_api(run: dict) -> dict:
    """The run as the report needs it: no owner id, no Slack channel, and per client only its
    outcome, not the working data a resumed tick keeps."""
    out = with_id(run, drop=("user_id", "claimed_at", "claim_kind", "in_flight", "tick_now", "tick_deadline", "open_preview", "slack",
                             "weekly"))
    out["slack"] = {"ts": (run.get("slack") or {}).get("ts")}
    out["clients"] = {g: {k: c[k] for k in CLIENT_FIELDS if k in c} for g, c in (run.get("clients") or {}).items()}
    return out


def card_to_api(card: dict, *, has_newer: bool = False) -> dict:
    out = with_id(card, drop=("user_id",))
    # Only a live card still waiting on a person can be applied. A preview card never can, a card a
    # newer one has replaced never can, and a partly applied one can only while it has lines left.
    # `has_newer` is sent too: a card with only failed lines left has can_apply false but can be retried.
    status = card.get("status")
    left = any(ln.get("state") == STATE_PROPOSED for ln in card.get("lines") or [])
    out["has_newer"] = has_newer
    out["can_apply"] = not card.get("preview") and not has_newer and (
        status == STATUS_OPEN or (status == STATUS_PARTLY_APPLIED and left))
    return out


async def card_for_api(db, card: dict) -> dict:
    # A preview is never replaced: it says what a run would do, and later live cards don't change that.
    has_newer = (not card.get("preview") and card.get("status") in (STATUS_OPEN, STATUS_PARTLY_APPLIED)
                 and await stock_apply.has_newer_card(db, card))
    return card_to_api(card, has_newer=has_newer)


async def scoreboard(db, agent_id: str) -> dict:
    rows = await db[OUTCOMES].find({"agent_id": agent_id}).sort("created_at", -1).to_list(length=None)
    results = [str(r.get("result") or "").lower() for r in rows]
    return {"won": results.count(WON), "lost": results.count(LOST),
            "moves": len(rows) - results.count(UNSCORED) - results.count(CLEARED),
            "rows": [{"id": r["_id"], **{k: r[k] for k in OUTCOME_FIELDS if k in r}} for r in rows[:OUTCOME_ROWS]]}


async def learned(db, agent_id: str, run: dict) -> list[dict]:
    """"What I learned" in a report: the lessons made since the report before it, up to this one's end
    (or now, while it is still open). Each can be kept or overruled."""
    if not run.get("as_of"):
        return []
    prev = await db[stock_runner.RUNS].find(
        {"agent_id": agent_id, "kind": {"$ne": stock_runner.PREVIEW}, "as_of": {"$lt": run["as_of"]}}
    ).sort("as_of", -1).limit(1).to_list(length=1)
    window = {"$lte": run.get("finished_at") or _utcnow().replace(tzinfo=None)}
    if prev:   # from where the report before it finished, so no lesson shows in two reports
        window["$gt"] = prev[0].get("finished_at") or prev[0]["as_of"]
    rows = await db[agent_store.MEMORY].find({"agent_id": agent_id, "created_at": window}).sort("created_at", 1).to_list(length=None)
    return [memory_to_api(m) for m in rows]


async def suggestions(db, agent_id: str) -> list[dict]:
    """The open "Make it a rule?" suggestions."""
    rows = await db[agent_store.PROPOSALS].find(
        {"agent_id": agent_id, "kind": stock_lessons.PROMOTE, "status": "open"}).sort("created_at", 1).to_list(length=None)
    return [proposal_to_api(p) for p in rows]


async def list_runs(db, agent_id: str, limit: int, before: Optional[datetime] = None) -> dict:
    """Past reports, newest first."""
    q: dict = {"agent_id": agent_id, "kind": {"$ne": stock_runner.PREVIEW}}
    if before is not None:
        q["as_of"] = {"$lt": stock_runner._naive(before)}
    rows = await db[stock_runner.RUNS].find(q).sort("as_of", -1).limit(limit + 1).to_list(length=limit + 1)
    more = len(rows) > limit
    runs = [run_to_api(r) for r in rows[:limit]]
    return {"runs": runs, "next_before": runs[-1]["as_of"] if more else None}


async def latest_run(db, agent_id: str, preview: bool = False) -> dict:
    """The newest report (or preview) and `open_cards`: how many of its cards wait on a person."""
    q = {"agent_id": agent_id, "kind": stock_runner.PREVIEW} if preview \
        else {"agent_id": agent_id, "kind": {"$ne": stock_runner.PREVIEW}}
    rows = await db[stock_runner.RUNS].find(q).sort("as_of", -1).limit(1).to_list(length=1)
    if not rows:
        return {"run": None, "open_cards": 0}
    open_cards = 0
    if not preview:
        waiting = db[stock_runner.CARDS].find(
            {"run_id": rows[0]["_id"], "agent_id": agent_id, "tag": "changes", "status": {"$in": [STATUS_OPEN, STATUS_PARTLY_APPLIED]}})
        open_cards = sum([1 async for c in waiting if (await card_for_api(db, c))["can_apply"]])
    return {"run": run_to_api(rows[0]), "open_cards": open_cards}


async def get_run(db, agent_id: str, run_id: str) -> dict:
    run = await db[stock_runner.RUNS].find_one({"_id": run_id, "agent_id": agent_id})
    if run is None:
        raise ReportError(404, "Run not found")
    cards = await db[stock_runner.CARDS].find(
        {"run_id": run_id, "agent_id": agent_id, "status": {"$ne": STATUS_CLOSING}}
    ).sort("created_at", 1).to_list(length=None)
    follow_ups = [{"client_group_id": c["client_group_id"], **f} for c in cards for f in c.get("follow_ups") or []]
    return {"run": run_to_api(run), "cards": [await card_for_api(db, c) for c in cards], "follow_ups": follow_ups,
            "scoreboard": await scoreboard(db, agent_id), "learned": await learned(db, agent_id, run),
            "proposals": await suggestions(db, agent_id), "weekly": stock_weekly.weekly_view(run)}


async def month_report(db, agent: dict, month: str) -> dict:
    """The whole-month report: per client, the month's leads, spend, CPL and zone, and how many cards were decided."""
    try:
        if not _MONTH_RE.match(month):
            raise ValueError(month)
        stock_months.parse_month(month)
    except ValueError:
        raise ReportError(400, "bad_month")
    if month > stock_months.current_month(agent, _utcnow()):
        raise ReportError(400, "future_month")
    return await stock_months.month_rows(db, agent, month)


async def lessons(db, agent_id: str) -> list[dict]:
    """Every lesson, overruled ones included, oldest first."""
    return [{"uses": 0, "last_used": [], **memory_to_api(m)}
            for m in await agent_store.list_memory(db, agent_id, active_only=False)]


def stored_templates(agent: dict) -> dict:
    return ((agent.get("settings") or {}).get("slack") or {}).get("templates") or {}


def check_template_kind(kind: str) -> str:
    if kind not in stock_settings.TEMPLATE_KINDS:
        raise ReportError(404, f"No template called {kind!r}")
    return kind


def template_view(kind: str, agent: dict) -> dict:
    stored = stored_templates(agent).get(kind)
    return {"kind": kind, "customised": bool(stored), "template": stored or stock_templates.DEFAULT_TEMPLATES[kind],
            "defaults": stock_templates.DEFAULT_TEMPLATES[kind], "placeholders": list(stock_templates.KIND_TOKENS[kind]),
            "notice": ((agent.get(stock_slack.NOTICES) or {}).get(kind) or {}).get("message")}


CARD_LIST_STATUSES = {"open": [STATUS_OPEN, STATUS_PARTLY_APPLIED], "applied": ["applied"], "dismissed": ["dismissed"]}


async def list_cards(db, agent_id: str, status: str, limit: int) -> list[dict]:
    """Live cards (never previews) newest first, by decision state. 'open' is the ones waiting on a person."""
    q: dict = {"agent_id": agent_id, "preview": {"$ne": True}, "status": {"$ne": STATUS_CLOSING}}
    if status != "all":
        q["status"] = {"$in": CARD_LIST_STATUSES[status]}
    rows = await db[stock_runner.CARDS].find(q).sort("created_at", -1).limit(limit).to_list(length=limit)
    return [await card_for_api(db, c) for c in rows]
