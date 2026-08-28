"""
services/client_health.py
-------------------------
Client health — Healthy / Warning / Critical — derived from the client's own
monthly closes goal, not set by hand.

    expected = close_target × (days elapsed / days in month)
    deficit  = expected − actual closes
    pace     = actual / expected

Bands, and the reason they are `or` rather than `and`: a client must fail on
BOTH pace and deficit to be downgraded, so a client with a small target does
not false-alarm. Missing one close out of two is 50% pace, which would read
Critical on pace alone — the deficit arm keeps it Healthy.

    Healthy   pace >= 90%, OR less than 1 close behind
    Warning   pace >= 70%, OR less than 2 closes behind
    Critical  anything worse

Recomputed weekly (Monday 06:00) against data through the previous Sunday, so
the window is whole days and every client is judged on the same elapsed period.
"""

import calendar
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

HEALTHY = "Healthy"
WARNING = "Warning"
CRITICAL = "Critical"

# Band thresholds, named so the rule reads like the spec it came from.
HEALTHY_PACE = 0.90
HEALTHY_DEFICIT = 1.0
WARNING_PACE = 0.70
WARNING_DEFICIT = 2.0


def previous_sunday(today: date) -> date:
    """
    The Sunday on or before `today`, which is where the evaluation window ends.

    The job runs Monday 06:00 "using data through the previous Sunday", so on a
    Monday this is yesterday. Defined for any weekday so a manual re-run
    mid-week measures the same window the Monday run would have.
    """
    # date.weekday(): Monday 0 … Sunday 6
    return today - timedelta(days=today.weekday() + 1)


def month_progress(through: date) -> tuple[int, int]:
    """(days elapsed, days in month) for the month `through` falls in."""
    days_in_month = calendar.monthrange(through.year, through.month)[1]
    return through.day, days_in_month


def compute_health(
    close_target: float | None,
    actual_closes: float | None,
    days_elapsed: int,
    days_in_month: int,
) -> dict:
    """
    Evaluate one client. Returns the band plus the figures behind it, so the
    result can be explained rather than just asserted.

    A client with no target — or a target of zero — has nothing to be behind
    on and reads Healthy. Guessing a default target would invent an
    expectation the agency never set.
    """
    target = float(close_target or 0)
    actual = float(actual_closes or 0)

    if target <= 0 or days_in_month <= 0:
        return {
            "health": HEALTHY,
            "expected": 0.0,
            "actual": actual,
            "deficit": 0.0,
            "pace": None,          # undefined, not 100% — nothing was expected
            "close_target": target,
            "reason": "no monthly closes target set",
        }

    elapsed = max(0, min(days_elapsed, days_in_month))
    expected = target * (elapsed / days_in_month)

    if expected <= 0:
        # The month has not started yet; nothing can be behind.
        return {
            "health": HEALTHY,
            "expected": 0.0,
            "actual": actual,
            "deficit": 0.0,
            "pace": None,
            "close_target": target,
            "reason": "month has not started",
        }

    deficit = expected - actual
    pace = actual / expected

    if pace >= HEALTHY_PACE or deficit < HEALTHY_DEFICIT:
        health = HEALTHY
    elif pace >= WARNING_PACE or deficit < WARNING_DEFICIT:
        health = WARNING
    else:
        health = CRITICAL

    return {
        "health": health,
        "expected": round(expected, 2),
        "actual": actual,
        "deficit": round(deficit, 2),
        "pace": round(pace, 4),
        "close_target": target,
        "reason": f"{actual:g} of {expected:.1f} expected closes",
    }


def health_for_group(group: dict, through: date | None = None) -> dict:
    """
    Evaluate a client_groups document.

    `actual closes` is the won-opportunity count for the current month, read
    from the cached GHL opportunity stats rather than recounted here — the
    cache is what every other surface reports, so health agrees with the
    numbers the user can see.
    """
    through = through or previous_sunday(date.today())
    days_elapsed, days_in_month = month_progress(through)

    target = ((group.get("targets") or {}).get("monthly_wins"))

    # "this_month" is the preset whose window matches the rule's month-to-date
    # arithmetic; fall back to the legacy location if the cache predates it.
    opp_cache = group.get("ghl_opp_cache") or {}
    stats = opp_cache.get("this_month")
    if not stats:
        stats = (
            (group.get("gohighlevel_cache") or {})
            .get("metrics", {})
            .get("opportunity_stats", {})
        )
    actual = (stats or {}).get("won", 0)

    result = compute_health(target, actual, days_elapsed, days_in_month)
    result["through"] = through.isoformat()
    result["days_elapsed"] = days_elapsed
    result["days_in_month"] = days_in_month
    return result


async def recompute_all(db, through: date | None = None) -> dict:
    """
    Re-evaluate every client group and store the band on each.

    Writes only when the band actually changes, so an unchanged run costs no
    writes and `health_updated_at` means "when this last moved" rather than
    "when the job last ran".
    """
    through = through or previous_sunday(date.today())
    cursor = db["client_groups"].find(
        {}, {"id": 1, "user_id": 1, "targets": 1, "ghl_opp_cache.this_month": 1,
             "gohighlevel_cache.metrics.opportunity_stats": 1, "health": 1, "_id": 0},
    )

    counts = {HEALTHY: 0, WARNING: 0, CRITICAL: 0}
    changed = 0
    scanned = 0

    async for group in cursor:
        scanned += 1
        result = health_for_group(group, through)
        counts[result["health"]] += 1

        if group.get("health") == result["health"]:
            continue

        await db["client_groups"].update_one(
            {"id": group.get("id")},
            {"$set": {
                "health": result["health"],
                "health_detail": result,
                "health_updated_at": datetime.utcnow(),
            }},
        )
        changed += 1

    logger.info(
        "Client health recomputed through %s: %d scanned, %d changed, %s",
        through.isoformat(), scanned, changed, counts,
    )
    return {"scanned": scanned, "changed": changed, "counts": counts,
            "through": through.isoformat()}
