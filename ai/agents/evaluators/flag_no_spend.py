"""
ai/agents/evaluators/flag_no_spend.py
-------------------------------------
Flag a live ad set (in a live campaign, with a budget) that spent nothing over
the quiet window although it spent in the 14 days before. Flag only: no action.
Pure: no I/O, no clock.
"""

import math
from datetime import date, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import SEVERITY_MEDIUM, Evidence, Finding, Stat

KIND = "flag_no_spend"
LOOKBACK_DAYS = 14


def _merge_params(rule: dict, client: ClientCtx) -> dict:
    overrides = (rule.get("client_overrides") or {}).get(client.client_group_id, {})
    return {**DEFAULT_PARAMS[KIND], **(rule.get("params") or {}), **overrides}


def evaluate(rule: dict, client: ClientCtx, ad_days: dict[str, list[AdDay]],
             ads: dict[str, AdState], adsets: dict[str, AdsetState], as_of: date,
             *, near_miss: bool = False) -> list[Finding]:
    p = _merge_params(rule, client)
    hours = p["hours"]
    n = max(1, math.ceil(hours / 24))
    quiet_start = as_of - timedelta(days=n - 1)
    prior_start = quiet_start - timedelta(days=LOOKBACK_DAYS)
    sym = client.currency_symbol

    quiet: dict[str, float] = {}
    prior: dict[str, float] = {}
    for rows in ad_days.values():
        for r in rows:
            if r.day > as_of or not r.adset_id:
                continue
            if r.day >= quiet_start:
                quiet[r.adset_id] = quiet.get(r.adset_id, 0.0) + r.spend
            elif r.day >= prior_start:
                prior[r.adset_id] = prior.get(r.adset_id, 0.0) + r.spend

    out: list[Finding] = []
    for adset_id, adset in adsets.items():
        if (adset.effective_status or "").upper() != "ACTIVE" or (adset.campaign_status or "").upper() != "ACTIVE":
            continue
        if not (adset.daily_budget > 0 or adset.lifetime_budget > 0):
            continue
        if prior.get(adset_id, 0.0) <= 0 or quiet.get(adset_id, 0.0) > 0:
            continue
        name = adset.name or adset_id
        out.append(Finding(
            agent=f"media_buying:{rule['kind']}",
            client_group_id=client.client_group_id,
            client_name=client.name,
            severity=SEVERITY_MEDIUM,
            title=f"“{name}” spent {sym}0 for {hours} hrs",
            description=f"Live with a budget but no spend in the last {hours} hours",
            evidence=Evidence(
                window=f"last_{hours}h",
                stats=[
                    Stat("Spent", f"{sym}0", bad=True),
                    Stat("Prior 14d spend", f"{sym}{prior[adset_id]:.0f}"),
                ],
                raw={
                    "adset_id": adset_id,
                    "hours": hours,
                    "prior_spend": round(prior[adset_id], 2),
                    "daily_budget": adset.daily_budget,
                    "lifetime_budget": adset.lifetime_budget,
                    "params": {k: p[k] for k in DEFAULT_PARAMS[KIND]},
                },
            ),
            action=None,
            confidence=0.8,
            icon="alert",
            agent_id=rule.get("agent_id") or "",
            rule_id=str(rule.get("_id") or ""),
            rule_version=int(rule.get("version") or 0),
            near_miss=False,
        ))
    return out
