"""
ai/suggestions/agents/useless_ad_purger.py
------------------------------------------
The first analyzer subagent: finds active ads that are wasting spend and proposes
pausing them.

Detection is 100% deterministic (no LLM) — a pause is a real-money action, so it
must never rest on a fabricated number. An ad is "underperforming" when, in the
window:

  * it spent a meaningful amount but produced ZERO leads (clear waste), or
  * its cost-per-lead exceeds the client's TARGET, where the target is:
        1. the value of an existing `cost_per_result`/`cpl` ceiling ALERT the
           agency already set for this client (operator "gt"), else
        2. a relative baseline = the median CPL of the client's other converting
           ads × BASELINE_MULTIPLIER.

All underperformers in one client+window are grouped into a single finding
("Pause N underperforming ads") carrying a pause_ads action over every offending
ad. The LLM composer later rewrites the copy from these exact numbers.
"""

import logging
import os
import statistics

from ai.tools.derived_metrics import enrich
from ai.suggestions.contracts import (
    Action,
    AnalyzerContext,
    Evidence,
    Finding,
    Stat,
    ACTION_PAUSE_ADS,
    SEVERITY_HIGH,
    SEVERITY_MEDIUM,
    WINDOW_MONTHLY,
    WINDOW_WEEKLY,
)

logger = logging.getLogger(__name__)

# window → the facebook_cache preset that holds its numbers.
_WINDOW_PRESET = {
    WINDOW_WEEKLY: "last_7d",
    WINDOW_MONTHLY: "last_30d",
}


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.getenv(env_key, default))
    except (TypeError, ValueError):
        return default


# Tuning knobs (account-currency units). Env-overridable so cadence/strictness
# can change without a redeploy.
MIN_SPEND = {
    WINDOW_WEEKLY: _f("PURGER_MIN_SPEND_WEEKLY", 30.0),
    WINDOW_MONTHLY: _f("PURGER_MIN_SPEND_MONTHLY", 100.0),
}
# Zero leads AND at least this much spend → clear waste, flagged HIGH.
ZERO_LEAD_FLOOR = {
    WINDOW_WEEKLY: _f("PURGER_ZERO_LEAD_FLOOR_WEEKLY", 50.0),
    WINDOW_MONTHLY: _f("PURGER_ZERO_LEAD_FLOOR_MONTHLY", 150.0),
}
# How far above the converting-ads median counts as "too expensive".
BASELINE_MULTIPLIER = _f("PURGER_BASELINE_MULTIPLIER", 1.75)
# CPL this far over target escalates the whole finding to HIGH.
HIGH_SEVERITY_MULTIPLIER = _f("PURGER_HIGH_MULTIPLIER", 1.5)

_CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "AED": "د.إ", "INR": "₹", "ZAR": "R", "JPY": "¥",
}


def _symbol(currency: str | None) -> str:
    if not currency:
        return "$"
    return _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency.upper()} ")


def _is_active(status) -> bool:
    return str(status or "").strip().upper() == "ACTIVE"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _resolve_target_cpl(ctx: AnalyzerContext, client_group_id: str) -> tuple[float | None, str]:
    """
    Return (target_cpl, source). source ∈ {"alert","baseline","none"}.

    Prefers an existing agency-defined ceiling alert for this client; the caller
    falls back to a relative baseline when this returns None.
    """
    try:
        cursor = ctx.db["alerts"].find({
            "user_id": ctx.user_id,
            "condition.metric": {"$in": ["cost_per_result", "cpl"]},
            "condition.operator": "gt",
            "status": {"$in": ["active", "triggered"]},
        })
        alerts = await cursor.to_list(length=100)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("purger: alert lookup failed for %s: %s", ctx.user_id, e)
        alerts = []

    # Prefer an alert explicitly targeting this client; else any account-wide one.
    scoped = [a for a in alerts if client_group_id in (a.get("target_group_ids") or [])]
    account_wide = [a for a in alerts if not (a.get("target_group_ids") or [])]
    pool = scoped or account_wide
    values = [
        _num((a.get("condition") or {}).get("value"))
        for a in pool
    ]
    values = [v for v in values if v is not None and v > 0]
    if values:
        # Most conservative (lowest ceiling) wins.
        return min(values), "alert"
    return None, "none"


class UselessAdPurger:
    """Subagent implementing the CPA-based ad-purge heuristic."""

    name = "useless_ad_purger"

    async def analyze(self, ctx: AnalyzerContext, client_group: dict, window: str) -> list[Finding]:
        preset = _WINDOW_PRESET.get(window)
        if not preset:
            return []

        cache = (client_group.get("facebook_cache") or {}).get(preset) or {}
        ads = cache.get("ads") or []
        if not ads:
            return []

        client_group_id = client_group.get("id")
        client_name = client_group.get("name") or "Client"
        symbol = _symbol(client_group.get("ad_account_currency"))
        min_spend = MIN_SPEND.get(window, 30.0)
        zero_floor = ZERO_LEAD_FLOOR.get(window, 50.0)

        # Enrich a copy of each ad so we don't mutate the cached doc.
        rows = []
        for ad in ads:
            row = enrich(dict(ad))
            row["_spend"] = _num(row.get("spend")) or 0.0
            row["_leads"] = _num(row.get("results")) or 0.0
            row["_cpl"] = _num(row.get("cost_per_result"))
            rows.append(row)

        # Baseline from ACTIVE + converting ads with real spend (fallback target).
        converting_cpls = [
            r["_cpl"] for r in rows
            if _is_active(r.get("status")) and r["_leads"] > 0 and r["_cpl"] and r["_spend"] >= min_spend
        ]
        baseline = None
        if len(converting_cpls) >= 2:
            baseline = round(statistics.median(converting_cpls) * BASELINE_MULTIPLIER, 2)

        # Target: alert ceiling first, else baseline.
        target, target_source = await _resolve_target_cpl(ctx, client_group_id)
        if target is None and baseline is not None:
            target, target_source = baseline, "baseline"

        # Evaluate each active ad → ONE finding per offending ad (so a shared ad
        # across client-groups dedupes to a single suggestion downstream).
        offenders = []
        for r in rows:
            if not _is_active(r.get("status")):
                continue
            spend = r["_spend"]
            leads = r["_leads"]
            cpl = r["_cpl"]
            if spend < min_spend:
                continue

            reason = None
            if leads == 0 and spend >= zero_floor:
                reason = "zero_leads"
            elif target is not None and cpl is not None and cpl > target:
                reason = "over_target"

            if reason:
                offenders.append({
                    "object_id": str(r.get("id")),
                    "name": r.get("name") or "Ad",
                    "spend": round(spend, 2),
                    "leads": int(leads),
                    "cpl": round(cpl, 2) if cpl is not None else None,
                    "reason": reason,
                })

        return [
            self._build_ad_finding(
                client_group_id=client_group_id,
                client_name=client_name,
                window=window,
                preset=preset,
                symbol=symbol,
                target=target,
                target_source=target_source,
                offender=off,
            )
            for off in offenders
        ]

    def _build_ad_finding(self, *, client_group_id, client_name, window, preset, symbol,
                          target, target_source, offender) -> Finding:
        """Build one suggestion for a single underperforming ad."""
        cpl = offender["cpl"]
        spend = offender["spend"]
        leads = offender["leads"]
        name = offender["name"]
        window_label = "7d" if window == WINDOW_WEEKLY else "30d"

        high = offender["reason"] == "zero_leads" or (
            cpl is not None and target is not None and cpl > target * HIGH_SEVERITY_MULTIPLIER
        )
        severity = SEVERITY_HIGH if high else SEVERITY_MEDIUM

        stats = []
        if cpl is not None:
            stats.append(Stat("CPL", f"{symbol}{cpl:.2f}", bad=True))
        if target is not None:
            target_label = "Target" if target_source == "alert" else "Acct median"
            stats.append(Stat(target_label, f"{symbol}{target:.2f}"))
        stats.append(Stat(f"Spent ({window_label})", f"{symbol}{spend:.0f}"))
        stats.append(Stat("Leads", str(leads), bad=(leads == 0)))

        # Deterministic template copy — always safe to show even with no LLM.
        if offender["reason"] == "zero_leads":
            basis = f"spent {symbol}{spend:.0f} over the last {window_label} with 0 leads"
        elif target_source == "alert" and target is not None:
            basis = f"is at {symbol}{cpl:.2f} cost-per-lead over the last {window_label}, above your {symbol}{target:.0f} target"
        elif target_source == "baseline" and target is not None:
            basis = f"is at {symbol}{cpl:.2f} cost-per-lead over the last {window_label}, well above this account's {symbol}{target:.0f} median"
        else:
            basis = f"spent {symbol}{spend:.0f} over the last {window_label} with poor return"
        title = f"Pause underperforming ad — {name}"
        description = f"'{name}' {basis}. Pausing it frees that budget for your top performers."

        action = Action(
            type=ACTION_PAUSE_ADS,
            targets=[{"object_id": offender["object_id"], "object_type": "ad", "name": name}],
            params={},
        )
        evidence = Evidence(
            window=window,
            stats=stats,
            raw={
                "preset": preset,
                "target": target,
                "target_source": target_source,
                "ad": name,
                "ad_id": offender["object_id"],
                "spend": spend,
                "leads": leads,
                "cpl": cpl,
                "reason": offender["reason"],
            },
        )
        return Finding(
            agent=self.name,
            client_group_id=client_group_id,
            client_name=client_name,
            severity=severity,
            title=title,
            description=description,
            evidence=evidence,
            action=action,
            platform="Meta Ads",
            confidence=0.9 if offender["reason"] == "zero_leads" else 0.75,
            icon="pause",
        )
