"""
routers/agent_stock.py
----------------------
The Birdy Media Buying Agent's settings API. Owner-scoped like routers/agents.py.
"""

import copy
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ai.agents import claims
from ai.agents import store as agent_store
from ai.agents.stock import settings as stock_settings
from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from routers import agents as agents_router
from services import client_targets

router = APIRouter(prefix="/api/agents", tags=["agent-stock"])


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
