"""
routers/agent_stock.py
----------------------
The Birdy Media Buying Agent's API: settings, preview, runs and reports, cards, scoreboard and
lessons. Owner-scoped like routers/agents.py: another user's agent is a 404, a custom agent a 400.
"""

from contextlib import contextmanager
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai import conversation_log
from ai.agents import access as agent_access
from ai.agents import store as agent_store
from ai.agents import history as ad_history
from ai.agents.stock import add as stock_add
from ai.agents.stock import agent_settings
from ai.agents.stock import apply as stock_apply
from ai.agents.stock import cards as stock_cards
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock import reports
from ai.agents.stock import runner as stock_runner
from ai.agents.stock import templates as stock_templates
from ai.agents.stock.apply import APPLY_BUDGET_SECONDS
from ai.agents.stock.scoreboard import clear_row
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from routers import agents as agents_router

router = APIRouter(prefix="/api/agents", tags=["agent-stock"])
logger = logging.getLogger(__name__)

PREVIEW_BUDGET_SECONDS = 170   # of Vercel's 300 s; with the runner's capped reads the request ends well inside it


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _stock_agent(db, user_id: str, agent_id: str) -> dict:
    agent = await agents_router._agent_or_404(db, user_id, agent_id)
    if agent.get("type") != agent_store.TYPE_STOCK:
        raise HTTPException(status_code=400, detail="Only the Birdy Media Buying Agent has this")
    return agent


_with_id = reports.with_id
_run_to_api = reports.run_to_api
_card_to_api = reports.card_to_api
_card_for_api = reports.card_for_api
CLIENT_FIELDS, OUTCOME_FIELDS = reports.CLIENT_FIELDS, reports.OUTCOME_FIELDS


@contextmanager
def _http_errors():
    """The shared report functions raise ReportError; here it is an HTTP error."""
    try:
        yield
    except reports.ReportError as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: Optional[dict] = None
    client_group_ids: Optional[list[str]] = None   # the watched clients; keeps scope in sync
    move_claims: bool = False
    only_flag: Optional[list[str]] = None


@router.patch("/{agent_id}/settings")
async def patch_settings(agent_id: str, body: SettingsPatch,
                         current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await agents_router._agent_or_404(db, current_user, agent_id)
        if agent.get("type") != agent_store.TYPE_STOCK:
            raise HTTPException(status_code=400, detail="Only the Birdy Media Buying Agent has these settings")
        try:
            updated = await agent_settings.update_settings(
                db, current_user, agent, settings=body.settings, client_group_ids=body.client_group_ids,
                move_claims=body.move_claims, only_flag=body.only_flag)
        except agent_settings.SettingsError as e:
            return JSONResponse(status_code=e.status, content=e.body)
        return agents_router._agent_to_api(updated)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

@router.post("/{agent_id}/preview")
async def start_preview(agent_id: str, current_user: str = Depends(get_current_user)):
    """A run on real data that writes nothing to Meta or Slack and never calls the urgent hook.
    It works inside this request's budget and the tick finishes what is left."""
    deadline = time.monotonic() + PREVIEW_BUDGET_SECONDS
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _stock_agent(db, current_user, agent_id)
        gids = list((agent.get("scope") or {}).get("client_group_ids") or [])
        if not gids:
            return JSONResponse(status_code=409, content={"detail": "Choose at least one client first"})
        watched = [g["id"] async for g in db["client_groups"].find(
            {"id": {"$in": gids}, "user_id": current_user, "meta_ad_account_id": {"$nin": [None, ""]}})]
        states = await ad_history.statuses(db, watched)
        # Only a client still syncing holds the preview up. One Meta won't let Birdy read may stay that
        # way for good, so it shows as having no data, unless no client at all can be read.
        ready = [g for g in watched if ad_history.is_ready(states.get(g))]
        not_ready = [g for g in watched if g not in ready and states[g]["status"] != ad_history.STATUS_ERROR]
        if not_ready:
            return JSONResponse(status_code=409, content={
                "detail": "Ad history isn't ready yet", "not_ready": not_ready})
        if watched and not ready:
            return JSONResponse(status_code=409, content={
                "detail": "Birdy can't read ad history from Meta for any of your clients, so there's nothing to preview yet.",
                "unreadable": watched})
        now = _utcnow()
        run = await stock_runner.get_or_create_run(db, agent, now, kind=stock_runner.PREVIEW)
        if run["phase"] != stock_runner.DONE:
            run = await stock_runner.advance_run(db, mongo_client, run, agent, deadline=deadline, now_utc=now)
        return {"run_id": run["_id"], "kind": run["kind"], "phase": run["phase"],
                "done": run["phase"] == stock_runner.DONE}


# ---------------------------------------------------------------------------
# Runs and reports
# ---------------------------------------------------------------------------

_scoreboard = reports.scoreboard
_learned = reports.learned
_suggestions = reports.suggestions


@router.get("/{agent_id}/runs")
async def list_runs(agent_id: str, limit: int = Query(20, ge=1, le=100), before: Optional[datetime] = None,
                    current_user: str = Depends(get_current_user)):
    """Past reports, newest first. Pass `next_before` back as `before` for the next page."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        return await reports.list_runs(db, agent_id, limit, before)


@router.get("/{agent_id}/runs/latest")
async def latest_run(agent_id: str, kind: Optional[str] = Query(None, pattern="^preview$"),
                     current_user: str = Depends(get_current_user)):
    """The newest report, or with kind=preview the newest preview. `open_cards` is how many of its cards wait on a
    person, for the nav badge: a card proposing changes that passes its own `can_apply` rule. Green and no-changes cards
    pass that rule too but have nothing to decide. No scoreboard or lessons are built here."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        return await reports.latest_run(db, agent_id, preview=bool(kind))


@router.get("/{agent_id}/runs/{run_id}")
async def get_run(agent_id: str, run_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        with _http_errors():
            return await reports.get_run(db, agent_id, run_id)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

class CardPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lines: Optional[list[dict]] = None   # only keeps the listed lines, in this order
    additional_changes: Optional[str] = None
    thought_process: Optional[str] = None
    next_steps: Optional[str] = None


@router.patch("/{agent_id}/cards/{card_id}")
async def patch_card(agent_id: str, card_id: str, body: CardPatch, current_user: str = Depends(get_current_user)):
    """Edit a card still waiting on a person. `original` stays as Birdy wrote it. A line can be
    dropped or reordered, nothing else about it changes: what it does comes from the stored line."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        res = await stock_cards.edit_card(
            db, current_user, card_id, lines=None if body.lines is None else [ln.get("id") for ln in body.lines],
            additional_changes=body.additional_changes, thought_process=body.thought_process,
            next_steps=body.next_steps, by=current_user, agent_id=agent_id)
        if not res["ok"]:
            status = {stock_cards.EDIT_NOT_FOUND: 404, stock_cards.EDIT_BAD_LINES: 400}.get(res["error"], 409)
            raise HTTPException(status_code=status, detail=res["reason"])
        saved = res["card"]
        return await _card_for_api(db, saved)


class ApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_ids: Optional[list[str]] = None   # naming a failed or blocked line retries it


class DismissBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: Optional[str] = None


class UndoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: str


async def _require_card(db, agent_id: str, card_id: str, user_id: str) -> None:
    """The card must be this agent's. A different agent's card is a 404, like another user's."""
    if await agent_access.find_card(db, user_id, agent_id, card_id) is None:
        raise HTTPException(status_code=404, detail="Card not found")


DECISION_STATUS = {"not_found": 404, "bad_lines": 400, "refused": 409, "expired": 410}


async def _decision_response(db, res: dict, *, more: Optional[dict] = None) -> dict:
    """A refusal is an HTTP error with the plain reason. Anything else that happened, a blocked or
    failed line included, is a 200 carrying each line's result. `more` lists further HTTP errors."""
    code = {**DECISION_STATUS, **(more or {})}.get(res["outcome"])
    if code:
        raise HTTPException(status_code=code, detail=res.get("reason") or "Card not found")
    return {**res, "card": await _card_for_api(db, res["card"])}


@router.post("/{agent_id}/cards/{card_id}/apply")
async def apply_card(agent_id: str, card_id: str, body: ApplyBody = ApplyBody(), current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        await _require_card(db, agent_id, card_id, current_user)
        res = await stock_apply.apply_card(db, mongo_client, current_user, card_id, by=current_user, source="dashboard",
                                           line_ids=body.line_ids, budget_s=APPLY_BUDGET_SECONDS)
        return await _decision_response(db, res)


@router.post("/{agent_id}/cards/{card_id}/dismiss")
async def dismiss_card(agent_id: str, card_id: str, body: DismissBody = DismissBody(), current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        await _require_card(db, agent_id, card_id, current_user)
        res = await stock_apply.dismiss_card(db, current_user, card_id, by=current_user, source="dashboard",
                                             reason=body.reason)
        if res["outcome"] in ("busy", "already") and not res["ok"]:
            raise HTTPException(status_code=409, detail=res["reason"])
        return await _decision_response(db, res)


@router.post("/{agent_id}/cards/{card_id}/undo")
async def undo_line(agent_id: str, card_id: str, body: UndoBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        await _require_card(db, agent_id, card_id, current_user)
        res = await stock_apply.undo_line(db, mongo_client, current_user, card_id, body.line_id, by=current_user,
                                          source="dashboard")
        return await _decision_response(db, res, more={"not_undoable": 409, "busy": 409, "blocked": 409, "failed": 502})


class FileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blob_url: str


@router.post("/{agent_id}/cards/{card_id}/lines/{line_id}/file")
async def attach_line_file(agent_id: str, card_id: str, line_id: str, body: FileBody,
                           current_user: str = Depends(get_current_user)):
    """Keep a person's upload of an Add's video, for this line and any later Add of that creative."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        await _require_card(db, agent_id, card_id, current_user)
        res = await stock_add.attach_file(db, current_user, card_id, line_id, body.blob_url)
        return await _decision_response(db, res, more={"bad_file": 400, "refused": 409})


@router.post("/{agent_id}/runs/{run_id}/apply-all")
async def apply_all(agent_id: str, run_id: str, current_user: str = Depends(get_current_user)):
    """Every open Ask me card in the run. What doesn't fit in the budget is finished by the cron."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        if not await db[stock_runner.RUNS].find_one({"_id": run_id, "agent_id": agent_id}, {"_id": 1}):
            raise HTTPException(status_code=404, detail="Run not found")
        res = await stock_apply.apply_run(db, mongo_client, current_user, run_id, by=current_user, source="dashboard",
                                          budget_s=APPLY_BUDGET_SECONDS)
        code = DECISION_STATUS.get(res["outcome"])
        if code:
            raise HTTPException(status_code=code, detail=res.get("reason") or "Run not found")
        return res


# ---------------------------------------------------------------------------
# Weekly report
# ---------------------------------------------------------------------------

@router.post("/{agent_id}/runs/{run_id}/weekly/post")
async def post_weekly(agent_id: str, run_id: str, current_user: str = Depends(get_current_user)):
    """Post the run's weekly message to Slack. Pressing again says it is already posted; it never posts twice."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _stock_agent(db, current_user, agent_id)
        with _http_errors():
            return await reports.post_weekly_report(db, agent, run_id, by=current_user)


# ---------------------------------------------------------------------------
# Slack templates
# ---------------------------------------------------------------------------

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://birdy-beta.vercel.app").rstrip("/")


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: Optional[Any] = None


def _kind_or_404(kind: str) -> str:
    with _http_errors():
        return reports.check_template_kind(kind)


_stored_templates = reports.stored_templates
_template_view = reports.template_view


def _template_error(e: stock_templates.TemplateError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "This template can't be used", "errors": e.errors})


_save_template = reports.save_template


@router.get("/{agent_id}/slack-templates/{kind}")
async def get_slack_template(agent_id: str, kind: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        agent = await _stock_agent(mongo_client[DB_NAME], current_user, agent_id)
        return _template_view(_kind_or_404(kind), agent)


@router.put("/{agent_id}/slack-templates/{kind}")
async def put_slack_template(agent_id: str, kind: str, body: TemplateBody, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _stock_agent(db, current_user, agent_id)
        _kind_or_404(kind)
        try:
            clean = stock_templates.validate_template(kind, body.model_dump())
        except stock_templates.TemplateError as e:
            return _template_error(e)
        return _template_view(kind, await _save_template(db, agent, current_user, kind, clean))


@router.delete("/{agent_id}/slack-templates/{kind}")
async def reset_slack_template(agent_id: str, kind: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _stock_agent(db, current_user, agent_id)
        _kind_or_404(kind)
        return _template_view(kind, await _save_template(db, agent, current_user, kind, None))


@router.post("/{agent_id}/slack-templates/{kind}/preview")
async def preview_slack_template(agent_id: str, kind: str, body: TemplateBody = TemplateBody(),
                                 current_user: str = Depends(get_current_user)):
    """The template (the unsaved draft in the body, else the saved one) rendered with sample values.
    Nothing is read from Meta or sent to Slack."""
    async with get_mongo_client() as mongo_client:
        agent = await _stock_agent(mongo_client[DB_NAME], current_user, agent_id)
        _kind_or_404(kind)
        try:
            template = (stock_templates.validate_template(kind, body.model_dump()) if body.blocks is not None
                        else stock_templates.validate_template(kind, _template_view(kind, agent)["template"]))
            currency = ((agent.get("settings") or {}).get("zones") or {}).get("currency") or "GBP"
            blocks, text = stock_templates.sample_preview(kind, template, now=_utcnow(), currency=currency,
                                                          frontend_url=FRONTEND_URL)
        except stock_templates.TemplateError as e:
            return _template_error(e)
        return {"kind": kind, "blocks": blocks, "text": text, "sample": True}


# ---------------------------------------------------------------------------
# Scoreboard and lessons
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/outcomes")
async def get_outcomes(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        return await _scoreboard(db, agent_id)


@router.get("/{agent_id}/cards/{card_id}/chat")
async def get_card_chat(agent_id: str, card_id: str, current_user: str = Depends(get_current_user)):
    """The latest Birdy chat about this card, so the card's chat shows its history after a reload."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        if await db[agent_store.CARDS].find_one({"_id": card_id, "agent_id": agent_id}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="Card not found")
        return await conversation_log.card_transcript(db, current_user, card_id)


@router.get("/{agent_id}/months/{month}")
async def get_month(agent_id: str, month: str, current_user: str = Depends(get_current_user)):
    """The whole-month report: per client, the month's leads, spend, CPL and zone, and how many cards were decided."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _stock_agent(db, current_user, agent_id)
        with _http_errors():
            return await reports.month_report(db, agent, month)


@router.get("/{agent_id}/lessons")
async def get_lessons(agent_id: str, current_user: str = Depends(get_current_user)):
    """Every lesson, overruled ones included, oldest first."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        return await reports.lessons(db, agent_id)


@router.post("/{agent_id}/lessons/{lesson_id}/keep")
async def keep_lesson(agent_id: str, lesson_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        lesson = await stock_lessons.keep_lesson(db, agent_id, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return agents_router._memory_to_api(lesson)


@router.post("/{agent_id}/lessons/{lesson_id}/overrule")
async def overrule_lesson(agent_id: str, lesson_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        lesson = await stock_lessons.overrule_lesson(db, agent_id, lesson_id)
        if lesson is None:
            raise HTTPException(status_code=404, detail="Lesson not found")
        return agents_router._memory_to_api(lesson)


@router.post("/{agent_id}/outcomes/{row_id}/clear")
async def clear_outcome(agent_id: str, row_id: str, current_user: str = Depends(get_current_user)):
    """A result is wrong: take it out of the record. The row stays listed as cleared, with who and when."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        row = await clear_row(db, agent_id, row_id, by=current_user, now=_utcnow())
        if row is None:
            raise HTTPException(status_code=404, detail="Result not found")
        return {"id": row["_id"], **{k: row[k] for k in OUTCOME_FIELDS if k in row}}
