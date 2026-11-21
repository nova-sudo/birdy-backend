"""
routers/team_setup.py
---------------------
Agent onboarding (ai/team/setup.py). Owner-scoped; refusals as JSON {"detail": ...} with their status,
423 {"detail": {"code": "agent_locked", "agent", "waits_on"}} for a locked agent.

  GET  /api/team/setup                                   -> state
  PUT  /api/team/setup/{agent}/{step}  body              -> state (409 out of order, 400 bad body)
  POST /api/team/setup/media_buyer/practice              -> {"job_id", "status"}
  GET  /api/team/setup/media_buyer/practice              -> {"status", "summary", "job_id"}
  GET  /api/team/setup/sales_coach/outcome-options       -> {"tags": [...], "pipelines": [...]}
  POST /api/team/setup/sub_agents/finish {"skipped"}     -> state

The practice run is the stock agent's preview (POST /api/agents/{id}/preview's own checks and run): it
reads real data and writes nothing to Meta or Slack. It works inside this request's budget and the
agent tick finishes what is left.
"""

import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from ai.team import setup as team_setup
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client

router = APIRouter(prefix="/api/team/setup", tags=["team-setup"])

PREVIEW_BUDGET_SECONDS = 170   # the same budget as POST /api/agents/{id}/preview


def _error(e: team_setup.SetupError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail, **e.extra})


async def _run(fn, *args):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await fn(db, *args)
        except team_setup.SetupError as e:
            return _error(e)


@router.get("")
async def get_setup(current_user: str = Depends(get_current_user)):
    return await _run(team_setup.state, current_user)


@router.get("/media_buyer/practice")
async def get_practice(current_user: str = Depends(get_current_user)):
    return await _run(team_setup.practice_status, current_user)


@router.post("/media_buyer/practice")
async def start_practice(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await start_practice_run(db, mongo_client, current_user)
        except team_setup.SetupError as e:
            return _error(e)


async def start_practice_run(db, mongo_client, user_id: str) -> dict:
    """The stock agent's preview, with the same checks as POST /api/agents/{id}/preview."""
    from ai.agents.stock import runner as stock_runner
    from routers.agent_stock import preview_blocker
    deadline = time.monotonic() + PREVIEW_BUDGET_SECONDS
    st = await team_setup.status_of(db, user_id, team_setup.MEDIA_BUYER)
    if st["status"] == team_setup.LOCKED:
        raise team_setup.SetupError(423, team_setup.locked_body(team_setup.MEDIA_BUYER, st["waits_on"]))
    agent = await team_setup.stock_agent(db, user_id)
    if agent is None:
        raise team_setup.SetupError(409, "Choose at least one client first")
    blocker = await preview_blocker(db, user_id, agent)
    if blocker is not None:
        raise team_setup.SetupError.from_body(409, blocker)
    now = datetime.now(timezone.utc)
    run = await stock_runner.get_or_create_run(db, agent, now, kind=stock_runner.PREVIEW)
    if run["phase"] != stock_runner.DONE:
        run = await stock_runner.advance_run(db, mongo_client, run, agent, deadline=deadline, now_utc=now)
    return {"job_id": run["_id"], "status": "done" if run["phase"] == stock_runner.DONE else "running"}


@router.get("/sales_coach/outcome-options")
async def outcome_options(current_user: str = Depends(get_current_user)):
    from ai.team import sales_coach
    return await _run(sales_coach.outcome_options, current_user)


@router.post("/sub_agents/finish")
async def finish_sub_agents(body: Any = Body(default=None), current_user: str = Depends(get_current_user)):
    skipped = body.get("skipped", False) if isinstance(body, dict) else None
    return await _run(team_setup.finish_sub_agents, current_user, skipped)


@router.put("/{agent}/{step}")
async def put_step(agent: str, step: str, body: Any = Body(default=None),
                   current_user: str = Depends(get_current_user)):
    return await _run(team_setup.save_step, current_user, agent, step, body if body is not None else {})
