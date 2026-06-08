"""
services/hp_members.py
----------------------
HotProspector member-dashboard historization.

`getMemberDashboardData` is a per-day endpoint, so we keep a daily per-agent
time-series in `hotprospector_member_daily`, backfilled once for the current year
and finalized daily (yesterday). Any date-preset range is aggregated at read time;
TODAY and YESTERDAY are always fetched live (never served from the store) so the
Members tab is real-time for them.
"""
import logging
from datetime import date, datetime, timedelta

from pymongo import UpdateOne

from core.database import DB_NAME

logger = logging.getLogger(__name__)

# Count metrics that SUM across days. Rates are recomputed from these.
_SUM_FIELDS = [
    "outboundCall", "inboundCall", "hangups", "answered_calls",
    "convos", "Prospects", "Appts", "SMS", "talkMin",
]


def _num(v):
    """Parse HP's stringy numbers ('250', '44 %', '320.000000') to float."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _accumulate(sums, metrics):
    for f in _SUM_FIELDS:
        sums[f] += _num(metrics.get(f))


def _format(agent_id, name, email, sums):
    out_c, in_c, answered = sums["outboundCall"], sums["inboundCall"], sums["answered_calls"]
    dialed = out_c + in_c
    answer_rate = (answered / dialed * 100) if dialed else 0
    cr = (sums["convos"] / answered * 100) if answered else 0
    return {
        "agentId": agent_id,
        "agentName": name,
        "agentEmail": email,
        "dashboard": {
            "outboundCall": int(out_c),
            "inboundCall": int(in_c),
            "answered_calls": int(answered),
            "hangups": int(sums["hangups"]),
            "convos": int(sums["convos"]),
            "Appts": int(sums["Appts"]),
            "SMS": int(sums["SMS"]),
            "talkMin": int(sums["talkMin"]),
            "answer_rate": f"{round(answer_rate)} %",
            "cr": f"{round(cr, 2)} %",
        },
    }


# ── Storage ──────────────────────────────────────────────────────────────────

async def save_member_dashboard_day(user_id, date_str, results, mongo_client):
    """Upsert one day's per-agent metrics into hotprospector_member_daily."""
    col = mongo_client[DB_NAME]["hotprospector_member_daily"]
    ops = []
    for a in results or []:
        agent_id = str(a.get("agentId") or "")
        if not agent_id:
            continue
        ops.append(UpdateOne(
            {"user_id": user_id, "date": date_str, "agentId": agent_id},
            {"$set": {
                "user_id": user_id,
                "date": date_str,
                "agentId": agent_id,
                "agentName": a.get("agentName"),
                "agentEmail": a.get("agentEmail"),
                "metrics": a,
                "updated_at": datetime.utcnow(),
            }},
            upsert=True,
        ))
    if ops:
        await col.bulk_write(ops, ordered=False)
    return len(ops)


async def _set_member_backfill(user_id, fields, mongo_client):
    await mongo_client[DB_NAME]["users"].update_one(
        {"user_id": user_id},
        {"$set": {f"integrations.hotprospector.member_backfill.{k}": v for k, v in fields.items()}},
        upsert=True,
    )


# ── Backfill / finalize ──────────────────────────────────────────────────────

async def backfill_member_year(user_id, integration, mongo_client, days_per_run=20):
    """
    Resumable backfill of getMemberDashboardData per-day from Jan 1 (current year)
    through yesterday. Processes up to `days_per_run` days per call and advances
    users.integrations.hotprospector.member_backfill.progress_date so the next tick
    resumes. Returns {done, processed, progress_date}.
    """
    db = mongo_client[DB_NAME]
    udoc = await db["users"].find_one(
        {"user_id": user_id}, {"integrations.hotprospector.member_backfill": 1})
    mb = (((udoc or {}).get("integrations") or {}).get("hotprospector") or {}).get("member_backfill") or {}

    today = date.today()
    end = today - timedelta(days=1)  # yesterday; today/yesterday are served live
    prog = mb.get("progress_date")
    if prog:
        try:
            cur = date.fromisoformat(prog) + timedelta(days=1)
        except ValueError:
            cur = date(today.year, 1, 1)
    else:
        cur = date(today.year, 1, 1)

    processed = 0
    while cur <= end and processed < days_per_run:
        ds = cur.isoformat()
        ok, dash = await integration.get_member_dashboard_data(ds)
        if ok:
            await save_member_dashboard_day(user_id, ds, dash.get("results", []), mongo_client)
        await _set_member_backfill(user_id, {"progress_date": ds, "status": "running"}, mongo_client)
        cur += timedelta(days=1)
        processed += 1

    done = cur > end
    if done:
        await _set_member_backfill(
            user_id, {"status": "complete", "last_finalized_date": end.isoformat()}, mongo_client)
    logger.info(f"[hp-members] backfill user={user_id} processed={processed} done={done}")
    return {"done": done, "processed": processed, "progress_date": cur.isoformat()}


async def finalize_member_yesterday(user_id, integration, mongo_client):
    """Fetch yesterday's dashboard and upsert it into the store."""
    y = (date.today() - timedelta(days=1)).isoformat()
    ok, dash = await integration.get_member_dashboard_data(y)
    n = await save_member_dashboard_day(user_id, y, dash.get("results", []), mongo_client) if ok else 0
    await _set_member_backfill(user_id, {"last_finalized_date": y}, mongo_client)
    logger.info(f"[hp-members] finalized {y} for user={user_id}: {n} agents")
    return n


# ── Read-time aggregation ────────────────────────────────────────────────────

async def get_member_dashboard_range(user_id, start_date, end_date, integration, mongo_client):
    """
    Aggregate per-agent metrics over [start_date, end_date] (inclusive, 'YYYY-MM-DD').
    Store serves days <= today-2; YESTERDAY and TODAY (when in range) are live-fetched.
    Returns a list of {agentId, agentName, agentEmail, dashboard:{...}}.
    """
    today = date.today()
    start = start_date or date(today.year, 1, 1).isoformat()
    end = end_date or today.isoformat()
    store_end = min(end, (today - timedelta(days=2)).isoformat())

    agg = {}  # agentId -> {name, email, sums}

    def _bucket(agent_id, name, email):
        info = agg.get(agent_id)
        if info is None:
            info = {"name": name, "email": email, "sums": {f: 0.0 for f in _SUM_FIELDS}}
            agg[agent_id] = info
        if name:
            info["name"] = name
        if email:
            info["email"] = email
        return info

    # Historical days from the store.
    if start <= store_end:
        cur = mongo_client[DB_NAME]["hotprospector_member_daily"].find(
            {"user_id": user_id, "date": {"$gte": start, "$lte": store_end}},
            {"agentId": 1, "agentName": 1, "agentEmail": 1, "metrics": 1},
        )
        async for doc in cur:
            aid = str(doc.get("agentId") or "")
            if not aid:
                continue
            _accumulate(_bucket(aid, doc.get("agentName"), doc.get("agentEmail"))["sums"], doc.get("metrics") or {})

    # Live days (yesterday + today) when they fall in the range.
    for live_day in {(today - timedelta(days=1)).isoformat(), today.isoformat()}:
        if start <= live_day <= end:
            ok, dash = await integration.get_member_dashboard_data(live_day)
            if ok:
                for a in dash.get("results", []):
                    aid = str(a.get("agentId") or "")
                    if not aid:
                        continue
                    _accumulate(_bucket(aid, a.get("agentName"), a.get("agentEmail"))["sums"], a)

    return [_format(aid, info["name"], info["email"], info["sums"]) for aid, info in agg.items()]
