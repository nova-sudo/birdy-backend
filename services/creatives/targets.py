"""Target prefill (client -> agency) and per-account target resolution."""

from __future__ import annotations

import copy
from collections import Counter

from services.client_targets import defaults_for


async def prefill_targets(db, user_id: str, client_group_ids: list[str]) -> dict:
    """The test-level CPL is in the user's currency; `per_account` stays in each
    group's ad-account currency (that is how client targets are entered)."""
    from services.creatives.verdict import fx_rate, user_currency

    groups = await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": list(client_group_ids)}},
        {"id": 1, "targets": 1, "ad_account_currency": 1, "_id": 0},
    ).to_list(length=None)
    by_id = {g["id"]: g for g in groups}

    per_account: dict[str, dict] = {}
    for gid in client_group_ids:  # keep caller order
        cpl = ((by_id.get(gid) or {}).get("targets") or {}).get("cpl")
        if cpl is not None:
            per_account[gid] = {"cpl": cpl}

    value = None
    if per_account:
        # Only look the user's currency up when a group has a currency to convert from.
        has_cur = any((by_id.get(gid) or {}).get("ad_account_currency") for gid in per_account)
        user_cur = await user_currency(user_id) if has_cur else None
        converted = []
        for gid, v in per_account.items():
            rate = fx_rate((by_id.get(gid) or {}).get("ad_account_currency"), user_cur)
            try:
                if rate is not None:
                    converted.append(round(float(v["cpl"]) * rate, 2))
            except (TypeError, ValueError):
                continue
        if converted:
            value = Counter(converted).most_common(1)[0][0]
    if value is not None:
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
    metrics = out.setdefault("metrics", {})
    group_applies = "cpl" in metrics or out.get("mode") == "cpl_only"
    cpl = override if override is not None else (group_cpl if group_applies else None)
    if cpl is not None:
        metrics["cpl"] = {**metrics.get("cpl", {"op": "lte"}), "value": cpl}
    return out
