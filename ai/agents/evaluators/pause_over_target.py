"""
ai/agents/evaluators/pause_over_target.py
-----------------------------------------
Pause active ads whose cost per lead is over the client's target, or that have
spent a lot with no leads. Pure: no I/O, no clock.
"""

from datetime import date, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import (
    ACTION_PAUSE_ADS,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    Action,
    Evidence,
    Finding,
    Stat,
)

KIND = "pause_over_target"
NEAR_MISS_RATIO = 0.85
RECENT_DAYS = 7


def _merge_params(rule: dict, client: ClientCtx) -> dict:
    overrides = (rule.get("client_overrides") or {}).get(client.client_group_id, {})
    return {**DEFAULT_PARAMS[KIND], **(rule.get("params") or {}), **overrides}


def _measure(ad: AdState, rows: list[AdDay], as_of: date, p: dict) -> dict | None:
    rows = [r for r in rows if r.day <= as_of]
    spend_rows = [r for r in rows if r.spend > 0]
    if not spend_rows:
        return None
    first = spend_rows[0].day
    days_live = (as_of - first).days + 1
    lifetime_spend = sum(r.spend for r in rows)

    start = ad.created_time.date() if ad.created_time else first
    age = (as_of - start).days + 1
    if age <= p["recent_window_days"]:
        window_kind, win, n_days = "lifetime", rows, days_live
    else:
        cutoff = as_of - timedelta(days=RECENT_DAYS - 1)
        window_kind, win, n_days = "last_7d", [r for r in rows if r.day >= cutoff], RECENT_DAYS
    spend = sum(r.spend for r in win)
    leads = sum(r.leads for r in win)
    return {
        "window_kind": window_kind,
        "spend": spend,
        "leads": leads,
        "cpl": spend / leads if leads > 0 else None,
        "n_days": n_days,
        "days_live": days_live,
        "lifetime_spend": lifetime_spend,
        "patience_met": lifetime_spend >= p["patience_spend"] or days_live >= p["patience_days"],
    }


def _classify(m: dict, target: float | None, p: dict) -> str | None:
    """'fire', 'near' or None. 'near' = within 15% under a threshold, or only patience is missing."""
    cpl, leads, spend = m["cpl"], m["leads"], m["spend"]
    cpl_limit = p["cpl_multiple"] * target if target else None
    zero_limit = p["zero_lead_spend"]

    over_cpl = cpl_limit is not None and cpl is not None and cpl > cpl_limit
    zero_hit = leads == 0 and spend >= zero_limit
    near_cpl = (cpl_limit is not None and cpl is not None and not over_cpl
                and cpl >= NEAR_MISS_RATIO * cpl_limit)
    near_zero = leads == 0 and not zero_hit and spend >= NEAR_MISS_RATIO * zero_limit

    if not m["patience_met"]:
        # Blocked only by patience: it already trips (or nears) a threshold, or has
        # spent with no leads yet.
        if over_cpl or zero_hit or near_cpl or near_zero or (leads == 0 and spend > 0):
            return "near"
        return None
    if over_cpl or zero_hit:
        return "fire"
    if near_cpl or near_zero:
        return "near"
    return None


def _copy(sym: str, name: str, spend: float, leads: int, n_days: int, target: float | None):
    cpl = spend / leads if leads else None
    if cpl is not None and target:
        desc = f"{sym}{cpl:.0f} CPL vs {sym}{target:.0f} target · {leads} leads in {n_days} days · {sym}{spend:.0f} spent"
    elif cpl is not None:
        desc = f"{sym}{cpl:.0f} CPL · {leads} leads in {n_days} days · {sym}{spend:.0f} spent"
    else:
        desc = f"0 leads in {n_days} days · {sym}{spend:.0f} spent"
        if target:
            desc += f" · {sym}{target:.0f} target"
    return f"Pause “{name}”", desc


def _finding(rule, client, p, *, name, targets, spend, leads, n_days, days_live,
             window_kind, lifetime_spend, near: bool, member_ids: list[str]) -> Finding:
    sym, target = client.currency_symbol, client.target_cpl
    cpl = spend / leads if leads else None
    title, desc = _copy(sym, name, spend, leads, n_days, target)
    stats = []
    if cpl is not None:
        bad = bool(target) and cpl > p["cpl_multiple"] * target
        stats.append(Stat("CPL", f"{sym}{cpl:.2f}", bad=bad))
    if target:
        stats.append(Stat("Target", f"{sym}{target:.2f}"))
    stats.append(Stat("Leads", str(leads), bad=(leads == 0)))
    stats.append(Stat("Spent", f"{sym}{spend:.0f}"))
    stats.append(Stat("Days live", str(days_live)))
    evidence = Evidence(
        window=window_kind,
        stats=stats,
        raw={
            "window_kind": window_kind,
            "ad_ids": member_ids,
            "spend": round(spend, 2),
            "leads": leads,
            "cpl": round(cpl, 2) if cpl is not None else None,
            "target_cpl": target,
            "days_live": days_live,
            "window_days": n_days,
            "lifetime_spend": round(lifetime_spend, 2),
            "params": {k: p[k] for k in DEFAULT_PARAMS[KIND]},
        },
    )
    return Finding(
        agent=f"media_buying:{rule['kind']}",
        client_group_id=client.client_group_id,
        client_name=client.name,
        severity=SEVERITY_HIGH if leads == 0 else SEVERITY_MEDIUM,
        title=title,
        description=desc,
        evidence=evidence,
        action=None if near else Action(type=ACTION_PAUSE_ADS, targets=targets, params={}),
        confidence=0.9 if leads == 0 else 0.75,
        icon="pause",
        agent_id=rule.get("agent_id") or "",
        rule_id=str(rule.get("_id") or ""),
        rule_version=int(rule.get("version") or 0),
        near_miss=near,
    )


def evaluate(rule: dict, client: ClientCtx, ad_days: dict[str, list[AdDay]],
             ads: dict[str, AdState], adsets: dict[str, AdsetState], as_of: date,
             *, near_miss: bool = False) -> list[Finding]:
    p = _merge_params(rule, client)
    fired: dict[str, dict] = {}
    near: dict[str, dict] = {}
    for ad_id, ad in ads.items():
        if (ad.effective_status or "").upper() != "ACTIVE":
            continue
        m = _measure(ad, ad_days.get(ad_id, []), as_of, p)
        if m is None:
            continue
        verdict = _classify(m, client.target_cpl, p)
        if verdict == "fire":
            fired[ad_id] = m
        elif verdict == "near" and near_miss:
            near[ad_id] = m

    out: list[Finding] = []
    done: set[str] = set()
    by_adset: dict[str, list[str]] = {}
    for ad_id, ad in ads.items():
        if (ad.effective_status or "").upper() == "ACTIVE":
            by_adset.setdefault(ad.adset_id, []).append(ad_id)

    for adset_id, ids in by_adset.items():
        if adset_id and len(ids) >= 2 and all(i in fired for i in ids):
            ms = [fired[i] for i in ids]
            adset = adsets.get(adset_id)
            name = adset.name if adset and adset.name else adset_id
            out.append(_finding(
                rule, client, p, name=name,
                targets=[{"object_id": adset_id, "object_type": "adset", "name": name}],
                spend=sum(m["spend"] for m in ms), leads=sum(m["leads"] for m in ms),
                n_days=max(m["n_days"] for m in ms), days_live=max(m["days_live"] for m in ms),
                window_kind=ms[0]["window_kind"] if len({m["window_kind"] for m in ms}) == 1 else "mixed",
                lifetime_spend=sum(m["lifetime_spend"] for m in ms), near=False, member_ids=list(ids),
            ))
            done.update(ids)

    for pool, is_near in ((fired, False), (near, True)):
        for ad_id, m in pool.items():
            if ad_id in done:
                continue
            name = ads[ad_id].name or ad_id
            out.append(_finding(
                rule, client, p, name=name,
                targets=[{"object_id": ad_id, "object_type": "ad", "name": name}],
                spend=m["spend"], leads=m["leads"], n_days=m["n_days"], days_live=m["days_live"],
                window_kind=m["window_kind"], lifetime_spend=m["lifetime_spend"],
                near=is_near, member_ids=[ad_id],
            ))
    return out
