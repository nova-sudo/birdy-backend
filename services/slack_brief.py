"""
services/slack_brief.py
-----------------------
The scheduled Slack brief: the daily or weekly summary each account chooses in
onboarding / Settings (users.integrations.slack_bot.brief) and receives in the
channel Birdy posts suggestions to.

Settings shape, as routers/onboarding.py writes it:

    brief = {
        "frequency": "daily" | "weekly",
        "time": "9:00 AM",                 # a whole hour, in the user's timezone
        "day": "Monday" | None,            # weekly only
        "items": {"spend", "leads", "conversion", "top", "alerts", "underperform": bool},
    }

A cron tick (hourly) calls send_due_briefs(). A brief is due when the user's
local clock has passed the chosen time on a matching day and that period has
not been sent yet. `integrations.slack_bot.brief_last_sent` records the local
date of the last send; it is claimed atomically *before* posting, so two ticks
racing each other cannot both send. A failed post releases the claim so the
next tick retries. A tick that runs more than LATE_LIMIT after the chosen time
skips that period rather than delivering a "morning" brief in the evening.

Every figure is read from the same caches, through the same metric registry,
that the app's own pages use — the brief never computes a number the app would
not show. Nothing here is LLM-written.

Periods: a daily brief reports *yesterday*; a weekly brief reports the *last 7
days*. Both are presets the Meta / GHL refreshes already cache.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.metric_orchestrator import aggregate_metric, get_metric_value

logger = logging.getLogger(__name__)

# The app's timezone picker lists IANA names; accounts that never chose one are
# overwhelmingly UK agencies (GBP ad accounts), so London is the honest default.
DEFAULT_TIMEZONE = "Europe/London"
LATE_LIMIT = timedelta(hours=3)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
ITEM_KEYS = ("spend", "leads", "conversion", "top", "alerts", "underperform")

_PERIODS = {
    "daily": {"preset": "yesterday", "label": "Yesterday", "days": 1},
    "weekly": {"preset": "last_7d", "label": "Last 7 days", "days": 7},
}

_CURRENCY_SYMBOLS = {
    "USD": "$", "GBP": "£", "EUR": "€", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "AED": "AED ", "INR": "₹", "ZAR": "R", "JPY": "¥",
}


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
    frequency = brief.get("frequency")
    if frequency not in _PERIODS:
        return None
    if not any((brief.get("items") or {}).get(k) for k in ITEM_KEYS):
        return None  # every section switched off — nothing to send

    local_now = now_utc.astimezone(user_zone(tz_name))
    if frequency == "weekly" and WEEKDAYS[local_now.weekday()] != (brief.get("day") or "Monday"):
        return None

    scheduled = datetime.combine(local_now.date(), parse_time(brief.get("time")), local_now.tzinfo)
    if not (scheduled <= local_now < scheduled + LATE_LIMIT):
        return None

    period = local_now.date().isoformat()
    return None if last_sent == period else period


# ── Content ───────────────────────────────────────────────────────────────────


def _money(amount: float, currency: str, pence: bool = False) -> str:
    """Totals in whole units; per-lead costs (pence=True) to two places, where
    rounding £1.49 and £2.51 to "£1" and "£3" would hide the difference."""
    symbol = _CURRENCY_SYMBOLS.get((currency or "").upper(), f"{(currency or '').upper()} ")
    return f"{symbol}{amount:,.2f}" if pence else f"{symbol}{amount:,.0f}"


async def gather_brief(db, user_id: str, frequency: str, now_utc: datetime) -> dict:
    """The figures for one brief. Pure reads."""
    period = _PERIODS[frequency]
    preset = period["preset"]

    groups = await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        {"_id": 0, "id": 1, "name": 1, "facebook_cache": 1, "ghl_opp_cache": 1,
         "gohighlevel_cache.metrics.opportunity_stats": 1},
    ).to_list(length=1000)

    user = await db["users"].find_one({"user_id": user_id}, {"default_currency": 1}) or {}
    currency = user.get("default_currency") or "GBP"

    spend = aggregate_metric("meta_spend", groups, preset)
    leads = aggregate_metric("meta_leads", groups, preset)
    # The GHL reader falls back to lifetime ("maximum") stats when a group has
    # no entry for the preset — right for a dashboard, wrong for "yesterday".
    # Only count groups whose cache actually holds this period.
    with_period = [g for g in groups if isinstance((g.get("ghl_opp_cache") or {}).get(preset), dict)]
    won = aggregate_metric("ghl_won_opps", with_period, preset)
    opps = aggregate_metric("ghl_total_opps", with_period, preset)
    spending_clients = sum(1 for g in groups if get_metric_value("meta_spend", g, preset) > 0)

    # Top performer: most leads in the period; cheaper leads break a tie.
    top = None
    for g in groups:
        g_leads = get_metric_value("meta_leads", g, preset)
        if g_leads <= 0:
            continue
        g_spend = get_metric_value("meta_spend", g, preset)
        cpl = g_spend / g_leads
        if top is None or (g_leads, -cpl) > (top["leads"], -top["cpl"]):
            top = {"name": g.get("name") or "Client", "leads": g_leads, "cpl": cpl}

    since = (now_utc - timedelta(days=period["days"])).replace(tzinfo=None)
    alerts = await db["alert_notifications"].find(
        {"user_id": user_id, "triggered_at": {"$gte": since}},
        {"_id": 0, "message": 1},
    ).sort("triggered_at", -1).to_list(length=50)

    underperforming = await db["ai_suggestions"].find(
        {"user_id": user_id, "status": "open", "action.type": "pause_ads"},
        {"_id": 0, "title": 1, "client_name": 1},
    ).to_list(length=50)

    return {
        "label": period["label"],
        "currency": currency,
        "clients": len(groups),
        "spending_clients": spending_clients,
        "spend": spend,
        "leads": leads,
        "won": won,
        "opportunities": opps,
        "top": top,
        "alerts": alerts,
        "underperforming": underperforming,
    }


def build_brief_blocks(data: dict, items: dict, frequency: str) -> tuple[list[dict], str]:
    """Slack blocks + fallback text, one line per section the user switched on."""
    cur = data["currency"]
    lines = []
    if items.get("spend"):
        who = f" across {data['spending_clients']} client{'s' if data['spending_clients'] != 1 else ''}"
        lines.append(f"💰 *Spend:* {_money(data['spend'], cur)}{who if data['spending_clients'] else ''}")
    if items.get("leads"):
        cpl = f" at {_money(data['spend'] / data['leads'], cur, pence=True)} per lead" if data["leads"] else ""
        lines.append(f"📈 *New leads:* {data['leads']:,.0f}{cpl}")
    if items.get("conversion"):
        if data["opportunities"]:
            rate = data["won"] / data["opportunities"] * 100
            lines.append(f"🎯 *Conversion rate:* {rate:.1f}% ({data['won']:,.0f} won of {data['opportunities']:,.0f} opportunities)")
        else:
            lines.append("🎯 *Conversion rate:* no opportunities closed or opened")
    if items.get("top"):
        top = data["top"]
        if top:
            lines.append(f"🏆 *Top performer:* {top['name']} — {top['leads']:,.0f} leads at {_money(top['cpl'], cur, pence=True)} each")
        else:
            lines.append("🏆 *Top performer:* no client generated leads")
    if items.get("alerts"):
        alerts = data["alerts"]
        if alerts:
            first = alerts[0].get("message") or ""
            more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
            lines.append(f"🔔 *Alerts:* {len(alerts)} triggered — {first}{more}")
        else:
            lines.append("🔔 *Alerts:* none triggered")
    if items.get("underperform"):
        flagged = data["underperforming"]
        if flagged:
            names = ", ".join(s.get("title", "").split("—")[-1].strip() for s in flagged[:3])
            more = f" +{len(flagged) - 3} more" if len(flagged) > 3 else ""
            lines.append(f"⚠️ *Underperforming:* {len(flagged)} ad{'s' if len(flagged) != 1 else ''} flagged for review — {names}{more}")
        else:
            lines.append("⚠️ *Underperforming:* no ads flagged")

    heading = f"Birdy {'daily' if frequency == 'daily' else 'weekly'} brief · {data['label']}"
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": heading}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"{data['clients']} active client{'s' if data['clients'] != 1 else ''} · change what's included in Birdy → Settings"}]},
    ]
    return blocks, f"{heading}\n" + "\n".join(lines)


# ── Sending ───────────────────────────────────────────────────────────────────


SENT, SKIPPED, FAILED = "sent", "skipped", "failed"


async def send_brief(db, user_id: str, brief: dict, now_utc: datetime) -> str:
    """
    Build and post one brief. Returns SENT, SKIPPED (nothing to send to or
    about — not worth retrying) or FAILED (Slack refused; retry next tick).
    """
    from services.slack_bot_service import get_notify_target

    bot_token, channel_id = await get_notify_target(db, user_id)
    if not bot_token or not channel_id:
        return SKIPPED

    data = await gather_brief(db, user_id, brief["frequency"], now_utc)
    if not data["clients"]:
        logger.info("slack brief: %s has no active clients — skipping", user_id)
        return SKIPPED

    blocks, fallback = build_brief_blocks(data, brief.get("items") or {}, brief["frequency"])
    from slack_sdk.web.async_client import AsyncWebClient
    try:
        await AsyncWebClient(token=bot_token).chat_postMessage(
            channel=channel_id, text=fallback, blocks=blocks,
        )
        return SENT
    except Exception as e:
        logger.warning("slack brief: post failed for %s: %s", user_id, e)
        return FAILED


async def send_due_briefs(db, now_utc: datetime | None = None) -> dict:
    """Send every brief that is due. Safe to call as often as you like."""
    now_utc = now_utc or datetime.now(timezone.utc)
    stats = {"checked": 0, "due": 0, SENT: 0, SKIPPED: 0, FAILED: 0}

    cursor = db["users"].find(
        {"integrations.slack_bot.brief.frequency": {"$in": list(_PERIODS)}},
        {"user_id": 1, "timezone": 1, "integrations.slack_bot.brief": 1,
         "integrations.slack_bot.brief_last_sent": 1},
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
            outcome = await send_brief(db, user["user_id"], brief, now_utc)
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
