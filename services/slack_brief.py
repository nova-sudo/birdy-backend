"""
services/slack_brief.py
-----------------------
The scheduled Slack brief: the daily or weekly summary each account chooses in
onboarding / Settings (users.integrations.slack_bot.brief) and receives in the
channel Birdy posts suggestions to.

What the brief says is a Block Kit message with live placeholders and data
blocks (services/report_blocks.py), written by the user or the report
copilot; older briefs are converted on read, and one renderer fills them all.

A cron tick (hourly) calls send_due_briefs(). A brief is due when the user's
local clock has passed the chosen time on a matching day and that period has
not been sent yet. `integrations.slack_bot.brief_last_sent` records the local
date of the last send; it is claimed atomically *before* posting, so two ticks
racing each other cannot both send. A failed post releases the claim so the
next tick retries. A tick that runs more than LATE_LIMIT after the chosen time
skips that period rather than delivering a "morning" brief in the evening.

Every figure is read from the same caches, through the same metric registry,
that the app's own pages use — the brief never computes a number the app would
not show. The only model-written text is the optional "Birdy's take", which is
fed the rendered lines and nothing else.

Each send stores its figures (`brief_last_figures`) so the next one can show
the change against the period before.

Periods: a daily brief reports *yesterday*; a weekly brief reports the *last 7
days*. Both are presets the Meta / GHL refreshes already cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
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


async def send_brief(db, user_id: str, brief: dict, now_utc: datetime,
                     period_key: str | None = None, last_figures: dict | None = None) -> str:
    """
    Build and post one brief. Returns SENT, SKIPPED (nothing to send to or
    about — not worth retrying) or FAILED (Slack refused; retry next tick).
    """
    from services.slack_bot_service import get_notify_target

    template = rt.normalize(brief)
    if not rt.has_content(template):
        return SKIPPED

    bot_token, channel_id = await get_notify_target(db, user_id)
    if not bot_token or not channel_id:
        return SKIPPED

    frequency = template["frequency"]
    period_key = period_key or now_utc.date().isoformat()
    previous = rt.previous_for(last_figures, period_key, frequency)
    ctx = await rt.build_context(db, user_id, template, now_utc, previous=previous)
    if not ctx.groups:
        logger.info("slack brief: %s has no active clients — skipping", user_id)
        return SKIPPED

    take = await rt.write_take(db, user_id, template, rt.render(template, ctx)[1])
    blocks, fallback = rt.render(template, ctx, take=take)

    from slack_sdk.web.async_client import AsyncWebClient
    try:
        await AsyncWebClient(token=bot_token).chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks,
        )
    except Exception as e:
        logger.warning("slack brief: post failed for %s: %s", user_id, e)
        return FAILED

    await db["users"].update_one(
        {"user_id": user_id},
        {"$set": {"integrations.slack_bot.brief_last_figures": rt.snapshot(ctx, period_key)}},
    )
    return SENT


async def send_due_briefs(db, now_utc: datetime | None = None) -> dict:
    """Send every brief that is due. Safe to call as often as you like."""
    now_utc = now_utc or datetime.now(timezone.utc)
    stats = {"checked": 0, "due": 0, SENT: 0, SKIPPED: 0, FAILED: 0}

    cursor = db["users"].find(
        {"integrations.slack_bot.brief.frequency": {"$in": list(_PERIODS)}},
        {"user_id": 1, "timezone": 1, "integrations.slack_bot.brief": 1,
         "integrations.slack_bot.brief_last_sent": 1,
         "integrations.slack_bot.brief_last_figures": 1},
    )
    async for user in cursor:
        stats["checked"] += 1
        slack_bot = (user.get("integrations") or {}).get("slack_bot") or {}
        brief = slack_bot.get("brief") or {}
        last_sent = slack_bot.get("brief_last_sent")
        period = due_period(brief, user.get("timezone"), last_sent, now_utc)
        if not period:
            continue
        stats["due"] += 1

        # Claim the period before posting, so a concurrent tick can't double-send.
        claim = await db["users"].update_one(
            {"user_id": user["user_id"],
             "integrations.slack_bot.brief_last_sent": {"$ne": period}},
            {"$set": {"integrations.slack_bot.brief_last_sent": period}},
        )
        if not claim.modified_count:
            continue

        try:
            outcome = await send_brief(db, user["user_id"], brief, now_utc, period_key=period,
                                       last_figures=slack_bot.get("brief_last_figures"))
        except Exception as e:
            logger.error("slack brief: failed for %s: %s", user["user_id"], e, exc_info=True)
            outcome = FAILED

        stats[outcome] += 1
        if outcome == FAILED:
            # Release the claim so the next tick (within LATE_LIMIT) retries.
            await db["users"].update_one(
                {"user_id": user["user_id"], "integrations.slack_bot.brief_last_sent": period},
                {"$set": {"integrations.slack_bot.brief_last_sent": last_sent}},
            )

    logger.info("slack briefs: %s", stats)
    return stats
