"""
ai/agents/stock/months.py
-------------------------
The whole-month report (BirdyHub redesign, spec section 2): one row per watched client with the
month's leads, spend, CPL and zone, plus how many of its cards were decided in that month.

  month_rows   the rows for one agent and one month ("YYYY-MM")

Days come from `agent_ad_days.date_start`, which Meta writes as the ad account's own local date
(YYYY-MM-DD), so a month is a plain string range on it. `decided_at` is a UTC time, so it is put
into the agent's timezone before it is counted in a month.
"""

import calendar
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai.agents.guardrails import DEFAULT_TZ, agent_tz
from ai.agents.snapshot import load_ad_days
from ai.agents.stock.checks import scoped_days, zone
from ai.agents.stock.settings import DEFAULT_SETTINGS, merge_settings, resolve_zones
from ai.agents.stock.templates import _day
from ai.agents.store import CARDS


def parse_month(month: str) -> tuple[date, date]:
    """The first and last day of "YYYY-MM". Raises ValueError for anything else."""
    year, mon = (int(p) for p in month.split("-"))
    first = date(year, mon, 1)
    return first, date(year, mon, calendar.monthrange(year, mon)[1])


def _zone_info(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _utc_bounds(first: date, last: date, tz_name: str) -> tuple[datetime, datetime]:
    """Local midnight on `first` and on the day after `last`, as naive UTC (DST-safe)."""
    tz = _zone_info(tz_name)

    def at(d: date) -> datetime:
        return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)

    return at(first), at(last + timedelta(days=1))


def current_month(agent: dict, now_utc: datetime) -> str:
    """This month in the agent's timezone, as "YYYY-MM"."""
    now = now_utc if now_utc.tzinfo else now_utc.replace(tzinfo=timezone.utc)
    return now.astimezone(_zone_info(agent_tz(agent))).strftime("%Y-%m")


async def month_rows(db, agent: dict, month: str) -> dict:
    """One row per client in the agent's scope for `month`. With no ad rows in the month the figures are
    None (not zero): nothing was read, so nothing is claimed. Zones come from the same settings the cards use."""
    first, last = parse_month(month)
    tz_name = agent_tz(agent)
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    user_id = agent.get("user_id")
    start_utc, end_utc = _utc_bounds(first, last, tz_name)

    rows = []
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        group = await db["client_groups"].find_one({"id": gid, "user_id": user_id}) or {"id": gid}
        days = scoped_days(await load_ad_days(db, user_id, gid, first, last), group)
        picked = [d for rows_ in days.values() for d in rows_ if first <= d.day <= last]
        if picked:
            spend = round(sum(d.spend for d in picked), 2)
            leads = sum(d.leads for d in picked)
            cpl = round(spend / leads, 2) if leads else None
        else:
            spend = leads = cpl = None
        zones = resolve_zones(settings, gid, group.get("ad_account_currency"))
        if zones is None:
            zone_ = "no_zones"
        elif spend is None:
            zone_ = "no_data"
        else:
            zone_ = zone(spend, leads, zones)
        decided = await db[CARDS].count_documents({
            "agent_id": agent["_id"], "client_group_id": gid, "preview": {"$ne": True},
            "decided_at": {"$gte": start_utc, "$lt": end_utc}})
        rows.append({"client_group_id": gid, "client_name": group.get("name") or gid,
                     "currency": group.get("ad_account_currency"), "leads": leads, "spend": spend, "cpl": cpl,
                     "zone": zone_, "decided": decided})
    return {"month": month, "range": f"{_day(first)} – {_day(last)}", "rows": rows}
