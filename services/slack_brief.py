"""
services/slack_brief.py
-----------------------
The scheduled Slack brief: the daily or weekly summary each account chooses in
onboarding / Settings (users.integrations.slack_bot.brief) and receives in the
channel Birdy posts suggestions to.

What the brief says is a Block Kit message with live placeholders and data
blocks (services/report_blocks.py), written by the user or the report
copilot; older briefs are converted on read, and one renderer fills them all.

The brief is the account's *default Slack report* (services/slack_reports.py):
sending, claiming and the 5-minute tick live there. This module keeps the
brief's schedule rules (due_period, parse_time, user_zone), which reports
share. A tick that runs more than LATE_LIMIT after the chosen time skips that
period rather than delivering a "morning" brief in the evening.

Every figure is read from the same caches, through the same metric registry,
that the app's own pages use — the brief never computes a number the app would
not show. The only model-written text is the optional "Birdy's take", which is
fed the rendered lines and nothing else.

Each send stores its figures on the report (`last_figures`) so the next one
can show the change against the period before.

Periods: a daily brief reports *yesterday*; a weekly brief reports the *last 7
days*. Both are presets the Meta / GHL refreshes already cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services import report_blocks as rt

logger = logging.getLogger(__name__)

# The app's timezone picker lists IANA names; accounts that never chose one are
# overwhelmingly UK agencies (GBP ad accounts), so London is the honest default.
DEFAULT_TIMEZONE = "Europe/London"
LATE_LIMIT = timedelta(hours=3)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ITEM_KEYS = rt.LEGACY_ITEM_KEYS
_PERIODS = rt.PERIODS


# ── Scheduling ────────────────────────────────────────────────────────────────


def parse_time(label: str | None) -> dtime:
    """'9:00 AM' → 09:00. Unparseable → 09:00, the app's own default."""
    try:
        return datetime.strptime((label or "").strip().upper(), "%I:%M %p").time()
    except ValueError:
        return dtime(9, 0)


def user_zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def due_period(brief: dict, tz_name: str | None, last_sent: str | None,
               now_utc: datetime) -> str | None:
    """
    The period key (the user's local date, ISO) to send now, or None.

    Due when: local time is at or past the chosen time, by no more than
    LATE_LIMIT; on the chosen weekday for weekly briefs; and not already sent
    for today.
    """
    brief = rt.normalize(brief)
    if not rt.has_content(brief):
        return None  # no schedule, or every section removed — nothing to send
    frequency = brief["frequency"]

    local_now = now_utc.astimezone(user_zone(tz_name))
    if frequency == "weekly" and WEEKDAYS[local_now.weekday()] != (brief.get("day") or "Monday"):
        return None

    scheduled = datetime.combine(local_now.date(), parse_time(brief.get("time")), local_now.tzinfo)
    if not (scheduled <= local_now < scheduled + LATE_LIMIT):
        return None

    period = local_now.date().isoformat()
    return None if last_sent == period else period


# ── Sending ───────────────────────────────────────────────────────────────────


SENT, SKIPPED, FAILED = "sent", "skipped", "failed"


async def send_due_briefs(db, now_utc: datetime | None = None) -> dict:
    """
    Send every brief that is due. The brief is now the account's default Slack
    report, so this is the report tick (services/slack_reports.py), which
    migrates any brief that has no report yet before it looks for due ones.
    """
    from services.slack_reports import send_due_reports
    return await send_due_reports(db, now_utc)
