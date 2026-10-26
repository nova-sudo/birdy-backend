"""
ai/agents/evaluators/flag_declining.py
--------------------------------------
Flag a campaign whose cost per lead over the last window is well above the
window before it. Flag only: no action. Pure: no I/O, no clock.
"""

from datetime import date, timedelta

from ai.agents.defaults import DEFAULT_PARAMS
from ai.agents.snapshot import AdDay, AdState, AdsetState, ClientCtx
from ai.suggestions.contracts import SEVERITY_MEDIUM, Evidence, Finding, Stat

KIND = "flag_declining"


def _merge_params(rule: dict, client: ClientCtx) -> dict:
    overrides = (rule.get("client_overrides") or {}).get(client.client_group_id, {})
    return {**DEFAULT_PARAMS[KIND], **(rule.get("params") or {}), **overrides}


def evaluate(rule: dict, client: ClientCtx, ad_days: dict[str, list[AdDay]],
             ads: dict[str, AdState], adsets: dict[str, AdsetState], as_of: date,
             *, near_miss: bool = False) -> list[Finding]:
    p = _merge_params(rule, client)
    w = int(p["window_days"])
    now_start = as_of - timedelta(days=w - 1)
    prev_start = now_start - timedelta(days=w)
    sym = client.currency_symbol

    # campaign_id -> [spend_now, leads_now, spend_prev, leads_prev, name]
    agg: dict[str, list] = {}
    for rows in ad_days.values():
        for r in rows:
            if r.day > as_of or r.day < prev_start or not r.campaign_id:
                continue
            a = agg.setdefault(r.campaign_id, [0.0, 0, 0.0, 0, ""])
            if r.day >= now_start:
                a[0] += r.spend
                a[1] += r.leads
            else:
                a[2] += r.spend
                a[3] += r.leads
            if r.campaign_name:
                a[4] = r.campaign_name

    out: list[Finding] = []
    for campaign_id, (spend_now, leads_now, spend_prev, leads_prev, cname) in agg.items():
        # No leads in the previous window: CPL is undefined, nothing to compare.
        if leads_prev <= 0 or spend_prev <= 0:
            continue
        if leads_now + leads_prev < p["min_leads"]:
            continue
        cpl_prev = spend_prev / leads_prev
        threshold = cpl_prev * (1 + p["pct_rise"] / 100)
        no_leads_now = leads_now <= 0
        if no_leads_now:
            # Spend with zero leads is the sharpest decline: treat as one lead.
            if spend_now < threshold - 1e-9:
                continue
            cpl_now, pct = None, None
        else:
            cpl_now = spend_now / leads_now
            if cpl_now < threshold - 1e-9:
                continue
            pct = round((cpl_now / cpl_prev - 1) * 100)
        label = cname or campaign_id
        out.append(Finding(
            agent=f"media_buying:{rule['kind']}",
            client_group_id=client.client_group_id,
            client_name=client.name,
            severity=SEVERITY_MEDIUM,
            title=(f"No leads this week after {leads_prev} last week" if no_leads_now
                   else f"CPL up {pct}% week on week"),
            description=(f"{sym}{cpl_prev:.2f} CPL → {sym}{spend_now:.0f} spent, 0 leads · {label}" if no_leads_now
                         else f"{sym}{cpl_prev:.2f} → {sym}{cpl_now:.2f} · {label}"),
            evidence=Evidence(
                window=f"last_{w}d",
                stats=[
                    (Stat("Spent now", f"{sym}{spend_now:.0f}", bad=True) if no_leads_now
                     else Stat("CPL now", f"{sym}{cpl_now:.2f}", bad=True)),
                    Stat("Leads now", str(leads_now), bad=no_leads_now),
                    Stat("CPL before", f"{sym}{cpl_prev:.2f}"),
                    Stat("Leads", str(leads_now + leads_prev)),
                ],
                raw={
                    "campaign_id": campaign_id,
                    "campaign_name": cname,
                    "cpl_now": round(cpl_now, 2) if cpl_now is not None else None,
                    "cpl_prev": round(cpl_prev, 2),
                    "leads_now": leads_now,
                    "leads_prev": leads_prev,
                    "spend_now": round(spend_now, 2),
                    "spend_prev": round(spend_prev, 2),
                    "pct": pct,
                    "params": {k: p[k] for k in DEFAULT_PARAMS[KIND]},
                },
            ),
            action=None,
            confidence=0.7,
            icon="trending-up",
            agent_id=rule.get("agent_id") or "",
            rule_id=str(rule.get("_id") or ""),
            rule_version=int(rule.get("version") or 0),
            near_miss=False,
        ))
    return out
