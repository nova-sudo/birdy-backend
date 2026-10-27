"""
ai/agents/snapshot.py
---------------------
Read-only view of one client's ads for the agent evaluators: per-ad daily
spend/leads, Meta ad/adset state, and the client context (currency, CPL
target, per-client rule overrides).
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from ai.agents.store import AD_DAYS, RULES
from ai.suggestions.agents.useless_ad_purger import _symbol
from core.utils import get_result_value


@dataclass(frozen=True)
class AdDay:
    ad_id: str
    adset_id: str
    campaign_id: str
    ad_name: str
    day: date
    spend: float
    leads: int
    campaign_name: str = ""


@dataclass(frozen=True)
class AdState:
    ad_id: str
    adset_id: str
    campaign_id: str
    name: str
    effective_status: str
    created_time: datetime | None
    paused_at: date | None


@dataclass(frozen=True)
class AdsetState:
    adset_id: str
    campaign_id: str
    name: str
    effective_status: str
    campaign_status: str
    daily_budget: float
    lifetime_budget: float


@dataclass(frozen=True)
class ClientCtx:
    client_group_id: str
    name: str
    currency_symbol: str
    target_cpl: float | None
    overrides: dict = field(default_factory=dict)
    tz: str = "UTC"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _to_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def leads_from_insight(insight_data: dict) -> int:
    """Leads for one insight row; same definition as the cache and the CPL alerts."""
    if not insight_data:
        return 0
    return int(get_result_value([insight_data], "lead") or 0)


async def load_ad_days(db, user_id: str, client_group_id: str, start: date, end: date) -> dict[str, list[AdDay]]:
    """Per-ad daily rows in [start, end], sorted by day; one row per (ad, day), latest updated_at wins."""
    q = {
        "user_id": user_id,
        "client_group_id": client_group_id,
        "date_start": {"$gte": start.isoformat(), "$lte": end.isoformat()},
    }
    latest: dict[tuple, dict] = {}
    async for row in db[AD_DAYS].find(q):
        day = _to_date(row.get("date_start"))
        if day is None or not row.get("ad_id"):
            continue
        key = (row["ad_id"], day)
        cur = latest.get(key)
        if cur is None or (row.get("updated_at") or datetime.min) >= (cur.get("updated_at") or datetime.min):
            latest[key] = row

    out: dict[str, list[AdDay]] = {}
    for (ad_id, day), row in latest.items():
        data = row.get("insight_data") or {}
        out.setdefault(ad_id, []).append(AdDay(
            ad_id=ad_id,
            adset_id=row.get("adset_id") or "",
            campaign_id=row.get("campaign_id") or "",
            ad_name=row.get("ad_name") or "",
            day=day,
            spend=_num(data.get("spend")),
            leads=leads_from_insight(data),
            campaign_name=row.get("campaign_name") or data.get("campaign_name") or "",
        ))
    for days in out.values():
        days.sort(key=lambda d: d.day)
    return out


async def load_client_ctx(db, user_id: str, client_group_id: str, agent: dict) -> ClientCtx:
    group = await db["client_groups"].find_one({"id": client_group_id, "user_id": user_id}) or {}
    cpl = (group.get("targets") or {}).get("cpl")
    target = _num(cpl) if cpl not in (None, "") and _num(cpl) > 0 else None

    # Keyed by rule kind so one client's overrides for different rules don't collide.
    overrides: dict = {}
    async for rule in db[RULES].find({"agent_id": agent.get("_id")}):
        ov = (rule.get("client_overrides") or {}).get(client_group_id)
        if ov:
            overrides[rule["kind"]] = dict(ov)

    tz = ((agent.get("scope") or {}).get("schedule") or {}).get("tz") or "UTC"
    return ClientCtx(
        client_group_id=client_group_id,
        name=group.get("name") or "",
        currency_symbol=_symbol(group.get("ad_account_currency")),
        target_cpl=target,
        overrides=overrides,
        tz=tz,
    )


async def load_state(db, client_group_id: str) -> tuple[dict[str, AdState], dict[str, AdsetState]]:
    ads: dict[str, AdState] = {}
    async for d in db["meta_ad_state"].find({"client_group_id": client_group_id}):
        ads[d["ad_id"]] = AdState(
            ad_id=d["ad_id"],
            adset_id=d.get("adset_id") or "",
            campaign_id=d.get("campaign_id") or "",
            name=d.get("name") or "",
            effective_status=d.get("effective_status") or "",
            created_time=d.get("created_time"),
            paused_at=_to_date(d.get("paused_at")),
        )
    adsets: dict[str, AdsetState] = {}
    async for d in db["meta_adset_state"].find({"client_group_id": client_group_id}):
        adsets[d["adset_id"]] = AdsetState(
            adset_id=d["adset_id"],
            campaign_id=d.get("campaign_id") or "",
            name=d.get("name") or "",
            effective_status=d.get("effective_status") or "",
            campaign_status=d.get("campaign_status") or "",
            daily_budget=_num(d.get("daily_budget")),
            lifetime_budget=_num(d.get("lifetime_budget")),
        )
    return ads, adsets
