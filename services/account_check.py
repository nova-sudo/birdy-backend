"""
services/account_check.py
-------------------------
The "watch my data" check: one verdict per client (high / medium / ok) with plain-English reasons, built from
numbers that already exist. No LLM: the connected agent writes the narrative.

Spend, leads and CPL for the period and the equal period before it come from the same code as get_daily_metrics
(ai/tools/multi_window_tools.fetch_group_daily). compare_periods only reads cached presets, which can't give an
N-day window with its prior period. Alerts fired come from `alert_notifications` (one row per firing pass,
counted as distinct alerts per client). Open cards come from `agent_cards` (stock and custom agents alike).
Clients are matched by `client_groups.id`, never by name.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta

from ai.agents.store import CARDS
from ai.tools import multi_window_tools as mw

logger = logging.getLogger(__name__)

MAX_CONCURRENCY = 8
HIGH_CPL_RATIO = 1.2
CPL_RISE_PCT = 25
LEADS_DROP_PCT = -25
MIN_PRIOR_LEADS = 5
OPEN_CARD_STATUSES = ("open", "flagged")
_RANK = {"high": 0, "medium": 1, "ok": 2}
_SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$", "NZD": "NZ$", "JPY": "¥", "INR": "₹"}


def _money(value, currency) -> str:
    code = (currency or "USD").upper()
    prefix = _SYMBOLS.get(code, f"{code} ")
    return f"{prefix}{value:,.0f}" if float(value).is_integer() else f"{prefix}{value:,.2f}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _attention(c: dict) -> tuple[str, list[str]]:
    cur = c["currency"]
    high: list[str] = []
    medium: list[str] = []
    cpl, target = c["cpl"], c["target_cpl"]
    if target and cpl is not None and cpl > target * HIGH_CPL_RATIO:
        high.append(f"CPL {_money(cpl, cur)} is {round((cpl / target - 1) * 100)}% over the {_money(target, cur)} target")
    if c["alerts_fired"] >= 1:
        high.append(f"{_plural(c['alerts_fired'], 'alert', 'alerts')} fired in this period")
    if (c["prior"]["spend"] or 0) > 0 and not c["spend"]:
        high.append(f"Spend stopped: {_money(c['prior']['spend'], cur)} in the prior period, nothing in this one")
    chg = c["change_pct"]
    if chg["cpl"] is not None and chg["cpl"] >= CPL_RISE_PCT:
        medium.append(f"CPL up {round(chg['cpl'])}% vs the prior period "
                      f"({_money(c['prior']['cpl'], cur)} to {_money(cpl, cur)})")
    if chg["leads"] is not None and chg["leads"] <= LEADS_DROP_PCT and (c["prior"]["leads"] or 0) >= MIN_PRIOR_LEADS:
        medium.append(f"Leads down {round(-chg['leads'])}% vs the prior period ({c['prior']['leads']} to {c['leads']})")
    if c["open_agent_cards"] > 0:
        medium.append(f"{_plural(c['open_agent_cards'], 'open agent card', 'open agent cards')} waiting for review")
    if high:
        return "high", high + medium
    return ("medium", medium) if medium else ("ok", [])


def _blank(group: dict) -> dict:
    return {
        "client": {"id": group.get("id"), "name": group.get("name")},
        "currency": group.get("ad_account_currency"),
        "spend": None, "leads": None, "cpl": None,
        "target_cpl": (group.get("targets") or {}).get("cpl"),
        "prior": {"spend": None, "leads": None, "cpl": None},
        "change_pct": {"spend": None, "leads": None, "cpl": None},
        "alerts_fired": 0, "open_agent_cards": 0,
    }


async def _load_metrics(group: dict, token, sem, since, prev_since, until) -> dict:
    """The client's numbers, or the reason there are none. Never raises."""
    row = _blank(group)
    try:
        async with sem:
            res, _ = await mw.fetch_group_daily(group, token, since, prev_since, until)
    except Exception as e:  # one bad client must not sink the check
        logger.warning("account check: client %s failed: %s", group.get("id"), e, exc_info=True)
        row["_error"] = str(e)[:120] or type(e).__name__
        return row
    if not group.get("meta_ad_account_id"):
        row["_note"] = "No Meta ad account connected"
    elif res.get("meta_error"):
        row["_error"] = res["meta_error"][:160]
    elif res.get("meta_note"):
        row["_note"] = res["meta_note"]
    else:
        s = res["summary"]
        row.update(spend=s["total"]["spend"], leads=s["total"]["leads"], cpl=s["total"]["cpl"])
        row["prior"] = {k: s["previous_period"][k] for k in ("spend", "leads", "cpl")}
        row["change_pct"] = {k: s["change_pct"][k] for k in ("spend", "leads", "cpl")}
    return row


async def _alerts_fired(db, user_id: str, since: datetime) -> dict[str, int]:
    """client id -> distinct alerts that fired since `since`. A firing that names its client counts for that
    client; otherwise it counts for each client the alert targets."""
    notes = await db["alert_notifications"].find(
        {"user_id": user_id, "triggered_at": {"$gte": since}}, {"alert_id": 1, "group_id": 1, "_id": 0},
    ).to_list(None)
    if not notes:
        return {}
    alert_ids = list({n["alert_id"] for n in notes if n.get("alert_id")})
    targets = {a["id"]: a.get("target_group_ids") or [] for a in await db["alerts"].find(
        {"user_id": user_id, "id": {"$in": alert_ids}}, {"id": 1, "target_group_ids": 1, "_id": 0}).to_list(None)}
    fired: dict[str, set] = {}
    for n in notes:
        gids = [n["group_id"]] if n.get("group_id") else targets.get(n.get("alert_id"), [])
        for gid in gids:
            fired.setdefault(gid, set()).add(n.get("alert_id"))
    return {gid: len(ids) for gid, ids in fired.items()}


async def _open_cards(db, user_id: str) -> dict[str, int]:
    rows = await db[CARDS].aggregate([
        {"$match": {"user_id": user_id, "status": {"$in": list(OPEN_CARD_STATUSES)}}},
        {"$group": {"_id": "$client_group_id", "n": {"$sum": 1}}},
    ]).to_list(None)
    return {r["_id"]: r["n"] for r in rows if r["_id"]}


async def build_account_check(db, mongo_client, user_id: str, days: int = 7) -> dict:
    today = date.today()
    until = (today - timedelta(days=1)).isoformat()
    since = (today - timedelta(days=days)).isoformat()
    prev_since = (today - timedelta(days=2 * days)).isoformat()
    prior_end = (today - timedelta(days=days + 1)).isoformat()

    groups = await db["client_groups"].find(
        {"user_id": user_id, "client_status": {"$ne": "Inactive"}},
        {"id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1, "meta_campaign_ids": 1,
         "targets": 1, "_id": 0},
    ).to_list(None)

    token = None
    if any(g.get("meta_ad_account_id") for g in groups):
        token = ((await mw.get_facebook_token(user_id, mongo_client)) or {}).get("access_token")

    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    rows, fired, cards = await asyncio.gather(
        asyncio.gather(*(_load_metrics(g, token, sem, since, prev_since, until) for g in groups)),
        _alerts_fired(db, user_id, datetime.combine(date.fromisoformat(since), datetime.min.time())),
        _open_cards(db, user_id),
    )

    clients = []
    for c in rows:
        gid = c["client"]["id"]
        c["alerts_fired"], c["open_agent_cards"] = fired.get(gid, 0), cards.get(gid, 0)
        error, note = c.pop("_error", None), c.pop("_note", None)
        if error:
            c["attention"], c["reasons"] = "medium", [f"Couldn't load data: {error}"]
        elif note:
            # No metrics, but alerts or open cards can still apply; the note goes last.
            c["attention"], reasons = _attention(c)
            c["reasons"] = reasons + [note]
        else:
            c["attention"], c["reasons"] = _attention(c)
        clients.append(c)

    clients.sort(key=lambda c: (_RANK[c["attention"]], -(c["spend"] or 0)))
    return {
        "period": {"start": since, "end": until, "prior_start": prev_since, "prior_end": prior_end},
        "clients": clients,
        "summary": {k: sum(1 for c in clients if c["attention"] == k) for k in ("high", "medium", "ok")},
    }
