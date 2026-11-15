"""
ai/agents/stock/agent_settings.py
---------------------------------
Change a Birdy Media Buying Agent's settings and watched clients. PATCH /api/agents/{id}/settings
(routers/agent_stock.py) and the MCP tool update_agent_settings both go through update_settings, so
there is one set of checks. A refusal raises SettingsError carrying the JSON body to answer with.
"""

import copy

from ai.agents import claims
from ai.agents import store as agent_store
from ai.agents import validation
from ai.agents.stock import settings as stock_settings
from ai.agents.stock import templates as stock_templates


class SettingsError(Exception):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("detail"))
        self.status, self.body = status, body


async def _require_owned_groups(db, user_id: str, group_ids) -> None:
    err = await validation.owned_groups_error(db, user_id, group_ids)
    if err:
        raise SettingsError(400, {"detail": err})


async def update_settings(db, user_id: str, agent: dict, *, settings: dict | None = None,
                          client_group_ids: list[str] | None = None, move_claims: bool = False,
                          only_flag: list[str] | None = None) -> dict:
    """Merge `settings` into the agent's, and set the watched clients when `client_group_ids` is given.
    Returns the updated agent doc."""
    patch = settings or {}
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
        raise SettingsError(400, {"detail": "Some settings are invalid", "errors": errors})
    await _require_owned_groups(db, user_id, (patch.get("clients") or {}).keys())

    current = agent.get("settings") or copy.deepcopy(stock_settings.DEFAULT_SETTINGS)
    update: dict = {"settings": stock_settings.merge_settings(current, patch)}

    if client_group_ids is not None:
        ids = list(dict.fromkeys(client_group_ids))
        await _require_owned_groups(db, user_id, ids)
        scope = agent.get("scope") or {}
        before = list(scope.get("client_group_ids") or [])
        flag_only = [g for g in (only_flag if only_flag is not None
                                 else scope.get(claims.FLAG_ONLY_KEY) or []) if g in ids]
        update["scope"] = {**scope, "client_group_ids": ids, claims.FLAG_ONLY_KEY: flag_only}
        if agent.get("status") == "active":
            conflict = await claims.claim_scope(db, user_id, agent["_id"], ids, move=move_claims, only_flag=flag_only)
            if conflict is not None:
                raise SettingsError(409, conflict)
            await claims.release_clients(db, user_id, agent["_id"], set(before) - set(ids))

    return await agent_store.update_agent(db, user_id, agent["_id"], update)
