"""
routers/agents.py
-----------------
BirdyHub agents API (Phase 1: the Media Buying Agent).

Every route is owner-scoped: another user's agent is a 404, never a 403.
`status` and `training` are not editable here (activate / practice own them).
Budgets are never writable: `permissions.budgets` is forced to false.
"""

import asyncio
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ai.agents import replay
from ai.agents import store as agent_store
from ai.agents.defaults import (
    DEFAULT_GUARDRAILS,
    DEFAULT_PARAMS,
    DEFAULT_PERMISSIONS,
)
from ai.suggestions import store as sug_store
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from routers.dashboard import _suggestion_to_api
from services import client_targets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

SETTABLE_MODES = ("flag", "ask")  # "auto" is granted only through graduation


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _agent_to_api(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "type": doc.get("type"),
        "status": doc.get("status"),
        "scope": doc.get("scope") or {},
        "permissions": doc.get("permissions") or {},
        "guardrails": doc.get("guardrails") or {},
        "client_caps": doc.get("client_caps") or {},
        "training": doc.get("training") or {},
        "confidence": doc.get("confidence") or {},
        "last_run": doc.get("last_run"),
        "last_run_at": doc.get("last_run_at"),
        "created_at": doc.get("created_at"),
    }


def _rule_to_api(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


def _memory_to_api(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["id"] = doc["_id"]
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _agent_or_404(db, user_id: str, agent_id: str) -> dict:
    agent = await agent_store.get_agent(db, user_id, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _require_owned_groups(db, user_id: str, group_ids) -> None:
    ids = list(group_ids)
    if not ids:
        return
    owned = await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": ids}}, {"id": 1, "_id": 0}
    ).to_list(length=None)
    missing = set(ids) - {g["id"] for g in owned}
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown client group(s): {sorted(missing)}")


def _validate_params(kind: str, params: dict) -> None:
    allowed = DEFAULT_PARAMS[kind]
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="params must be an object")
    unknown = set(params) - set(allowed)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown param(s) for {kind}: {sorted(unknown)}")
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(status_code=400, detail=f"Param '{k}' must be a number")
        if v < 0:
            raise HTTPException(status_code=400, detail=f"Param '{k}' must not be negative")


_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
SCOPE_KEYS = {"client_group_ids", "schedule"}


def _validate_schedule(sched) -> dict:
    if not isinstance(sched, dict):
        raise HTTPException(status_code=400, detail="schedule must be an object")
    unknown = set(sched) - {"time", "tz"}
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown schedule key(s): {sorted(unknown)}")
    if "time" in sched and not (isinstance(sched["time"], str) and _TIME_RE.match(sched["time"])):
        raise HTTPException(status_code=400, detail="schedule.time must be HH:MM (24h)")
    if "tz" in sched:
        try:
            ZoneInfo(sched["tz"])
        except Exception:
            raise HTTPException(status_code=400, detail="schedule.tz is not a valid timezone")
    return sched


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

@router.get("")
async def list_agents(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        return [_agent_to_api(a) for a in await agent_store.list_agents(db, current_user)]


@router.post("")
async def create_agent(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        try:
            agent = await agent_store.create_agent(db, current_user)
        except agent_store.AgentExists:
            raise HTTPException(status_code=409, detail="You already have a media buying agent")
        return _agent_to_api(agent)


@router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        return _agent_to_api(await _agent_or_404(db, current_user, agent_id))


class AgentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Optional[dict] = None
    permissions: Optional[dict] = None
    guardrails: Optional[dict] = None
    client_caps: Optional[dict] = None


@router.patch("/{agent_id}")
async def patch_agent(agent_id: str, body: AgentPatch, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _agent_or_404(db, current_user, agent_id)
        patch: dict = {}

        if body.scope is not None:
            unknown = set(body.scope) - SCOPE_KEYS
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown scope key(s): {sorted(unknown)}")
            ids = body.scope.get("client_group_ids")
            if ids is not None:
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    raise HTTPException(status_code=400, detail="client_group_ids must be a list of ids")
                await _require_owned_groups(db, current_user, ids)
            current = agent.get("scope") or {}
            merged_scope = {**current, **{k: v for k, v in body.scope.items() if k != "schedule"}}
            if "schedule" in body.scope:
                merged_scope["schedule"] = {**(current.get("schedule") or {}),
                                            **_validate_schedule(body.scope["schedule"])}
            patch["scope"] = merged_scope

        if body.permissions is not None:
            unknown = set(body.permissions) - set(DEFAULT_PERMISSIONS)
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown permission(s): {sorted(unknown)}")
            merged = {**(agent.get("permissions") or {}), **body.permissions}
            merged["budgets"] = False  # Phase 1: no budget writes, ever
            patch["permissions"] = merged

        if body.guardrails is not None:
            unknown = set(body.guardrails) - set(DEFAULT_GUARDRAILS)
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown guardrail(s): {sorted(unknown)}")
            patch["guardrails"] = {**(agent.get("guardrails") or {}), **body.guardrails}

        if body.client_caps is not None:
            await _require_owned_groups(db, current_user, body.client_caps.keys())
            for v in body.client_caps.values():
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise HTTPException(status_code=400, detail="client_caps values must be numbers >= 0")
            patch["client_caps"] = body.client_caps

        updated = await agent_store.update_agent(db, current_user, agent_id, patch)
        return _agent_to_api(updated)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/rules")
async def list_rules(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        return [_rule_to_api(r) for r in await agent_store.list_rules(db, agent_id)]


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: Optional[dict] = None
    client_overrides: Optional[dict] = None
    mode: Optional[str] = None
    enabled: Optional[bool] = None


@router.patch("/{agent_id}/rules/{rule_id}")
async def patch_rule(agent_id: str, rule_id: str, body: RulePatch,
                     current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        rule = await db[agent_store.RULES].find_one({"_id": rule_id, "agent_id": agent_id})
        if rule is None:
            raise HTTPException(status_code=404, detail="Rule not found")
        kind = rule["kind"]

        if body.mode is not None and body.mode not in SETTABLE_MODES:
            detail = ("Auto mode is only granted through graduation" if body.mode == "auto"
                      else "mode must be 'flag' or 'ask'")
            raise HTTPException(status_code=400, detail=detail)

        params = None
        if body.params is not None:
            _validate_params(kind, body.params)
            params = {**(rule.get("params") or {}), **body.params}

        overrides = None
        if body.client_overrides is not None:
            if not isinstance(body.client_overrides, dict):
                raise HTTPException(status_code=400, detail="client_overrides must be an object")
            await _require_owned_groups(db, current_user, body.client_overrides.keys())
            for p in body.client_overrides.values():
                _validate_params(kind, p)
            overrides = body.client_overrides

        updated = await agent_store.update_rule(
            db, agent_id, rule_id,
            params=params, client_overrides=overrides, mode=body.mode, enabled=body.enabled)
        return _rule_to_api(updated)


# ---------------------------------------------------------------------------
# Client targets
# ---------------------------------------------------------------------------

class ClientTargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cpl: Optional[float] = Field(default=None, ge=0)


@router.put("/{agent_id}/client-targets/{gid}")
async def put_client_target(agent_id: str, gid: str, body: ClientTargetBody,
                            current_user: str = Depends(get_current_user)):
    """Set (or clear, with null) one client's CPL target. Field-level $set, the
    same merge as PUT /api/client-groups/{id}/targets, so other targets survive."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        agent = await _agent_or_404(db, current_user, agent_id)
        if gid not in ((agent.get("scope") or {}).get("client_group_ids") or []):
            raise HTTPException(status_code=404, detail="Client not in this agent's scope")
        targets = await client_targets.apply_targets(
            db, current_user, gid,
            {} if body.cpl is None else {"cpl": body.cpl},
            clear=("cpl",) if body.cpl is None else ())
        if targets is None:
            raise HTTPException(status_code=404, detail="Client group not found")
        return {"client_group_id": gid, "targets": client_targets.clean(targets)}


# ---------------------------------------------------------------------------
# Work list
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/work")
async def get_work(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        coll = db[sug_store.SUGGESTIONS]
        open_docs = await coll.find(
            {"user_id": current_user, "agent_id": agent_id, "status": sug_store.STATUS_OPEN}
        ).sort("created_at", -1).to_list(length=None)
        since = datetime.utcnow() - timedelta(hours=24)
        auto_docs = await coll.find({
            "user_id": current_user, "agent_id": agent_id, "status": sug_store.STATUS_APPLIED,
            "decided_by.actor": sug_store.ACTOR_BIRDY, "applied_at": {"$gte": since},
        }).sort("applied_at", -1).to_list(length=None)
        return {"open": [_suggestion_to_api(d) for d in open_docs],
                "auto_applied": [_suggestion_to_api(d) for d in auto_docs]}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@router.get("/{agent_id}/memory")
async def list_memory(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        return [_memory_to_api(m) for m in await agent_store.list_memory(db, agent_id)]


@router.delete("/{agent_id}/memory/{memory_id}")
async def delete_memory(agent_id: str, memory_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        if not await agent_store.deactivate_memory(db, agent_id, memory_id):
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"ok": True}


# ---------------------------------------------------------------------------
# Dry run (background job)
# ---------------------------------------------------------------------------

JOB_TIMEOUT = timedelta(minutes=10)
_BACKGROUND: set = set()  # strong refs so running jobs aren't garbage-collected


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def _local_today(agent: dict) -> date:
    tz = ((agent.get("scope") or {}).get("schedule") or {}).get("tz") or "UTC"
    try:
        return datetime.now(ZoneInfo(tz)).date()
    except Exception:
        return datetime.utcnow().date()


async def _run_dry_run_job(job_id: str, user_id: str, agent_id: str) -> None:
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        jobs = db[agent_store.JOBS]
        await jobs.update_one({"_id": job_id}, {"$set": {"status": "running", "started_at": datetime.utcnow()}})
        try:
            agent = await agent_store.get_agent(db, user_id, agent_id)
            if agent is None:
                raise RuntimeError("agent not found")
            result = await replay.dry_run(db, mongo_client, agent, end=_local_today(agent))
            await jobs.update_one({"_id": job_id}, {"$set": {
                "status": "done", "result": result.to_dict(), "finished_at": datetime.utcnow()}})
        except Exception as e:
            logger.error("dry run job %s failed: %s", job_id, e, exc_info=True)
            await jobs.update_one({"_id": job_id}, {"$set": {
                "status": "failed", "error": str(e) or type(e).__name__, "finished_at": datetime.utcnow()}})


@router.post("/{agent_id}/dry-run")
async def start_dry_run(agent_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        job_id = uuid.uuid4().hex
        await db[agent_store.JOBS].insert_one({
            "_id": job_id, "agent_id": agent_id, "user_id": current_user, "kind": "dry_run",
            "status": "queued", "created_at": datetime.utcnow(),
        })
    _spawn(_run_dry_run_job(job_id, current_user, agent_id))
    return {"job_id": job_id}


@router.get("/{agent_id}/jobs/{job_id}")
async def get_job(agent_id: str, job_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _agent_or_404(db, current_user, agent_id)
        jobs = db[agent_store.JOBS]
        job = await jobs.find_one({"_id": job_id, "agent_id": agent_id, "user_id": current_user})
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        created = job.get("created_at")
        if (job.get("status") in ("queued", "running") and isinstance(created, datetime)
                and datetime.utcnow() - created > JOB_TIMEOUT):
            # The deployment may have killed the background task: report and persist it.
            await jobs.update_one({"_id": job_id, "status": job["status"]},
                                  {"$set": {"status": "failed", "error": "timed_out",
                                            "finished_at": datetime.utcnow()}})
            job = {**job, "status": "failed", "error": "timed_out"}
        out = {"status": job.get("status")}
        if job.get("status") == "done":
            out["result"] = job.get("result")
        elif job.get("status") == "failed":
            out["error"] = job.get("error")
        return out
