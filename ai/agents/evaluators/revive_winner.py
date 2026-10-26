"""
ai/agents/evaluators/revive_winner.py
-------------------------------------
Turn back on paused ads that were winners (lifetime CPL at or under target with
enough leads), have been off a while, are still young creative, and whose ad
set and campaign are live. Pure: no I/O, no clock.
"""

from datetime import date

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import (
    ACTION_ENABLE_ADS,
    SEVERITY_OPPORTUNITY,
    Action,
    Evidence,
    Finding,
    Stat,
)

KIND = "revive_winner"
CPL_NEAR_RATIO = 1.15
DAYS_MARGIN = 3
LEADS_MARGIN = 1


def _merge_params(rule: dict, client: ClientCtx) -> dict:
    overrides = (rule.get("client_overrides") or {}).get(client.client_group_id, {})
    return {**DEFAULT_PARAMS[KIND], **(rule.get("params") or {}), **overrides}


def evaluate(rule: dict, client: ClientCtx, ad_days: dict[str, list[AdDay]],
             ads: dict[str, AdState], adsets: dict[str, AdsetState], as_of: date,
             *, near_miss: bool = False) -> list[Finding]:
    target = client.target_cpl
    if not target:
        return []
    p = _merge_params(rule, client)
    sym = client.currency_symbol
    out: list[Finding] = []

    for ad_id, ad in ads.items():
        if (ad.effective_status or "").upper() != "PAUSED" or ad.paused_at is None or ad.created_time is None:
            continue
        adset = adsets.get(ad.adset_id)
        if adset is None:
            continue
        if (adset.effective_status or "").upper() != "ACTIVE" or (adset.campaign_status or "").upper() != "ACTIVE":
            continue

        rows = [r for r in ad_days.get(ad_id, []) if r.day <= as_of]
        spend = sum(r.spend for r in rows)
        leads = sum(r.leads for r in rows)
        cpl = spend / leads if leads > 0 else None
        paused_days = (as_of - ad.paused_at).days
        age = (as_of - ad.created_time.date()).days

        # Each check is 'ok', 'near' (short by its near-miss margin) or 'fail'.
        if leads >= p["min_leads"]:
            leads_c = "ok"
        elif leads > 0 and leads >= p["min_leads"] - LEADS_MARGIN:
            leads_c = "near"
        else:
            leads_c = "fail"

        if cpl is None:
            cpl_c = "fail"
        elif cpl <= target:
            cpl_c = "ok"
        elif cpl <= CPL_NEAR_RATIO * target:
            cpl_c = "near"
        else:
            cpl_c = "fail"

        if paused_days >= p["min_paused_days"]:
            paused_c = "ok"
        elif paused_days >= p["min_paused_days"] - DAYS_MARGIN:
            paused_c = "near"
        else:
            paused_c = "fail"

        if age < p["max_creative_age_days"]:
            age_c = "ok"
        elif age <= p["max_creative_age_days"] + DAYS_MARGIN:
            age_c = "near"
        else:
            age_c = "fail"

        checks = [leads_c, cpl_c, paused_c, age_c]
        if "fail" in checks:
            continue
        is_near = checks.count("near") == 1
        if checks.count("near") > 1:
            continue
        if is_near and not near_miss:
            continue

        name = ad.name or ad_id
        evidence = Evidence(
            window="lifetime",
            stats=[
                Stat("CPL", f"{sym}{cpl:.2f}"),
                Stat("Target", f"{sym}{target:.2f}"),
                Stat("Leads", str(leads)),
                Stat("Paused", f"{paused_days} days"),
                Stat("Creative age", f"{age} days"),
            ],
            raw={
                "ad_ids": [ad_id],
                "spend": round(spend, 2),
                "leads": leads,
                "cpl": round(cpl, 2),
                "target_cpl": target,
                "paused_days": paused_days,
                "creative_age_days": age,
                "params": {k: p[k] for k in DEFAULT_PARAMS[KIND]},
            },
        )
        out.append(Finding(
            agent=f"media_buying:{rule['kind']}",
            client_group_id=client.client_group_id,
            client_name=client.name,
            severity=SEVERITY_OPPORTUNITY,
            title=f"Turn back on “{name}”",
            description=(f"Paused {paused_days} days ago at {sym}{cpl:.0f} CPL vs "
                         f"{sym}{target:.0f} target · creative {age} days old"),
            evidence=evidence,
            action=None if is_near else Action(
                type=ACTION_ENABLE_ADS,
                targets=[{"object_id": ad_id, "object_type": "ad", "name": name}],
                params={},
            ),
            confidence=0.7,
            icon="play",
            agent_id=rule.get("agent_id") or "",
            rule_id=str(rule.get("_id") or ""),
            rule_version=int(rule.get("version") or 0),
            near_miss=is_near,
        ))
    return out
