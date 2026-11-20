"""
routers/sub_agents.py
---------------------
Sub-agents under Media Buyer and Sales Coach (ai/agents/sub_agents). Same conventions as
routers/agents.py: owner-scoped (another user's sub-agent is a 404, never a 403), refusals as
{detail} JSON with their status (here also a `code`).

There is no create route: a main agent proposes a sub-agent from its chat (propose_tool.py) and the
owner approves or rejects it here.

This router is included in main.py BEFORE routers/agents.py, so /api/agents/sub is not taken as
GET /api/agents/{agent_id} with agent_id "sub".
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai.agents.sub_agents import core as sub_core
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client

router = APIRouter(prefix="/api/agents/sub", tags=["sub-agents"])

Parent = Literal["media_buyer", "sales_coach"]
Status = Literal["proposed", "active", "paused"]
Kind = Literal["specialist", "custom"]


def _err(e: sub_core.SubAgentError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.body)


@router.get("")
async def list_sub_agents(parent: Optional[Parent] = None, status: Optional[Status] = None,
                          kind: Optional[Kind] = None, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        subs = await sub_core.list_sub_agents(db, current_user, parent, status=status, kind=kind)
        return await sub_core.to_api_many(db, current_user, subs)


# Declared before /{sub_agent_id}, so "tools" is never read as a sub-agent id.
@router.get("/tools")
async def parent_tools(parent: Optional[str] = None, current_user: str = Depends(get_current_user)):
    """The tools a main agent can give its sub-agents (exactly the set the editor's save accepts):
    {"tools": [{name, label}]}, sorted by label."""
    try:
        sub_core.check_parent(parent)
    except sub_core.SubAgentError as e:
        return _err(e)
    from ai.agents.sub_agents.tools import parent_tool_names, tool_label
    tools = [{"name": t, "label": tool_label(t)} for t in parent_tool_names(parent)]
    return {"tools": sorted(tools, key=lambda t: (t["label"].casefold(), t["name"]))}


@router.get("/{sub_agent_id}")
async def get_sub_agent(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        sub = await sub_core.get_sub_agent(db, current_user, sub_agent_id)
        if sub is None:
            return JSONResponse(status_code=404, content={"detail": "Sub-agent not found", "code": "not_found"})
        return await sub_core.to_api(db, current_user, sub)


async def _act(fn, current_user: str, sub_agent_id: str, *args):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            out = await fn(db, current_user, sub_agent_id, *args)
        except sub_core.SubAgentError as e:
            return _err(e)
        return out if "deleted" in out else await sub_core.to_api(db, current_user, out)


@router.post("/{sub_agent_id}/approve")
async def approve(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    return await _act(sub_core.approve_sub_agent, current_user, sub_agent_id)


@router.post("/{sub_agent_id}/reject")
async def reject(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    return await _act(sub_core.reject_sub_agent, current_user, sub_agent_id)


@router.post("/{sub_agent_id}/pause")
async def pause(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    return await _act(sub_core.pause_sub_agent, current_user, sub_agent_id)


@router.post("/{sub_agent_id}/resume")
async def resume(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    return await _act(sub_core.resume_sub_agent, current_user, sub_agent_id)


class SubAgentPatch(BaseModel):
    """Only the fields sent are changed. `schedule: null` removes the schedule. Types are checked in
    ai/agents/sub_agents/core.py, so the route and the chat tool refuse the same things."""
    model_config = ConfigDict(extra="forbid")
    name: Optional[object] = None
    purpose: Optional[object] = None
    instructions: Optional[object] = None
    tools: Optional[object] = None
    clients: Optional[object] = None
    schedule: Optional[object] = None


@router.patch("/{sub_agent_id}")
async def patch(sub_agent_id: str, body: SubAgentPatch, current_user: str = Depends(get_current_user)):
    return await _act(sub_core.update_sub_agent, current_user, sub_agent_id, body.model_dump(exclude_unset=True))


@router.delete("/{sub_agent_id}")
async def delete(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    """Deletes the sub-agent with its lessons, schedule and run records. The UI confirms first."""
    return await _act(sub_core.delete_sub_agent, current_user, sub_agent_id)


@router.post("/{sub_agent_id}/run")
async def run_now(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    """Run an active sub-agent now: the same run as its schedule (ai/team/scheduled.run_one). Returns
    the finished run. 409 wrong_status / managed_elsewhere / already_running, 402 out_of_credits."""
    from ai.agents.sub_agents import schedule
    from ai.team import scheduled
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await schedule.start_manual_run(db, mongo_client, current_user, sub_agent_id,
                                                   run_one=scheduled.run_one,
                                                   budget_seconds=scheduled.RUN_BUDGET_SECONDS)
        except sub_core.SubAgentError as e:
            return _err(e)


@router.get("/{sub_agent_id}/runs")
async def list_runs(sub_agent_id: str, limit: int = 20, current_user: str = Depends(get_current_user)):
    """{"runs": [{id, status, trigger, started_at, finished_at, summary, answer, credits, proposals}]},
    newest first."""
    from ai.agents.sub_agents import schedule
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return {"runs": await schedule.list_runs(db, current_user, sub_agent_id, limit)}
        except sub_core.SubAgentError as e:
            return _err(e)


@router.get("/{sub_agent_id}/lessons")
async def list_lessons(sub_agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await sub_core.list_sub_agent_lessons(db, current_user, sub_agent_id)
        except sub_core.SubAgentError as e:
            return _err(e)


@router.delete("/{sub_agent_id}/lessons/{lesson_id}")
async def delete_lesson(sub_agent_id: str, lesson_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            return await sub_core.delete_sub_agent_lesson(db, current_user, sub_agent_id, lesson_id)
        except sub_core.SubAgentError as e:
            return _err(e)
