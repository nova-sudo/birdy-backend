"""
services/daily_refresh_slot.py
------------------------------
When the daily Meta / GHL / HotProspector refreshes become due: once a night,
shortly after midnight in the account owner's timezone, so the morning starts
with yesterday complete.

They used to be due 24 hours after a group's previous refresh. That drifted
with whenever the group was created or last refreshed by hand: on one account
the same client refreshed Meta at 05:38, GHL at 17:06 and HotProspector at
08:42 UTC, so "yesterday" was whatever part of it had happened by then.

A group is now due when its last refresh is older than the most recent slot
(DAILY_REFRESH_LOCAL_HOUR:00 local) or it has never refreshed. The ticks still
run every minute and work through due groups in their usual batches.
"""

import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Meta finalises a day's numbers shortly after midnight in the ad account's
# timezone; an hour's margin keeps the run clear of that.
DAILY_REFRESH_LOCAL_HOUR = int(os.getenv("DAILY_REFRESH_LOCAL_HOUR", "1"))
# Every account so far is UK-based; users who set a timezone in Settings get theirs.
DEFAULT_TIMEZONE = "Europe/London"


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def last_slot_utc(now_utc: datetime, tz_name: str | None = None) -> datetime:
    """The most recent nightly slot at or before `now_utc`, as naive UTC (how the stamps are stored)."""
    zone = _zone(tz_name)
    aware_now = now_utc.replace(tzinfo=timezone.utc)
    local_now = aware_now.astimezone(zone)
    slot = datetime.combine(local_now.date(), time(DAILY_REFRESH_LOCAL_HOUR), tzinfo=zone)
    if slot > local_now:
        slot = datetime.combine(local_now.date() - timedelta(days=1), time(DAILY_REFRESH_LOCAL_HOUR), tzinfo=zone)
    return slot.astimezone(timezone.utc).replace(tzinfo=None)


async def daily_due_clauses(db, stamp_field: str, now_utc: datetime) -> list[dict]:
    """
    `$or` branches matching groups due for their nightly refresh of `stamp_field`.

    Users on the default timezone share one branch; each user with another
    timezone set gets their own.
    """
    others = await db["users"].find(
        {"timezone": {"$nin": [None, "", DEFAULT_TIMEZONE]}},
        {"user_id": 1, "timezone": 1},
    ).to_list(None)

    by_slot: dict[datetime, list[str]] = {}
    for u in others:
        if u.get("user_id"):
            by_slot.setdefault(last_slot_utc(now_utc, u["timezone"]), []).append(u["user_id"])

    # {field: None} also matches a missing field: never refreshed is always due.
    clauses = [{stamp_field: None}]
    default_users = [uid for ids in by_slot.values() for uid in ids]
    clauses.append({"user_id": {"$nin": default_users}, stamp_field: {"$lt": last_slot_utc(now_utc)}})
    for slot, ids in by_slot.items():
        clauses.append({"user_id": {"$in": ids}, stamp_field: {"$lt": slot}})
    return clauses
