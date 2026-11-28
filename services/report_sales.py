"""
services/report_sales.py
------------------------
Sales Coach's outcome figures as Slack report metrics (services/report_template.CATALOG):

    booked               contacts the owner's "booked" definition counts (a GoHighLevel tag or stage)
    closed               the same for "closed"
    speed_to_lead        average minutes to the first call, over the calls HotProspector measured it for
    speed_within_target  the share of those calls made within the owner's speed target

They are counted when the report is built, by the code Sales Coach's get_sales_outcomes uses
(ai/team/sales_coach.py), with the definitions and target the owner saved in Sales Coach's setup,
for the clients Sales Coach coaches. Nothing comes from the model. A client with nothing cached
has no value ("no data"), never an estimate.

By-day series are not offered: a tag-defined count is only cached per window, not per day. Charts by
client and period comparison (show=change, and compare="previous" on a chart) work as for every metric.
"""

import logging

logger = logging.getLogger(__name__)

METRICS = ("booked", "closed", "speed_to_lead", "speed_within_target")
# What each needs, as report_template source names (see `sources`).
BOOKED, CLOSED, SPEED, SPEED_TARGET = "sales_booked", "sales_closed", "sales_speed", "sales_speed_target"


async def sources(db, user_id: str) -> set[str]:
    """Which of the metrics this account can fill, once Sales Coach's setup has an answer: a definition for
    booked / closed, a call centre on a coached client for speed to lead, and a speed target as well for
    the share within it."""
    from ai.team import setup as team_setup
    from services.report_template import sources_connected
    try:
        settings = await team_setup.sales_coach_settings(db, user_id)
        outcomes = settings.get("outcomes") or {}
        if not (outcomes or settings.get("speed_minutes")):
            return set()   # Sales Coach isn't set up: get_sales_outcomes refuses too
        out = {s for s, key in ((BOOKED, "booked"), (CLOSED, "closed")) if outcomes.get(key)}
        ids = await team_setup.coached_ids(db, user_id, settings)
        groups = await db["client_groups"].find(
            {"user_id": user_id, "id": {"$in": ids}},
            {"_id": 0, "ghl_location_id": 1, "hotprospector_group_id": 1, "hotprospector_call_cache": 1,
             "call_log_provider": 1}).to_list(length=1000)
    except Exception:
        logger.warning("report sales metrics: settings read failed for %s", user_id, exc_info=True)
        return set()
    if "hp_call" in sources_connected(groups):
        out.add(SPEED)
        if settings.get("speed_minutes"):
            out.add(SPEED_TARGET)
    return out


async def load(db, user_id: str, group_ids: list[str], presets: set[str], metrics: set[str]) -> dict:
    """{(client id, preset): {"booked": n | None, "closed": n | None, "speed": [seconds] | None,
    "target": minutes | None}} for the coached clients among `group_ids`, for what `metrics` need."""
    from ai.team import sales_coach as sc
    from ai.team import setup as team_setup
    from core.constants import ghl_date_bounds
    try:
        settings = await team_setup.sales_coach_settings(db, user_id)
        coached = set(await team_setup.coached_ids(db, user_id, settings))
    except Exception:
        logger.warning("report sales metrics: settings read failed for %s", user_id, exc_info=True)
        return {}
    outcomes, target = settings.get("outcomes") or {}, settings.get("speed_minutes")
    ids = [g for g in group_ids if g in coached]
    if not ids:
        return {}
    locations = {g["id"]: g.get("ghl_location_id") for g in await db["client_groups"].find(
        {"user_id": user_id, "id": {"$in": ids}}, {"_id": 0, "id": 1, "ghl_location_id": 1}).to_list(length=None)}
    want_speed = bool({"speed_to_lead", "speed_within_target"} & metrics)
    out = {}
    for preset in presets:
        start, end = ghl_date_bounds(preset)
        for gid in ids:
            row = {"booked": None, "closed": None, "speed": None, "target": target}
            for key in ("booked", "closed"):
                if key in metrics and outcomes.get(key):
                    row[key] = (await sc.outcome(db, user_id, gid, outcomes[key], preset, start, end)).get("count")
            if want_speed and locations.get(gid):
                row["speed"] = await sc.speed_seconds(db, user_id, locations[gid], start, end)
            out[(gid, preset)] = row
    return out


def value(metric: str, rows: list[dict]) -> float | None:
    """One metric over some clients' rows from `load`: counts add up, speed averages over every call."""
    if metric in ("booked", "closed"):
        counts = [r[metric] for r in rows if r.get(metric) is not None]
        return float(sum(counts)) if counts else None
    seconds = [s for r in rows for s in (r.get("speed") or [])]
    if not seconds:
        return None
    if metric == "speed_to_lead":
        return sum(seconds) / len(seconds) / 60
    target = next((r["target"] for r in rows if r.get("target")), None)
    if not target:
        return None
    return 100 * sum(1 for s in seconds if s <= target * 60) / len(seconds)
