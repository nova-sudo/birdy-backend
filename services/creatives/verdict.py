"""Pure verdict logic for creative tests: aggregate, derive, score, verdict, rollup."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

LEAD_TYPES = ("lead", "onsite_conversion.lead_grouped", "offsite_conversion.fb_pixel_lead")
VIDEO_VIEW = "video_view"


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _opt(d: dict, key: str, default):
    """Value of key, with an explicit None treated as the default (0 stays valid)."""
    v = d.get(key)
    return default if v is None else v


def _action(actions: list[dict] | None, types: tuple[str, ...]) -> list[float]:
    return [_num(a.get("value")) for a in (actions or []) if a.get("action_type") in types]


def aggregate_days(days: list[dict]) -> dict:
    out = {"spend": 0.0, "impressions": 0, "clicks": 0, "link_clicks": 0,
           "leads": 0, "video_views_3s": 0}
    live: set = set()
    for d in days:
        spend = _num(d.get("spend"))
        out["spend"] += spend
        out["impressions"] += int(_num(d.get("impressions")))
        out["clicks"] += int(_num(d.get("clicks")))
        out["link_clicks"] += int(_num(d.get("link_clicks")))
        # Meta double-reports leads under several action types: take the max, not the sum.
        out["leads"] += int(max(_action(d.get("actions"), LEAD_TYPES), default=0))
        out["video_views_3s"] += int(sum(_action(d.get("actions"), (VIDEO_VIEW,))))
        if spend > 0:
            live.add(d.get("date"))
    out["spend"] = round(out["spend"], 4)
    out["days_live"] = len(live)
    return out


def derive(totals: dict) -> dict:
    spend = _num(totals.get("spend"))
    leads = _num(totals.get("leads"))
    imps = _num(totals.get("impressions"))
    link_clicks = _num(totals.get("link_clicks"))
    return {
        **totals,
        "cpl": spend / leads if leads > 0 else None,
        "ctr": link_clicks / imps * 100 if imps > 0 else None,
        "cpc": spend / link_clicks if link_clicks > 0 else None,
        "hook_rate": _num(totals.get("video_views_3s")) / imps * 100 if imps > 0 else None,
    }


def _check(metric: str, op: str, target: float, value: float | None) -> dict:
    if value is None:
        ok = False
    elif op == "lte":
        ok = value <= target
    else:
        ok = value >= target
    return {"metric": metric, "op": op, "target": target, "value": value, "ok": ok}


def score_account(totals: dict, targets: dict, *, fmt: str, baseline: dict | None = None) -> dict:
    d = derive(totals)
    min_spend = _opt(targets, "min_spend", 150)
    min_days = _opt(targets, "min_days", 7)
    if _num(d.get("spend")) < min_spend and _num(d.get("days_live")) < min_days:
        return {**d, "state": "scoring", "checks": []}

    mode = targets.get("mode", "thresholds")
    metrics = targets.get("metrics") or {}
    checks: list[dict] = []

    if mode == "baseline":
        if not baseline or baseline.get("cpl") is None:
            return {**d, "state": "scoring", "checks": []}
        pct = _opt(targets, "baseline_pct", 15) / 100
        checks.append(_check("cpl", "lte", baseline["cpl"] * (1 - pct), d["cpl"]))
        if metrics.get("ctr") and baseline.get("ctr") is not None:
            checks.append(_check("ctr", "gte", baseline["ctr"] * (1 + pct), d["ctr"]))
    elif mode == "cpl_only":
        cpl = metrics.get("cpl") or {}
        if cpl.get("value") is None:
            return {**d, "state": "scoring", "checks": []}
        checks.append(_check("cpl", "lte", cpl["value"], d["cpl"]))
    else:
        for name, spec in metrics.items():
            if not spec or spec.get("value") is None:
                continue
            if name == "hook_rate" and fmt == "image":
                continue
            default_op = "lte" if name in ("cpl", "cpc") else "gte"
            value = d["leads"] if name == "leads" else d.get(name)
            checks.append(_check(name, spec.get("op", default_op), spec["value"], value))
        if not checks:
            return {**d, "state": "scoring", "checks": []}

    state = "pass" if all(c["ok"] for c in checks) else "fail"
    return {**d, "state": state, "checks": checks}


def test_verdict(scores: list[dict], win_share: float) -> str:
    win_share = 0.6 if win_share is None else win_share
    scored = [s for s in scores if s.get("state") in ("pass", "fail")]
    if len(scored) < 2:
        return "testing"
    passes = sum(1 for s in scored if s["state"] == "pass")
    fails = len(scored) - passes
    if passes / len(scored) >= win_share:
        return "winner"
    if fails / len(scored) >= win_share:
        return "loser"
    if len(scored) == len(scores):
        return "inconclusive"
    return "testing"


test_verdict.__test__ = False  # not a pytest test when imported into test modules


async def user_currency(user_id: str) -> str:
    """The user's currency (targets and test totals are in it); GBP when unknown."""
    from utils.currency_exchange import CurrencyService

    try:
        return await CurrencyService.get_user_currency(user_id) or "GBP"
    except (ValueError, RuntimeError) as e:
        logger.warning(f"creatives: user currency lookup failed for {user_id}: {e}")
        return "GBP"


def fx_rate(from_cur: str | None, to_cur: str) -> float | None:
    """from_cur -> to_cur rate (1.0 when equal or unknown source); None when no rate exists."""
    from utils.currency_exchange import CurrencyService

    if not from_cur or from_cur == to_cur:
        return 1.0
    try:
        return float(CurrencyService.get_rate(from_cur, to_cur))
    except Exception as e:
        logger.warning(f"creatives: no rate {from_cur}->{to_cur}: {e}")
        return None


async def rollup(db, user_id: str, ads: list[dict]) -> dict:
    """Test summary across ads, money converted to the user's currency."""
    from utils.currency_exchange import CurrencyService

    user_cur = await user_currency(user_id)
    spend = rated_leads = 0.0
    leads = imps = link_clicks = views = video_imps = 0.0
    rate_missing = mixed = False
    accounts: dict[str, str] = {}

    for ad in ads:
        sc = ad.get("score") or {}
        cur = ad.get("currency") or user_cur
        ad_spend = _num(sc.get("spend"))
        if cur != user_cur:
            mixed = True
            try:
                ad_spend = ad_spend * CurrencyService.get_rate(cur, user_cur)
            except Exception as e:
                logger.warning(f"creative rollup: no rate {cur}->{user_cur}: {e}")
                rate_missing = True
                ad_spend = None
        leads += _num(sc.get("leads"))
        imps += _num(sc.get("impressions"))
        link_clicks += _num(sc.get("link_clicks"))
        if ad.get("format") == "video" or sc.get("video_views_3s"):
            views += _num(sc.get("video_views_3s"))
            video_imps += _num(sc.get("impressions"))
        if ad_spend is not None:
            spend += ad_spend
            rated_leads += _num(sc.get("leads"))
        key = ad.get("ad_account_id") or ad.get("id") or str(len(accounts))
        accounts[key] = sc.get("state", "scoring")

    states = list(accounts.values())
    return {
        "spend": round(spend, 2),
        "currency": user_cur,
        "mixed_currency": mixed,
        "rate_missing": rate_missing,
        "leads": int(leads),
        "cpl": round(spend / rated_leads, 2) if rated_leads > 0 else None,
        "ctr": round(link_clicks / imps * 100, 2) if imps > 0 else None,
        "hook_rate": round(views / video_imps * 100, 2) if video_imps > 0 else None,
        "accounts_total": len(states),
        "accounts_scored": sum(1 for s in states if s in ("pass", "fail")),
        "accounts_pass": sum(1 for s in states if s == "pass"),
        "accounts_fail": sum(1 for s in states if s == "fail"),
    }
