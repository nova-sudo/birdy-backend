"""
routers/agent_stock.py
----------------------
The Birdy Media Buying Agent's API: settings, preview, runs and reports, cards, scoreboard and
lessons. Owner-scoped like routers/agents.py: another user's agent is a 404, a custom agent a 400.
"""

import copy
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai.agents import claims
from ai.agents import store as agent_store
from ai.agents import history as ad_history
from ai.agents.stock import add as stock_add
from ai.agents.stock import apply as stock_apply
from ai.agents.stock import lessons as stock_lessons
from ai.agents.stock import runner as stock_runner
from ai.agents.stock import settings as stock_settings
from ai.agents.stock import templates as stock_templates
from ai.agents.stock.cards import (STATE_PROPOSED, STATUS_CLOSING, STATUS_FLAGGED, STATUS_OPEN, STATUS_PARTLY_APPLIED,
                                   guarded_update)
from ai.agents.stock.scoreboard import CLEARED, LOST, OUTCOMES, UNSCORED, WON, clear_row
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from routers import agents as agents_router
from services import client_targets

router = APIRouter(prefix="/api/agents", tags=["agent-stock"])
logger = logging.getLogger(__name__)

PREVIEW_BUDGET_SECONDS = 170   # of Vercel's 300 s; with the runner's capped reads the request ends well inside it
OUTCOME_ROWS = 100
CARD_EDITABLE = ("additional_changes", "thought_process", "next_steps")
APPLY_BUDGET_SECONDS = 20      # the dashboard waits this long for lines in the request; the cron finishes the rest


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _stock_agent(db, user_id: str, agent_id: str) -> dict:
    agent = await agents_router._agent_or_404(db, user_id, agent_id)
    if agent.get("type") != agent_store.TYPE_STOCK:
        raise HTTPException(status_code=400, detail="Only the Birdy Media Buying Agent has this")
    return agent


def _with_id(doc: dict, drop: tuple = ()) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id" and k not in drop}
    out["id"] = doc["_id"]
    return out


CLIENT_FIELDS = ("state", "error", "reason", "carded", "stale", "card_id", "wait_for", "health_due")
OUTCOME_FIELDS = ("client_group_id", "creative_key", "ad_name", "treatment_ids", "result", "spend", "leads",
                  "cpl", "currency", "card_id", "line_id", "ad_id", "baseline_cpl", "live_since", "flagged",
                  "unscored_reason", "cleared_by", "cleared_at", "cleared_from", "created_at", "scored_at")


def _run_to_api(run: dict) -> dict:
    """The run as the report needs it: no owner id, no Slack channel, and per client only its
    outcome, not the working data a resumed tick keeps."""
    out = _with_id(run, drop=("user_id", "claimed_at", "in_flight", "tick_now", "tick_deadline", "open_preview", "slack"))
    out["slack"] = {"ts": (run.get("slack") or {}).get("ts")}
    out["clients"] = {g: {k: c[k] for k in CLIENT_FIELDS if k in c} for g, c in (run.get("clients") or {}).items()}
    return out


def _card_to_api(card: dict, *, has_newer: bool = False) -> dict:
    out = _with_id(card, drop=("user_id",))
    # Only a live card still waiting on a person can be applied. A preview card never can, and a
    # partly applied one can only while it has lines left and no newer card has replaced it.
    status = card.get("status")
    left = any(ln.get("state") == STATE_PROPOSED for ln in card.get("lines") or [])
    out["can_apply"] = not card.get("preview") and (
        status == STATUS_OPEN or (status == STATUS_PARTLY_APPLIED and left and not has_newer))
    return out


async def _card_for_api(db, card: dict) -> dict:
    has_newer = card.get("status") == STATUS_PARTLY_APPLIED and await stock_apply.has_newer_card(db, card)
    return _card_to_api(card, has_newer=has_newer)


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
        patch = body.settings or {}
        errors = stock_settings.shape_errors(patch)
        if not errors and (patch.get("slack") or {}).get("templates"):
            patch = copy.deepcopy(patch)
            for kind, t in patch["slack"]["templates"].items():
                if t is None:
                    continue
                try:
                    patch["slack"]["templates"][kind] = stock_templates.validate_template(kind, t)
                except stock_templates.TemplateError as e:
                    errors += [{"field": f"slack.templates.{kind}", "message": m} for m in e.errors]
        if errors:
            return JSONResponse(status_code=400, content={"detail": "Some settings are invalid", "errors": errors})
        await agents_router._require_owned_groups(db, current_user, (patch.get("clients") or {}).keys())

        current = agent.get("settings") or copy.deepcopy(stock_settings.DEFAULT_SETTINGS)
        merged = stock_settings.merge_settings(current, patch)
        update: dict = {"settings": merged}

        if body.client_group_ids is not None:
            ids = list(dict.fromkeys(body.client_group_ids))
            await agents_router._require_owned_groups(db, current_user, ids)
            scope = agent.get("scope") or {}
            before = list(scope.get("client_group_ids") or [])
            flag_only = [g for g in (body.only_flag if body.only_flag is not None
                                     else scope.get(agents_router.FLAG_ONLY_KEY) or []) if g in ids]
            update["scope"] = {**scope, "client_group_ids": ids, agents_router.FLAG_ONLY_KEY: flag_only}
            if agent.get("status") == "active":
                resp = await agents_router._claim_scope(db, current_user, agent_id, ids,
                                                        move=body.move_claims, only_flag=flag_only)
                if resp is not None:
                    return resp
                await claims.release_clients(db, current_user, agent_id, set(before) - set(ids))
            # First add: the green limit starts from the client's CPL target, if it has one.
            added = [g for g in ids if g not in before]
            groups = {g["id"]: g async for g in db["client_groups"].find(
                {"id": {"$in": added}, "user_id": current_user})}
            for gid in added:
                cpl = client_targets.clean((groups.get(gid) or {}).get("targets")).get("cpl")
                entry = merged["clients"].get(gid) or {}
                if cpl is not None and (entry.get("zones") or {}).get("green_under") is None:
                    merged["clients"][gid] = {**entry, "zones": {**(entry.get("zones") or {}), "green_under": cpl}}

        updated = await agent_store.update_agent(db, current_user, agent_id, update)
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
        not_ready = [g for g in watched if not ad_history.is_ready(states.get(g))]
        if not_ready:
            return JSONResponse(status_code=409, content={
                "detail": "Ad history isn't ready yet", "not_ready": not_ready})
        now = _utcnow()
        run = await stock_runner.get_or_create_run(db, agent, now, kind=stock_runner.PREVIEW)
        if run["phase"] != stock_runner.DONE:
            run = await stock_runner.advance_run(db, mongo_client, run, agent, deadline=deadline, now_utc=now)
        return {"run_id": run["_id"], "kind": run["kind"], "phase": run["phase"],
                "done": run["phase"] == stock_runner.DONE}


# ---------------------------------------------------------------------------
# Runs and reports
# ---------------------------------------------------------------------------

async def _scoreboard(db, agent_id: str) -> dict:
    rows = await db[OUTCOMES].find({"agent_id": agent_id}).sort("created_at", -1).to_list(length=None)
    results = [str(r.get("result") or "").lower() for r in rows]
    return {"won": results.count(WON), "lost": results.count(LOST),
            "moves": len(rows) - results.count(UNSCORED) - results.count(CLEARED),
            "rows": [{"id": r["_id"], **{k: r[k] for k in OUTCOME_FIELDS if k in r}} for r in rows[:OUTCOME_ROWS]]}


async def _learned(db, agent_id: str, run: dict) -> list[dict]:
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
    return [agents_router._memory_to_api(m) for m in rows]


async def _suggestions(db, agent_id: str) -> list[dict]:
    """The open "Make it a rule?" suggestions."""
    rows = await db[agent_store.PROPOSALS].find(
        {"agent_id": agent_id, "kind": stock_lessons.PROMOTE, "status": "open"}).sort("created_at", 1).to_list(length=None)
    return [agents_router._proposal_to_api(p) for p in rows]


@router.get("/{agent_id}/runs")
async def list_runs(agent_id: str, limit: int = Query(20, ge=1, le=100), before: Optional[datetime] = None,
                    current_user: str = Depends(get_current_user)):
    """Past reports, newest first. Pass `next_before` back as `before` for the next page."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        q: dict = {"agent_id": agent_id, "kind": {"$ne": stock_runner.PREVIEW}}
        if before is not None:
            q["as_of"] = {"$lt": stock_runner._naive(before)}
        rows = await db[stock_runner.RUNS].find(q).sort("as_of", -1).limit(limit + 1).to_list(length=limit + 1)
        more = len(rows) > limit
        runs = [_run_to_api(r) for r in rows[:limit]]
        return {"runs": runs, "next_before": runs[-1]["as_of"] if more else None}


@router.get("/{agent_id}/runs/latest")
async def latest_run(agent_id: str, kind: Optional[str] = Query(None, pattern="^preview$"),
                     current_user: str = Depends(get_current_user)):
    """The newest report, or with kind=preview the newest preview."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        q = {"agent_id": agent_id, "kind": stock_runner.PREVIEW} if kind \
            else {"agent_id": agent_id, "kind": {"$ne": stock_runner.PREVIEW}}
        rows = await db[stock_runner.RUNS].find(q).sort("as_of", -1).limit(1).to_list(length=1)
        return {"run": _run_to_api(rows[0]) if rows else None}


@router.get("/{agent_id}/runs/{run_id}")
async def get_run(agent_id: str, run_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        run = await db[stock_runner.RUNS].find_one({"_id": run_id, "agent_id": agent_id})
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        cards = await db[stock_runner.CARDS].find(
            {"run_id": run_id, "agent_id": agent_id, "status": {"$ne": STATUS_CLOSING}}
        ).sort("created_at", 1).to_list(length=None)
        follow_ups = [{"client_group_id": c["client_group_id"], **f}
                      for c in cards for f in c.get("follow_ups") or []]
        # weekly is filled in by the weekly message.
        return {"run": _run_to_api(run), "cards": [await _card_for_api(db, c) for c in cards], "follow_ups": follow_ups,
                "scoreboard": await _scoreboard(db, agent_id), "learned": await _learned(db, agent_id, run),
                "proposals": await _suggestions(db, agent_id), "weekly": None}


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
        agent = await _stock_agent(db, current_user, agent_id)
        card = await db[stock_runner.CARDS].find_one(
            {"_id": card_id, "agent_id": agent_id, "status": {"$ne": STATUS_CLOSING}})
        if card is None:
            raise HTTPException(status_code=404, detail="Card not found")
        if card.get("preview"):
            raise HTTPException(status_code=409, detail="A preview card can't be edited")
        if card.get("status") not in (STATUS_OPEN, STATUS_FLAGGED):
            raise HTTPException(status_code=409, detail="This card has already been decided")
        patch = {k: getattr(body, k) for k in CARD_EDITABLE if getattr(body, k) is not None}
        if body.lines is not None:
            stored = {ln["id"]: ln for ln in card.get("lines") or []}
            ids = [ln.get("id") for ln in body.lines]
            if any(i not in stored for i in ids) or len(set(ids)) != len(ids):
                raise HTTPException(status_code=400, detail="Lines must be existing lines of this card")
            if any(ln.get("state") not in ("proposed", "flagged", "needs_file")
                   for i, ln in stored.items() if i not in ids):
                raise HTTPException(status_code=409, detail="A line that is being or has been applied can't be removed")
            patch["lines"] = [stored[i] for i in ids]
        if patch and not await guarded_update(db, card, patch):
            raise HTTPException(status_code=409, detail="This card changed while you were editing it. Reload it")
        saved = await db[stock_runner.CARDS].find_one({"_id": card_id})
        if patch:
            try:
                await stock_lessons.lesson_from_edit(db, agent, saved, current_user)
            except Exception as e:  # noqa: BLE001 - a lesson is never worth failing a saved edit
                logger.warning("stock card %s: could not record a lesson from the edit: %s", card_id, e)
        return _card_to_api(saved)


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
    if not await db[stock_runner.CARDS].find_one({"_id": card_id, "agent_id": agent_id, "user_id": user_id}, {"_id": 1}):
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
# Slack templates
# ---------------------------------------------------------------------------

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://birdy-beta.vercel.app").rstrip("/")


class TemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: Optional[Any] = None


def _kind_or_404(kind: str) -> str:
    if kind not in stock_settings.TEMPLATE_KINDS:
        raise HTTPException(status_code=404, detail=f"No template called {kind!r}")
    return kind


def _stored_templates(agent: dict) -> dict:
    return ((agent.get("settings") or {}).get("slack") or {}).get("templates") or {}


def _template_error(e: stock_templates.TemplateError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "This template can't be used", "errors": e.errors})


def _template_view(kind: str, agent: dict) -> dict:
    stored = _stored_templates(agent).get(kind)
    return {"kind": kind, "customised": bool(stored), "template": stored or stock_templates.DEFAULT_TEMPLATES[kind],
            "defaults": stock_templates.DEFAULT_TEMPLATES[kind], "placeholders": list(stock_templates.KIND_TOKENS[kind])}


async def _save_template(db, agent: dict, user_id: str, kind: str, template: dict | None) -> dict:
    """None resets it: merge_settings drops a null template, so it is really gone."""
    current = agent.get("settings") or copy.deepcopy(stock_settings.DEFAULT_SETTINGS)
    merged = stock_settings.merge_settings(current, {"slack": {"templates": {kind: template}}})
    return await agent_store.update_agent(db, user_id, agent["_id"], {"settings": merged})


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


@router.get("/{agent_id}/lessons")
async def get_lessons(agent_id: str, current_user: str = Depends(get_current_user)):
    """Every lesson, overruled ones included, oldest first."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _stock_agent(db, current_user, agent_id)
        return [agents_router._memory_to_api(m) for m in await agent_store.list_memory(db, agent_id, active_only=False)]


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
