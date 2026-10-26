"""
ai/agents/outcomes.py
---------------------
What a BirdyHub agent's actions led to, for the Activity tab.

  compute_outcome  the 7-day follow-up on one applied agent action:
                     pause  -> the client's account CPL in the 7 days before the
                               action day vs the 7 full days after it
                               {metric: "account_cpl", before, after}
                     revive -> the revived ad(s)' leads and CPL in the 7 full days
                               after the action day {metric: "ad_cpl", after, leads}
                   None when it is too early (the 7th day after has not finished)
                   or there is not enough data (no spend, or no leads to divide by).
  run_outcomes     the daily `agents-outcomes` job: stores `outcome_7d` on every
                   applied agent row 7-30 days old that has none yet. A row whose
                   data is still missing is retried on later days, until 30 days.
  list_activity    the agent's decision feed (applied / denied / undone).
  compute_impact   the month's metric tabs: actions, auto, wasted spend stopped,
                   approval rate, and last month's action count.

Money is returned as plain numbers plus the ISO `currency` code of the client
(per row) or the most common one across the clients involved (agent-wide).
The frontend formats it; nothing here assumes a currency.

Days are UTC days, as insights are stored per `date_start`; the impact month is
the agent's local calendar month.
"""

import base64
import logging
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ai.agents.guardrails import DEFAULT_TZ, agent_tz
from ai.agents.snapshot import load_ad_days
from ai.suggestions import store as sstore
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from core.database import DB_NAME

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
MEASURE_UNTIL = timedelta(days=30)   # rows still without data after this are left alone
UNDO_AUTO_WINDOW = timedelta(hours=24)

DECIDED_KINDS = (sstore.KIND_ACTION_APPLIED, sstore.KIND_SUGGESTION_DISMISSED, sstore.KIND_ACTION_UNDONE)
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_TITLE_PREFIXES = ("Dismissed: ", "Undid: ")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def iso_z(dt: datetime | None) -> str | None:
    """Naive-UTC datetime -> '2026-10-26T07:00:00.000Z' (fixed width, sorts as text)."""
    if not isinstance(dt, datetime):
        return None
    return _naive_utc(dt).isoformat(timespec="milliseconds") + "Z"


def parse_cursor(value: str) -> datetime:
    """An `at` value (ISO, with or without Z / offset) -> naive UTC. ValueError if not ISO."""
    dt = datetime.fromisoformat(value.strip())
    return _naive_utc(dt)


def _targets(sug: dict | None) -> list[dict]:
    """What the action touched: applied_targets while applied, else the action's targets
    (an undo clears applied_targets)."""
    sug = sug or {}
    return list(sug.get("applied_targets") or (sug.get("action") or {}).get("targets") or [])


_FIELD = {"ad": "ad_id", "adset": "adset_id", "campaign": "campaign_id"}


def _matches(day, target: dict) -> bool:
    field = _FIELD.get(target.get("object_type") or "ad", "ad_id")
    return str(getattr(day, field)) == str(target.get("object_id"))


def _sum(days_by_ad: dict, start: date, end: date, targets: list[dict] | None = None) -> tuple[float, int]:
    """(spend, leads) over [start, end], for every ad or only those matching `targets`."""
    spend, leads = 0.0, 0
    for days in days_by_ad.values():
        for d in days:
            if not (start <= d.day <= end):
                continue
            if targets is not None and not any(_matches(d, t) for t in targets):
                continue
            spend += d.spend
            leads += d.leads
    return spend, leads


async def _currencies(db, user_id: str, gids) -> dict[str, str | None]:
    gids = [g for g in set(gids) if g]
    if not gids:
        return {}
    out = {}
    async for g in db["client_groups"].find({"id": {"$in": gids}, "user_id": user_id},
                                            projection={"id": 1, "ad_account_currency": 1}):
        cur = g.get("ad_account_currency")
        out[g["id"]] = cur.upper() if isinstance(cur, str) and cur else None
    return out


def _majority(currencies) -> str | None:
    counts = Counter(c for c in currencies if c)
    return counts.most_common(1)[0][0] if counts else None


# ---------------------------------------------------------------------------
# 7-day outcome
# ---------------------------------------------------------------------------

async def compute_outcome(db, activity_doc: dict, *, today: date) -> dict | None:
    """The 7-day outcome of one applied agent action, or None (too early / not enough data
    / not a pause or revive that changed anything)."""
    atype = activity_doc.get("applied_action_type")
    gid = activity_doc.get("client_group_id")
    at = activity_doc.get("created_at")
    if atype not in (ACTION_PAUSE_ADS, ACTION_ENABLE_ADS) or not gid or not isinstance(at, datetime):
        return None
    action_day = _naive_utc(at).date()
    after_start = action_day + timedelta(days=1)
    after_end = action_day + timedelta(days=WINDOW_DAYS)
    # Complete and fresh: one day's buffer after the 7th day (insights are dated in the
    # ad account's timezone), and the client's Meta data refreshed after d+7 ended.
    if today < action_day + timedelta(days=WINDOW_DAYS + 2):
        return None

    user_id = activity_doc.get("user_id")
    group = await db["client_groups"].find_one(
        {"id": gid, "user_id": user_id}, projection={"ad_account_currency": 1, "last_meta_refresh": 1}) or {}
    refreshed = group.get("last_meta_refresh")
    if not isinstance(refreshed, datetime) or _naive_utc(refreshed).date() < action_day + timedelta(days=WINDOW_DAYS + 1):
        return None
    cur = group.get("ad_account_currency")
    currency = cur.upper() if isinstance(cur, str) and cur else None

    if atype == ACTION_PAUSE_ADS:
        before_start = action_day - timedelta(days=WINDOW_DAYS)
        before_end = action_day - timedelta(days=1)
        days_by_ad = await load_ad_days(db, user_id, gid, before_start, after_end)
        b_spend, b_leads = _sum(days_by_ad, before_start, before_end)
        a_spend, a_leads = _sum(days_by_ad, after_start, after_end)
        if b_spend <= 0 or a_spend <= 0 or b_leads <= 0 or a_leads <= 0:
            return None
        return {"metric": "account_cpl", "before": round(b_spend / b_leads, 2),
                "after": round(a_spend / a_leads, 2), "currency": currency,
                "before_spend": round(b_spend, 2), "before_leads": b_leads,
                "after_spend": round(a_spend, 2), "after_leads": a_leads}

    sug = await db[sstore.SUGGESTIONS].find_one({"_id": activity_doc.get("ref_id"), "user_id": user_id})
    targets = _targets(sug)
    if not targets:
        return None
    days_by_ad = await load_ad_days(db, user_id, gid, after_start, after_end)
    spend, leads = _sum(days_by_ad, after_start, after_end, targets)
    if spend <= 0:
        return None
    return {"metric": "ad_cpl", "after": round(spend / leads, 2) if leads else None,
            "leads": leads, "spend": round(spend, 2), "currency": currency}


async def run_outcomes_db(db, *, now_utc: datetime) -> dict:
    now = _naive_utc(now_utc)
    today = now.date()
    q = {
        "agent_id": {"$exists": True, "$ne": None},
        "kind": sstore.KIND_ACTION_APPLIED,
        "applied_action_type": {"$in": [ACTION_PAUSE_ADS, ACTION_ENABLE_ADS]},
        "outcome_7d": {"$exists": False},
        "created_at": {"$gte": now - MEASURE_UNTIL, "$lte": now - timedelta(days=WINDOW_DAYS + 1)},
    }
    out = {"checked": 0, "measured": 0, "errors": 0}
    async for row in db[sstore.ACTIVITY].find(q):
        out["checked"] += 1
        try:
            res = await compute_outcome(db, row, today=today)
            if res is not None:
                await db[sstore.ACTIVITY].update_one(
                    {"_id": row["_id"]}, {"$set": {"outcome_7d": res, "outcome_at": now}})
                out["measured"] += 1
        except Exception as e:
            logger.error("agents-outcomes: row %s failed: %s", row.get("_id"), e, exc_info=True)
            out["errors"] += 1
    return out


async def run_outcomes(mongo_client, now_utc: datetime) -> dict:
    out = await run_outcomes_db(mongo_client[DB_NAME], now_utc=now_utc)
    logger.info("[agents-outcomes] %s", out)
    return out


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------

def _decided(row: dict) -> str | None:
    kind = row.get("kind")
    if kind == sstore.KIND_ACTION_APPLIED:
        return "auto" if row.get("actor") == sstore.ACTOR_BIRDY else "approved"
    if kind == sstore.KIND_SUGGESTION_DISMISSED:
        return "denied"
    if kind == sstore.KIND_ACTION_UNDONE:
        return "undone"
    return None


def _title(row: dict) -> str:
    title = row.get("title") or ""
    for p in _TITLE_PREFIXES:
        if title.startswith(p):
            return title[len(p):]
    return title


def can_undo(row: dict, sug: dict | None, now: datetime) -> bool:
    """Same test as POST /undo-auto: Birdy's own apply, still applied, under 24 hours."""
    if row.get("kind") != sstore.KIND_ACTION_APPLIED or row.get("actor") != sstore.ACTOR_BIRDY:
        return False
    if not sug or sug.get("status") != sstore.STATUS_APPLIED:
        return False
    if (sug.get("decided_by") or {}).get("actor") != sstore.ACTOR_BIRDY:
        return False
    applied_at = sug.get("applied_at")
    created = row.get("created_at")
    if not isinstance(applied_at, datetime) or not isinstance(created, datetime):
        return False
    # The row must be the apply the suggestion is currently in (not an earlier one).
    if abs(applied_at - created) > timedelta(minutes=5):
        return False
    return now - applied_at <= UNDO_AUTO_WINDOW


def encode_cursor(row: dict) -> str:
    """Opaque page cursor: the row's exact (created_at, _id)."""
    raw = f"{_naive_utc(row['created_at']).isoformat()}|{row['_id']}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    """ValueError if it is not a cursor from encode_cursor."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        at, _id = raw.split("|", 1)
        return _naive_utc(datetime.fromisoformat(at)), _id
    except Exception as e:
        raise ValueError("bad cursor") from e


async def list_activity(db, agent: dict, *, limit: int = 50, before: datetime | None = None,
                        cursor: tuple[datetime, str] | None = None,
                        now: datetime | None = None) -> dict:
    """Newest first, ordered by (created_at, _id). `cursor` (from next_cursor) pages without
    skipping rows that share a timestamp; `before` (an ISO `at`) is kept for older callers."""
    now = now or datetime.utcnow()
    q: dict = {"user_id": agent["user_id"], "agent_id": agent["_id"], "kind": {"$in": list(DECIDED_KINDS)}}
    if cursor is not None:
        at, _id = cursor
        q["$or"] = [{"created_at": {"$lt": at}}, {"created_at": at, "_id": {"$lt": _id}}]
    elif before is not None:
        q["created_at"] = {"$lt": before}
    rows = await (db[sstore.ACTIVITY].find(q).sort([("created_at", -1), ("_id", -1)])
                  .limit(limit).to_list(length=limit))

    sids = list({r.get("ref_id") for r in rows if r.get("ref_id")})
    sugs = {}
    if sids:
        async for s in db[sstore.SUGGESTIONS].find({"_id": {"$in": sids}, "user_id": agent["user_id"]}):
            sugs[s["_id"]] = s
    currencies = await _currencies(db, agent["user_id"], [r.get("client_group_id") for r in rows])

    items = []
    for r in rows:
        sug = sugs.get(r.get("ref_id"))
        item = {
            "id": r["_id"],
            "at": iso_z(r.get("created_at")),
            "title": _title(r),
            "client_name": r.get("client"),
            "client_group_id": r.get("client_group_id"),
            "suggestion_id": r.get("ref_id"),
            "rule_id": r.get("rule_id"),
            "action_type": r.get("applied_action_type") or ((sug or {}).get("action") or {}).get("type"),
            "decided": _decided(r),
            "can_undo": can_undo(r, sug, now),
            "currency": currencies.get(r.get("client_group_id")),
        }
        for k in ("reason_code", "note", "outcome_7d"):
            if r.get(k) is not None:
                item[k] = r[k]
        items.append(item)
    more = len(items) == limit and items
    return {"items": items,
            "next_before": items[-1]["at"] if more else None,
            "next_cursor": encode_cursor(rows[-1]) if more else None}


# ---------------------------------------------------------------------------
# Monthly impact
# ---------------------------------------------------------------------------

def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except Exception:
        return ZoneInfo(DEFAULT_TZ)


def _month_bounds(month: str, tz_name: str) -> tuple[date, date, datetime, datetime]:
    """(first day, first day of next month, both as naive-UTC instants of local midnight)."""
    y, m = int(month[:4]), int(month[5:7])
    first = date(y, m, 1)
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    tz = _zone(tz_name)

    def utc(d: date) -> datetime:
        return datetime.combine(d, time(0, 0), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)

    return first, nxt, utc(first), utc(nxt)


def _prev_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


async def _month_rows(db, agent: dict, start: datetime, end: datetime) -> list[dict]:
    q = {"user_id": agent["user_id"], "agent_id": agent["_id"], "kind": {"$in": list(DECIDED_KINDS)},
         "created_at": {"$gte": start, "$lt": end}}
    return await db[sstore.ACTIVITY].find(q).sort("created_at", 1).to_list(length=None)


def _local_date(dt: datetime, tz) -> date:
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).date()


async def _wasted_spend(db, agent: dict, pauses: list[dict], sugs: dict, *, month_end: datetime,
                        now: datetime, tz) -> tuple[float, dict]:
    """Sum over the month's pauses of (target avg daily spend in the 7 local days before
    the action day) x (days paused inside the month). Days paused run from the pause
    instant to the earliest of: its undo, a later agent revive of the same object, the
    end of the month, now. Fractional days count."""
    if not pauses:
        return 0.0, {}
    user_id, agent_id = agent["user_id"], agent["_id"]
    cap = min(month_end, now)

    # Later events that end a pause: undo rows and agent revives.
    first_at = min(p["created_at"] for p in pauses)
    enders = await db[sstore.ACTIVITY].find({
        "user_id": user_id, "agent_id": agent_id, "created_at": {"$gt": first_at},
        "$or": [{"kind": sstore.KIND_ACTION_UNDONE},
                {"kind": sstore.KIND_ACTION_APPLIED, "applied_action_type": ACTION_ENABLE_ADS}],
    }).sort("created_at", 1).to_list(length=None)
    revive_ids = [e.get("ref_id") for e in enders
                  if e.get("kind") == sstore.KIND_ACTION_APPLIED and e.get("ref_id") not in sugs]
    if revive_ids:
        async for s in db[sstore.SUGGESTIONS].find({"_id": {"$in": revive_ids}, "user_id": user_id}):
            sugs[s["_id"]] = s

    currencies = await _currencies(db, user_id, [p.get("client_group_id") for p in pauses])
    cache: dict = {}
    total, by_currency = 0.0, {}
    for p in pauses:
        gid = p.get("client_group_id")
        targets = _targets(sugs.get(p.get("ref_id")))
        if not gid or not targets:
            continue
        start = p["created_at"]
        local_day = _local_date(start, tz)
        for t in targets:
            end = cap
            for e in enders:
                if e["created_at"] <= start:
                    continue
                if e.get("kind") == sstore.KIND_ACTION_UNDONE:
                    hit = e.get("ref_id") == p.get("ref_id")
                else:
                    hit = any(str(x.get("object_id")) == str(t.get("object_id"))
                              for x in _targets(sugs.get(e.get("ref_id"))))
                if hit:
                    end = min(end, e["created_at"])
                    break
            days_paused = (end - start).total_seconds() / 86400
            if days_paused <= 0:
                continue
            b_start, b_end = local_day - timedelta(days=WINDOW_DAYS), local_day - timedelta(days=1)
            key = (gid, b_start)
            if key not in cache:
                cache[key] = await load_ad_days(db, user_id, gid, b_start, b_end)
            spend, _ = _sum(cache[key], b_start, b_end, [t])
            value = spend / WINDOW_DAYS * days_paused
            total += value
            cur = currencies.get(gid) or "unknown"
            by_currency[cur] = by_currency.get(cur, 0.0) + value
    return total, by_currency


async def _reversed_applies(db, agent: dict, applies: list[dict]) -> set:
    """_ids of the given apply rows that a later undo reversed: the next apply/undo row
    of the same suggestion is an undo. One query for all of them."""
    refs = list({a.get("ref_id") for a in applies if a.get("ref_id")})
    if not refs:
        return set()
    events = await db[sstore.ACTIVITY].find({
        "user_id": agent["user_id"], "agent_id": agent["_id"], "ref_id": {"$in": refs},
        "kind": {"$in": [sstore.KIND_ACTION_APPLIED, sstore.KIND_ACTION_UNDONE]},
        "created_at": {"$gte": min(a["created_at"] for a in applies)},
    }).sort([("created_at", 1), ("_id", 1)]).to_list(length=None)
    by_ref: dict = {}
    for e in events:
        by_ref.setdefault(e.get("ref_id"), []).append(e)
    out = set()
    for evs in by_ref.values():
        for cur, nxt in zip(evs, evs[1:]):
            if cur.get("kind") == sstore.KIND_ACTION_APPLIED and nxt.get("kind") == sstore.KIND_ACTION_UNDONE:
                out.add(cur["_id"])
    return out


async def _month_figures(db, agent: dict, month: str, now: datetime, *, with_spend: bool) -> dict:
    tz_name = agent_tz(agent)
    first, nxt, start, end = _month_bounds(month, tz_name)
    rows = await _month_rows(db, agent, start, end)
    sids = list({r.get("ref_id") for r in rows if r.get("ref_id")})
    sugs = {}
    if sids:
        async for s in db[sstore.SUGGESTIONS].find({"_id": {"$in": sids}, "user_id": agent["user_id"]}):
            sugs[s["_id"]] = s

    def has_action(r):
        return bool((sugs.get(r.get("ref_id")) or {}).get("action"))

    applied = [r for r in rows if r.get("kind") == sstore.KIND_ACTION_APPLIED]
    actions = [r for r in applied if r.get("applied_action_type")]
    auto = [r for r in actions if r.get("actor") == sstore.ACTOR_BIRDY]

    # Each user approval in the month is one decision, as in confidence.py: an approval
    # the user later undid is undone_user (a failure), not also an approval.
    user_applies = [r for r in applied if r.get("actor") == sstore.ACTOR_USER and has_action(r)]
    reversed_ids = await _reversed_applies(db, agent, user_applies)
    undone_user = sum(1 for r in user_applies if r["_id"] in reversed_ids)
    approved = len(user_applies) - undone_user
    # A repeat dismissal of the same item (Slack + dashboard) is one denial.
    denied = len({r.get("ref_id") for r in rows
                  if r.get("kind") == sstore.KIND_SUGGESTION_DISMISSED and has_action(r)})
    denom = approved + denied + undone_user

    out = {"actions": len(actions), "auto": len(auto),
           "approval_rate": approved / denom if denom else None,
           "approved": approved, "denied": denied, "undone_user": undone_user}
    if with_spend:
        pauses = [r for r in actions if r.get("applied_action_type") == ACTION_PAUSE_ADS]
        total, by_cur = await _wasted_spend(db, agent, pauses, sugs, month_end=end, now=now,
                                            tz=_zone(tz_name))
        out["wasted_spend_stopped"] = round(total, 2)
        out["wasted_spend_by_currency"] = {k: round(v, 2) for k, v in by_cur.items()}
        # Majority currency: of the paused clients, else of this month's rows, else of scope.
        gids = ([p.get("client_group_id") for p in pauses]
                or [r.get("client_group_id") for r in rows]
                or list((agent.get("scope") or {}).get("client_group_ids") or []))
        cur_by_gid = await _currencies(db, agent["user_id"], gids)
        out["currency"] = _majority(cur_by_gid.get(g) for g in gids)
    return out


def current_month(agent: dict, now: datetime | None = None) -> str:
    """This month (YYYY-MM) in the agent's timezone."""
    now = _naive_utc(now or datetime.utcnow())
    return _local_date(now, _zone(agent_tz(agent))).strftime("%Y-%m")


async def compute_impact(db, agent: dict, *, month: str, now: datetime | None = None) -> dict:
    """Impact for one local calendar month (YYYY-MM), measured up to `now` (default: now)."""
    if not MONTH_RE.match(month or ""):
        raise ValueError("month must be YYYY-MM")
    now = _naive_utc(now or datetime.utcnow())
    cur = await _month_figures(db, agent, month, now, with_spend=True)
    prev = await _month_figures(db, agent, _prev_month(month), now, with_spend=False)
    return {
        "month": month,
        "actions": cur["actions"],
        "auto": cur["auto"],
        "wasted_spend_stopped": cur["wasted_spend_stopped"],
        "wasted_spend_by_currency": cur["wasted_spend_by_currency"],
        "currency": cur["currency"],
        "approval_rate": cur["approval_rate"],
        "approval_counts": {"approved": cur["approved"], "denied": cur["denied"],
                            "undone_user": cur["undone_user"]},
        "vs_prev_month": {"month": _prev_month(month), "actions": prev["actions"],
                          "auto": prev["auto"], "approval_rate": prev["approval_rate"]},
    }
