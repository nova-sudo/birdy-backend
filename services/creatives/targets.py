"""Target prefill (client -> agency) and per-account target resolution."""

from __future__ import annotations

import copy
from collections import Counter

from services.client_targets import defaults_for


async def prefill_targets(db, user_id: str, client_group_ids: list[str]) -> dict:
    groups = await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": list(client_group_ids)}},
        {"id": 1, "targets": 1, "_id": 0},
    ).to_list(length=None)
    by_id = {g["id"]: g for g in groups}

    per_account: dict[str, dict] = {}
    for gid in client_group_ids:  # keep caller order
        cpl = ((by_id.get(gid) or {}).get("targets") or {}).get("cpl")
        if cpl is not None:
            per_account[gid] = {"cpl": cpl}

    if per_account:
        value = Counter(v["cpl"] for v in per_account.values()).most_common(1)[0][0]
        source = "client"
    else:
        value = (await defaults_for(user_id, db)).get("cpl")
        source = "agency" if value is not None else None
    return {"metrics": {"cpl": {"op": "lte", "value": value, "source": source}},
            "per_account": per_account}


def owning_group(groups: list[dict], campaign_id: str | None) -> dict:
    if campaign_id is not None:
        for g in groups:
            if campaign_id in (g.get("meta_campaign_ids") or []):
                return g
    for g in groups:
        if g.get("meta_campaign_ids") is None:
            return g
    return groups[0]


def resolve_account_targets(test_targets: dict, ad: dict, group_cpl: float | None) -> dict:
    """ad.targets_override > group CPL (cpl only) > test targets. Returns a copy."""
    out = copy.deepcopy(test_targets)
    override = (ad.get("targets_override") or {}).get("cpl")
    cpl = override if override is not None else group_cpl
    if cpl is not None:
        metrics = out.setdefault("metrics", {})
        metrics["cpl"] = {**metrics.get("cpl", {"op": "lte"}), "value": cpl}
    return out
