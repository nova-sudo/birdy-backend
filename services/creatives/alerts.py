"""Alert evaluation for creative tests (alert scope "creative_tests").

Mirrors services.alert_service.evaluate_alert's return shape. tracking_mode
"per_client" means per ad account here: per_client_results carries
group_id = ad_account_id and group_name = the client group's name.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.constants import OPERATOR_LABELS
from services.creatives.store import ARCHIVED, LIVE_STATUSES, NOT_MANAGED, POSTING, TEST_ADS, TESTS

logger = logging.getLogger(__name__)

LIVE_TEST_STATUSES = LIVE_STATUSES
SCORE_LABELS = {"cpl": "CPL", "ctr": "CTR", "hook_rate": "Hook rate"}
SCORE_METRICS = {"creative_cpl": "cpl", "creative_ctr": "ctr", "creative_hook_rate": "hook_rate"}
NOT_AVAILABLE = "Not available for creative tests"
OPS = {
    "gt": lambda v, t: v > t,
    "lt": lambda v, t: v < t,
    "eq": lambda v, t: v == t,
    "neq": lambda v, t: v != t,
}


def _result(triggered: bool, value: float, progress: float, threshold: float, message: str) -> dict:
    return {"triggered": triggered, "current_value": value, "progress_pct": round(progress, 1),
            "threshold": threshold, "message": message}


def _naive(dt):
    if isinstance(dt, datetime) and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _progress(op: str, value: float, threshold: float, triggered: bool) -> float:
    if triggered:
        return 100.0
    if op == "gt" and threshold:
        return max(0.0, min(value / threshold * 100, 100.0))
    if op == "lt" and value > 0:
        return max(0.0, min(threshold / value * 100, 100.0))
    return 0.0


async def _tests(db, alert: dict, *, include_posting: bool = False) -> list[dict]:
    query: dict = {"user_id": alert.get("user_id"), **NOT_MANAGED}
    ids = alert.get("creative_test_ids") or []
    if ids:
        query["id"] = {"$in": ids}
        query["status"] = {"$ne": ARCHIVED}
    else:
        statuses = (POSTING, *LIVE_TEST_STATUSES) if include_posting else LIVE_TEST_STATUSES
        query["status"] = {"$in": list(statuses)}
    return await db[TESTS].find(query).to_list(None)


async def _ads(db, alert: dict, tests: list[dict]) -> list[dict]:
    if not tests:
        return []
    return await db[TEST_ADS].find(
        {"user_id": alert.get("user_id"), "test_id": {"$in": [t["id"] for t in tests]}}).to_list(None)


async def _account_names(db, user_id: str, ads: list[dict]) -> dict:
    gids = {a.get("client_group_id") for a in ads if a.get("client_group_id")}
    groups = await db["client_groups"].find({"user_id": user_id, "id": {"$in": list(gids)}},
                                            {"id": 1, "name": 1}).to_list(None) if gids else []
    by_gid = {g["id"]: g.get("name") for g in groups}
    names = {}
    for a in ads:
        names.setdefault(a.get("ad_account_id"), by_gid.get(a.get("client_group_id")) or a.get("ad_account_id"))
    return names


def _verdict_fired(alert: dict, tests: list[dict]) -> list[dict]:
    last = _naive(alert.get("last_triggered_at"))
    fired = []
    for t in tests:
        if t.get("verdict") not in ("winner", "loser"):
            continue
        at = _naive(t.get("verdict_at"))
        if at is None:
            continue
        if last is None or at > last:
            fired.append(t)
    return fired


async def evaluate_creative_alert(alert: dict, db) -> dict:
    condition = alert.get("condition", {})
    metric = condition.get("metric", "")
    operator = condition.get("operator", "")
    threshold = float(condition.get("value", 0))
    per_account = alert.get("tracking_mode") == "per_client"
    try:
        tests = await _tests(db, alert, include_posting=metric == "creative_post_failed")

        if metric == "creative_verdict":
            fired = _verdict_fired(alert, tests)
            if fired:
                msg = "; ".join(f"{t.get('code')} is a {t['verdict']}" for t in fired)
                return _result(True, 1.0, 100.0, threshold, msg)
            return _result(False, 0.0, 0.0, threshold, "No new creative verdicts")

        if operator.startswith("pct_"):
            return _result(False, 0.0, 0.0, threshold, NOT_AVAILABLE)

        ads = await _ads(db, alert, tests)
        if metric == "creative_post_failed":
            op, thr, threshold = "gt", 0.0, 0.0  # always "any failed post", whatever the saved condition
            label = "Creative ads failed to post"
            by_acct: dict = {}
            for a in ads:
                by_acct.setdefault(a.get("ad_account_id"), []).append(1.0 if a.get("post_state") == "failed" else 0.0)
            values = {k: sum(v) for k, v in by_acct.items()}
        elif metric in SCORE_METRICS:
            if operator not in OPS:
                return _result(False, 0.0, 0.0, threshold, NOT_AVAILABLE)
            op, thr = operator, threshold
            key = SCORE_METRICS[metric]
            label = SCORE_LABELS[key]
            values = {}
            for a in ads:
                if a.get("post_state") != "active":
                    continue
                if (a.get("score") or {}).get("rate_missing"):
                    continue  # still in the account's currency: not comparable with the threshold
                v = (a.get("score") or {}).get(key)
                if v is None:
                    continue
                acct = a.get("ad_account_id")
                prev = values.get(acct)
                crosses = OPS[op](float(v), thr)
                # Per account keep a crossing ad's value if any, else the first seen.
                if prev is None or (crosses and not OPS[op](prev, thr)):
                    values[acct] = float(v)
        else:
            return _result(False, 0.0, 0.0, threshold, f"Unsupported creative metric: {metric}")

        if not values:
            return _result(False, 0.0, 0.0, threshold, "No live creative ads to evaluate")

        names = await _account_names(db, alert.get("user_id"), ads)
        rows = []
        for acct, v in values.items():
            hit = OPS[op](v, thr)
            rows.append({"triggered": hit, "current_value": v, "progress_pct": round(_progress(op, v, thr, hit), 1),
                         "threshold": threshold, "group_id": acct, "group_name": names.get(acct) or acct,
                         "message": (f"{int(v)} failed post(s)" if metric == "creative_post_failed" else
                                    f"{label} is {v:g}, watching for {OPERATOR_LABELS.get(op, op)} {threshold:g}")})
        hits = [r for r in rows if r["triggered"]]
        worst = max(rows, key=lambda r: r["progress_pct"])
        if per_account:
            msg = f"Triggered for: {', '.join(r['group_name'] for r in hits)}" if hits else worst["message"]
            return {**_result(bool(hits), worst["current_value"], worst["progress_pct"], threshold, msg),
                    "per_client_results": rows}
        if metric == "creative_post_failed":
            total = sum(values.values())
            return _result(total > 0, total, 100.0 if total > 0 else 0.0, threshold,
                           f"{int(total)} creative ad(s) failed to post" if total > 0 else "No failed creative ads")
        pick = hits[0] if hits else worst
        return _result(bool(hits), pick["current_value"], pick["progress_pct"], threshold, pick["message"])
    except Exception as e:
        logger.error(f"Error evaluating creative alert {alert.get('id')}: {e}", exc_info=True)
        return _result(False, 0.0, 0.0, threshold, str(e))


def _channels(test: dict) -> list[str]:
    raw = (test.get("on_verdict") or {}).get("notify_channels") or []
    out = [c for c in raw if isinstance(c, str) and c]
    return out or ["in_app"]


async def create_verdict_alert(db, user_id: str, test: dict) -> str:
    """The wizard's "notify when a verdict lands" alert: one per test."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    alert_id = f"alert_{user_id}_{int(now.timestamp() * 1000)}_{test['id'][:8]}"
    await db["alerts"].insert_one({
        "id": alert_id,
        "user_id": user_id,
        "name": f"{test.get('code')} verdict",
        "description": f"Notify when {test.get('code')} becomes a winner or loser",
        "type": "win",
        "scope": "creative_tests",
        "creative_test_ids": [test["id"]],
        "condition": {"metric": "creative_verdict", "operator": "eq", "value": 1, "period": "week"},
        "target_group_ids": [],
        "target_group_names": [],
        "notification_channels": _channels(test),
        "frequency": "hourly",
        "tracking_mode": "total",
        "status": "active",
        "last_triggered_at": None,
        "last_evaluated_at": None,
        "trigger_count": 0,
        "snoozed_until": None,
        "per_client_results": [],
        "snoozed_clients": {},
        "auto_created": True,
        "created_at": now,
        "updated_at": now,
    })
    return alert_id
