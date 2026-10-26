"""
routers/agents.py
-----------------
BirdyHub agents API (Phase 1: the Media Buying Agent).

Every route is owner-scoped: another user's agent is a 404, never a 403.
`status` and `training` are not editable here (activate / practice own them).
Budgets are never writable: `permissions.budgets` is forced to false.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

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
        if agent["permissions"].get("budgets"):
            agent = await agent_store.update_agent(
                db, current_user, agent["_id"],
                {"permissions": {**agent["permissions"], "budgets": False}})
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
            ids = body.scope.get("client_group_ids")
            if ids is not None:
                if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                    raise HTTPException(status_code=400, detail="client_group_ids must be a list of ids")
                await _require_owned_groups(db, current_user, ids)
            patch["scope"] = {**(agent.get("scope") or {}), **body.scope}

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
        rule = next((r for r in await agent_store.list_rules(db, agent_id) if r["_id"] == rule_id), None)
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
    cpl: Optional[float] = None


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
        now = datetime.utcnow()
        if body.cpl is None:
            op = {"$unset": {"targets.cpl": ""}, "$set": {"targets.updated_at": now, "updated_at": now}}
        else:
            op = {"$set": {"targets.cpl": body.cpl, "targets.updated_at": now, "updated_at": now}}
        res = await db["client_groups"].update_one({"id": gid, "user_id": current_user}, op)
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail="Client group not found")
        group = await db["client_groups"].find_one({"id": gid, "user_id": current_user}, {"targets": 1, "_id": 0})
        return {"client_group_id": gid, "targets": client_targets.clean((group or {}).get("targets"))}


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
