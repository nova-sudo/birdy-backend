"""Fired-alert history for the MCP tools: collapses the per-pass rows in alert_notifications into firings."""

from datetime import datetime, timedelta

from core.constants import METRIC_LABELS
from services.alert_service import format_condition_display

# The alert job runs hourly (Vercel cron "15 * * * *") and writes one alert_notifications row per pass while
# an alert stays triggered. The gap must be longer than that cadence (plus slack for a late or retried run),
# or every pass would read as a separate firing. Rows of the same (alert_id, group_id) no more than this far
# apart are one firing.
FIRING_GAP_MINUTES = 90
FIRING_GAP = timedelta(minutes=FIRING_GAP_MINUTES)


def collapse_firings(rows: list[dict]) -> list[dict]:
    """Group notification rows into firings: consecutive rows (gap <= FIRING_GAP) for the same
    (alert_id, group_id) become one {alert_id, group_id, first_triggered_at, last_seen_at, count,
    message, current_value} (message/value from the latest row). Newest firing first."""
    by_key: dict[tuple, list[dict]] = {}
    for r in sorted(rows, key=lambda r: r["triggered_at"]):
        by_key.setdefault((r.get("alert_id"), r.get("group_id")), []).append(r)
    firings = []
    for (alert_id, group_id), rs in by_key.items():
        cur = None
        for r in rs:
            if cur and r["triggered_at"] - cur["last_seen_at"] <= FIRING_GAP:
                cur.update(last_seen_at=r["triggered_at"], count=cur["count"] + 1,
                           message=r.get("message"), current_value=r.get("current_value"))
            else:
                cur = {"alert_id": alert_id, "group_id": group_id, "first_triggered_at": r["triggered_at"],
                       "last_seen_at": r["triggered_at"], "count": 1, "message": r.get("message"),
                       "current_value": r.get("current_value")}
                firings.append(cur)
    firings.sort(key=lambda f: f["last_seen_at"], reverse=True)
    return firings


def _iso(d):
    return d.isoformat() if isinstance(d, datetime) else d


async def fired_alerts(db, user_id: str, days: int) -> list[dict]:
    since = datetime.utcnow() - timedelta(days=days)
    rows = await db["alert_notifications"].find(
        {"user_id": user_id, "triggered_at": {"$gte": since}}, {"_id": 0}).to_list(None)
    firings = collapse_firings(rows)
    alert_ids = list({f["alert_id"] for f in firings if f["alert_id"]})
    alerts = {a["id"]: a for a in await db["alerts"].find(
        {"user_id": user_id, "id": {"$in": alert_ids}},
        {"_id": 0, "id": 1, "name": 1, "condition": 1, "target_group_ids": 1}).to_list(None)}
    for f in firings:
        # The job logs no client on a row unless the alert is checked per client, so an
        # alert aimed at exactly one client came back looking account-wide.
        targets = (alerts.get(f["alert_id"]) or {}).get("target_group_ids") or []
        if not f["group_id"] and len(targets) == 1:
            f["group_id"] = targets[0]
    group_ids = list({f["group_id"] for f in firings if f["group_id"]})
    groups = {g["id"]: g.get("name") for g in await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": group_ids}}, {"_id": 0, "id": 1, "name": 1}).to_list(None)} if group_ids else {}
    out = []
    for f in firings:
        a = alerts.get(f["alert_id"], {})
        out.append({
            "alert_id": f["alert_id"],
            "alert_name": a.get("name"),
            "condition_display": format_condition_display(a["condition"]) if a.get("condition") else None,
            "group_id": f["group_id"],
            "client_name": groups.get(f["group_id"]) if f["group_id"] else None,
            "first_triggered_at": _iso(f["first_triggered_at"]),
            "last_seen_at": _iso(f["last_seen_at"]),
            "count": f["count"],
            "message": f["message"],
            "current_value": f["current_value"],
        })
    return out
