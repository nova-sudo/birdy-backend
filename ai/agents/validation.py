"""
ai/agents/validation.py
-----------------------
Shared checks for rule params and per-client overrides. The PATCH route and the
training chat tools both call these, so the two paths can never disagree. Each
returns an error message (or None); callers decide how to surface it (HTTP 400
or a tool's {ok: false}).
"""

from typing import Optional

from ai.agents.defaults import DEFAULT_PARAMS


def params_error(kind: str, params) -> Optional[str]:
    """Why `params` is not a valid partial param set for rule `kind`, or None."""
    allowed = DEFAULT_PARAMS[kind]
    if not isinstance(params, dict):
        return "params must be an object"
    unknown = set(params) - set(allowed)
    if unknown:
        return f"Unknown param(s) for {kind}: {sorted(unknown)}"
    for k, v in params.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return f"Param '{k}' must be a number"
        if v < 0:
            return f"Param '{k}' must not be negative"
    return None


async def owned_groups_error(db, user_id: str, group_ids) -> Optional[str]:
    """Why some of `group_ids` are not the user's client groups, or None."""
    ids = list(group_ids)
    if not ids:
        return None
    owned = await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": ids}}, {"id": 1, "_id": 0}
    ).to_list(length=None)
    missing = set(ids) - {g["id"] for g in owned}
    if missing:
        return f"Unknown client group(s): {sorted(missing)}"
    return None


async def overrides_error(db, user_id: str, kind: str, overrides) -> Optional[str]:
    """Why `overrides` ({group_id: params}) is invalid for rule `kind`, or None."""
    if not isinstance(overrides, dict):
        return "client_overrides must be an object"
    err = await owned_groups_error(db, user_id, overrides.keys())
    if err:
        return err
    for p in overrides.values():
        err = params_error(kind, p)
        if err:
            return err
    return None
