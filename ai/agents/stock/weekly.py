"""
ai/agents/stock/weekly.py
-------------------------
The stock agent's weekly report (spec section 10): the last full Monday to Sunday against the week before, built
by code from each client's own numbers and posted to Slack.

Who goes where. A client in the red zone is under "Decreased / still in red", even if it improved. Everyone else with
a zone (green or yellow) is under "Improved / maintained", even if leads fell or it slipped a zone. Clients marked
`on_hold` are left out of everything. A client with no zones or whose ad data hasn't synced through the week is not
judged on part data: it is counted, and the comparison says so.

Status check compares the accounts that were judged last week, this week against last: the same set of accounts on both
sides, so a client added this week can't make the picture look worse. Wins are the three biggest CPL drops, in code.

Only numbers and client names reach the message. Card notes, follow-ups, alerts and Meta errors are never read here.
Money stays in each account's own currency, so the comparison is one line per currency.

Posting. `post_weekly` claims the message on the run (`weekly.claimed_at`) before it calls Slack and saves the `ts`
straight after, so a double press, or a publish retried after an outage, posts it once. A failed post releases the
claim. A claim older than CLAIM_STALE is a killed invocation's, and is taken over (a kill between Slack's answer and
the save can post that one message twice). A preview never posts.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from ai.agents.stock.alerts import ACCOUNT_TZ_FIELD
from ai.agents.stock.checks import client_facts, zone
from ai.agents.stock.settings import DEFAULT_SETTINGS, Zones, merge_settings
from ai.agents.stock.slack import PREVIEW, _call, _connection, _context, _deliver, _failure, _note_api_error
from ai.agents.stock.templates import _day, assemble, render_kind_safe
from ai.agents.stock.windows import Window, _tz
from ai.agents.stock.writer import _fmt_money
from ai.agents.store import RUNS
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

CLAIM_STALE = timedelta(minutes=2)    # a post is one Slack call; an older claim belongs to a killed invocation
MAX_WINS = 3
MEDALS = ("🥇", "💪", "🚀")
JUDGED = ("green", "yellow", "red")
ZONE_DOTS = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
NOT_THERE = "No clients"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def last_full_week(now_utc: datetime, tz_name: str | None) -> tuple[date, date, date, date]:
    """(start, end, prev_start, prev_end): the newest Monday to Sunday that has ended in the timezone, and the week
    before it. On a Monday morning that is the week that ended yesterday; on a Sunday it is the one before."""
    today = now_utc.astimezone(_tz(tz_name)).date()
    end = today - timedelta(days=(today.weekday() + 1) % 7 or 7)
    start = end - timedelta(days=6)
    return start, end, start - timedelta(days=7), start - timedelta(days=1)


def weekly_due(settings: dict, run: dict) -> bool:
    """A real run on the weekly weekday, with the report on and none built yet. `kind` doesn't matter: an alert-only
    day still gets it."""
    cfg = settings.get("weekly") or {}
    if run.get("kind") == PREVIEW or run.get("weekly") or not cfg.get("enabled") or not run.get("local_date"):
        return False
    try:
        return date.fromisoformat(run["local_date"]).weekday() == cfg.get("weekday")
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _pct(prev: float | None, cur: float | None) -> float | None:
    return (cur - prev) / prev * 100 if prev and cur is not None else None


def _zone_for(spend, leads, zones: dict | None) -> str:
    return zone(spend, leads, Zones(zones["green_under"], zones["red_over"])) if zones else "no_zones"


def _entry(facts: dict) -> dict:
    return {"client_group_id": facts["client_group_id"], "name": facts["name"], "currency": facts["currency"],
            "zone": facts["zone"], "prev_zone": facts["prev_zone"], "cpl": facts["cpl"], "prev_cpl": facts["prev_cpl"],
            "leads": facts["leads"], "prev_leads": facts["prev_leads"], "spend": facts["spend"],
            "prev_spend": facts["prev_spend"]}


def _cpl_text(entry: dict, key: str) -> str:
    v = entry.get(key)
    return "no leads" if v is None else _fmt_money(v, entry["currency"])


def _line(entry: dict) -> str:
    bits = [f"• {entry['name']}: CPL {_cpl_text(entry, 'prev_cpl')} → {_cpl_text(entry, 'cpl')}"]
    change = _pct(entry["prev_cpl"], entry["cpl"])
    if change is not None and round(change) != 0:
        bits.append(f"({'down' if change < 0 else 'up'} {abs(change):.0f}%)")
    bits.append(f"· leads {entry['prev_leads']:g} → {entry['leads']:g}")
    if entry["zone"] != entry["prev_zone"] and entry["prev_zone"] in ZONE_DOTS:
        bits.append(f"· {ZONE_DOTS[entry['prev_zone']]} → {ZONE_DOTS[entry['zone']]}")
    return " ".join(bits)


def _win_line(medal: str, entry: dict) -> str:
    drop = -_pct(entry["prev_cpl"], entry["cpl"])
    return (f"{medal} {entry['name']}: CPL {_cpl_text(entry, 'prev_cpl')} → {_cpl_text(entry, 'cpl')}, "
            f"down {drop:.0f}% on {entry['leads']:g} leads")


def _wins(entries: list[dict]) -> list[dict]:
    drops = [(e, -_pct(e["prev_cpl"], e["cpl"])) for e in entries
             if e["cpl"] is not None and e["prev_cpl"] and e["cpl"] < e["prev_cpl"]]
    drops.sort(key=lambda d: (-d[1], d[0]["name"]))
    return [e for e, _ in drops[:MAX_WINS]]


def _comparison(entries: list[dict], not_judged: list[dict]) -> tuple[dict, str]:
    """Totals for the clients the report covers: leads (no currency), and spend and CPL one line per currency."""
    leads, prev_leads = sum(e["leads"] for e in entries), sum(e["prev_leads"] for e in entries)
    by_cur: dict[str, dict] = {}
    for e in entries:
        t = by_cur.setdefault(e["currency"] or "", {"spend": 0.0, "prev_spend": 0.0, "leads": 0.0, "prev_leads": 0.0})
        for k in t:
            t[k] += e[k]
    lines = []
    lead_change = _pct(prev_leads, leads)
    lines.append(f"Leads: {leads:g} vs {prev_leads:g}" + (f" ({lead_change:+.0f}%)" if lead_change is not None else ""))
    for cur, t in sorted(by_cur.items()):
        label = f" ({cur})" if len(by_cur) > 1 and cur else ""
        cpl_now = t["spend"] / t["leads"] if t["leads"] else None
        cpl_prev = t["prev_spend"] / t["prev_leads"] if t["prev_leads"] else None
        lines.append(f"Spend{label}: {_fmt_money(t['spend'], cur)} vs {_fmt_money(t['prev_spend'], cur)}")
        lines.append(f"CPL{label}: {_fmt_money(cpl_now, cur) if cpl_now is not None else 'no leads'} vs "
                     f"{_fmt_money(cpl_prev, cur) if cpl_prev is not None else 'no leads'}")
    if not entries:
        lines = ["No clients had data for this week."]
    if not_judged:
        n = len(not_judged)
        lines.append(f"Not included: {n} client{'s' if n != 1 else ''} without zones or with ad data still syncing.")
    return ({"leads": leads, "prev_leads": prev_leads,
             "currencies": {cur: t for cur, t in sorted(by_cur.items())}}, "\n".join(lines))


def _status_check(entries: list[dict]) -> tuple[dict, str]:
    """This week against last, for the accounts that were judged last week."""
    base = [e for e in entries if e["prev_zone"] in JUDGED]
    last = {z: sum(1 for e in base if e["prev_zone"] == z) for z in JUDGED}
    now = {z: sum(1 for e in base if e["zone"] == z) for z in JUDGED}
    stopped = sum(1 for e in base if e["zone"] not in JUDGED)
    check = {"accounts": len(base), "last_week": last, "this_week": now, "stopped": stopped}
    if not base:
        return check, "No accounts were judged last week, so there is nothing to compare."
    lines = [f"Same {len(base)} account{'s' if len(base) != 1 else ''} as last week"]
    lines += [f"{ZONE_DOTS[z]} {z.title()}: {last[z]} → {now[z]}" for z in JUDGED]
    if stopped:
        lines.append(f"⚪ Not spending this week: {stopped}")
    return check, "\n".join(lines)


async def build_weekly(db, agent: dict, now_utc: datetime) -> dict:
    """The week's report from the stored ad rows. Never calls Meta or an AI. Safe to build again."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    agent_tz = ((agent.get("scope") or {}).get("schedule") or {}).get("tz")
    week = last_full_week(now_utc, agent_tz)
    entries, not_judged = [], []
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        if (settings["clients"].get(gid) or {}).get("on_hold"):
            continue
        group = await db["client_groups"].find_one({"id": gid, "user_id": agent["user_id"]})
        if group is None:
            continue
        facts = await client_facts(db, agent, group, Window(*last_full_week(now_utc, group.get(ACCOUNT_TZ_FIELD))))
        if facts["decision"] in ("no_zones", "no_history", "data_late") or facts["zones"] is None:
            not_judged.append({"client_group_id": gid, "name": facts["name"], "reason": facts["decision"]})
            continue
        facts["prev_zone"] = _zone_for(facts["prev_spend"], facts["prev_leads"], facts["zones"])
        entries.append(_entry(facts))

    improved = [e for e in entries if e["zone"] in ("green", "yellow")]
    decreased = [e for e in entries if e["zone"] == "red"]
    wins = _wins(entries)
    compare, comparison = _comparison(entries, not_judged)
    check, status_text = _status_check(entries)
    label = f"{_day(week[0])} to {_day(week[1])}"
    values = {"week": label, "comparison": comparison,
              "improved": "\n".join(_line(e) for e in improved) or NOT_THERE,
              "decreased": "\n".join(_line(e) for e in decreased) or NOT_THERE,
              "status_check": status_text,
              "wins": "\n".join(_win_line(m, e) for m, e in zip(MEDALS, wins)) or "No wins this week"}
    return {"week": {"start": week[0].isoformat(), "end": week[1].isoformat(),
                     "prev_start": week[2].isoformat(), "prev_end": week[3].isoformat()},
            "improved": improved, "decreased": decreased, "wins": wins, "comparison": compare, "status_check": check,
            "not_judged": not_judged, "values": values, "built_at": _now()}


def weekly_view(run: dict) -> dict | None:
    """What the dashboard gets: the report and whether it is out, never the Slack channel or the claim."""
    w = run.get("weekly")
    if not w:
        return None
    keep = ("week", "improved", "decreased", "wins", "comparison", "status_check", "not_judged", "values", "built_at",
            "posted_by", "posted_at", "post_error")
    return {**{k: w[k] for k in keep if k in w}, "posted": bool((w.get("slack") or {}).get("ts"))}


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def _claim(db, run_id) -> dict | None:
    """The run's weekly, claimed for this post, or None when it is already out or someone else is posting it."""
    now = _now()
    return await db[RUNS].find_one_and_update(
        {"_id": run_id, "kind": {"$ne": PREVIEW}, "weekly": {"$type": "object"}, "weekly.slack.ts": None,
         "$or": [{"weekly.claimed_at": None}, {"weekly.claimed_at": {"$lte": now - CLAIM_STALE}}]},
        {"$set": {"weekly.claimed_at": now}})


async def _release(db, run_id, **fields) -> None:
    await db[RUNS].update_one({"_id": run_id}, {"$set": {"weekly.claimed_at": None, **{f"weekly.{k}": v for k, v in fields.items()}}})


async def post_weekly(db, run: dict, agent: dict, *, by: str) -> dict:
    """Post the run's weekly message once. {"outcome": posted | already_posted | in_progress | preview | not_built}.
    Raises SlackPublishError when Slack didn't take it; the claim is released, so a retry posts it."""
    current = await db[RUNS].find_one({"_id": run["_id"]}, {"weekly": 1, "kind": 1})
    if current is None or not current.get("weekly"):
        return {"outcome": "not_built"}
    if current.get("kind") == PREVIEW or run.get("kind") == PREVIEW:
        return {"outcome": "preview"}
    if (current["weekly"].get("slack") or {}).get("ts"):
        return {"outcome": "already_posted", "ts": current["weekly"]["slack"]["ts"]}
    claimed = await _claim(db, run["_id"])
    if claimed is None:
        again = (await db[RUNS].find_one({"_id": run["_id"]}, {"weekly": 1})) or {}
        ts = ((again.get("weekly") or {}).get("slack") or {}).get("ts")
        return {"outcome": "already_posted", "ts": ts} if ts else {"outcome": "in_progress"}
    weekly = claimed["weekly"]
    try:
        client, channel = await _connection(db, run, agent)
        values = weekly["values"]

        def render(template):
            blocks, text, fell_back = render_kind_safe("weekly", template, values, None, _context("weekly", agent))
            return assemble(blocks, None), text, fell_back

        async def send(blocks, text):
            return await _call(client.chat_postMessage, channel=channel, blocks=blocks, text=text)

        resp = await _deliver(db, agent, "weekly", render, send)
    except SlackApiError as e:
        code = await _note_api_error(db, run["user_id"], e)
        failure = _failure(e, code)
        await _release(db, run["_id"], post_error=str(failure)[:300])
        raise failure from e
    except Exception as e:  # noqa: BLE001 - release the claim whatever went wrong, then let the caller see it
        await _release(db, run["_id"], post_error=str(e)[:300])
        raise
    ts = resp.get("ts")
    await _release(db, run["_id"], slack={"channel_id": channel, "ts": ts}, posted_by=by, posted_at=_now(), post_error=None)
    return {"outcome": "posted", "ts": ts}


async def post_if_automatic(db, run: dict, agent: dict) -> None:
    """At the end of a publish: post the weekly when the agent is set to. A Slack failure raises SlackPublishError, so
    the run's own late-post retry covers it, and post_weekly makes sure it never goes out twice."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    if (settings.get("weekly") or {}).get("auto_post") and run.get("weekly"):
        await post_weekly(db, run, agent, by="auto")
