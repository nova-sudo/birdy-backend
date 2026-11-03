"""
ai/agents/stock/windows.py
--------------------------
Date windows for the stock agent. Every window is in the ad account's own timezone and holds only
full days: the account's day still in progress is excluded, so an account behind the agency's
clock is never judged on a part day. Spec section 6, Timezones.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Window:
    start: date
    end: date
    prev_start: date
    prev_end: date


def _tz(name: str | None):
    try:
        return ZoneInfo(name) if name else timezone.utc
    except Exception:
        return timezone.utc


def last_full_day(now_utc: datetime, tz_name: str | None) -> date:
    """Yesterday in the account's timezone: the newest day that is complete there."""
    return now_utc.astimezone(_tz(tz_name)).date() - timedelta(days=1)


def full_day_window(now_utc: datetime, tz_name: str | None, days: int = 7) -> Window:
    """The last `days` full days and the same number of days before them."""
    end = last_full_day(now_utc, tz_name)
    start = end - timedelta(days=days - 1)
    prev_end = start - timedelta(days=1)
    return Window(start, end, prev_end - timedelta(days=days - 1), prev_end)
