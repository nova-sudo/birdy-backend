"""
ai/agents/stock/weekly.py
-------------------------
The stock agent's weekly report (spec section 10): the last full Monday to Sunday against the week before, built
by code from each client's own numbers and posted to Slack.

One agency week. The dates come from the agency's timezone (`windows.agency_tz`, the runner's default when none is
set) and are the same for every client. Each client's rows are its own account-local days (spec section 6), so a
row belongs to the day it was spent on in that account. A client whose last day of the week hasn't ended in its own
timezone yet is judged on what it has and carries a note; one whose day is over but whose data hasn't synced is not
judged on part data.

Who goes where. A client in the red zone is under "Decreased / still in red", even if it improved. Everyone else with
a zone (green or yellow) is under "Improved / maintained", even if leads fell or it slipped a zone. Clients marked
`on_hold` are left out of everything. Each account shows Leads, CPL and Ad Spend, last week to this, and a red account
gets one code-written reason. A client with no zones, no ad history or a missing group is counted in `not_judged`.

Status check compares the accounts in last week's stored weekly (`run.weekly.zones`, found by its week's end), this
week against last. It names the accounts that changed zone, stopped spending, or are no longer reported (on hold,
removed, not judged). With no weekly stored for the week before, it says this is the first week and compares nothing.
Wins are the three biggest CPL drops, in code.

Only numbers and client names are read. Card notes, follow-ups, alerts and Meta errors never are. Money stays in each
account's own currency, so the comparison is one line per currency.

Building. The runner builds it at the end of a run on the weekday. A failed build is recorded on the run and
`retry_weeklies` (every agents tick) builds it again and posts it when `auto_post` is on, within a day. An unposted
weekly is rebuilt just before it is posted, so a late client's rows are in; a posted one is never touched.

Posting. `post_weekly` claims the message on the run (`weekly.claimed_at` and a `claim_token`) before it calls Slack, and
records the post straight after, so a double press, or a publish retried after an outage, posts it once. A failed post
releases the claim, and only its own: a slow poster can't free the claim of one that took over. A claim older than
CLAIM_STALE is a killed invocation's and is taken over (a kill between Slack's answer and the save can post that one
message twice). Slack answering without a `ts` still counts as posted. A preview never posts.
"""

import logging
import time
import uuid
from datetime import date, datetime, timedelta, timezone

from slack_sdk.errors import SlackApiError

from ai.agents.stock.alerts import ACCOUNT_TZ_FIELD
from ai.agents.stock.checks import client_facts, zone
from ai.agents.stock.settings import DEFAULT_SETTINGS, Zones, merge_settings
from ai.agents.stock.slack import PREVIEW, _call, _connection, _context, _deliver, _failure, _note_api_error
from ai.agents.stock.templates import _day, assemble, render_kind_safe
from ai.agents.stock.windows import Window, _tz, agency_tz
from ai.agents.stock.writer import _fmt_money
from ai.agents.store import AGENTS, RUNS

logger = logging.getLogger(__name__)

BUILD_RETRY = timedelta(minutes=15)   # a failed build, or an automatic post that didn't go, is tried again after this
BUILD_GIVE_UP = timedelta(hours=24)
RETRY_BATCH = 20
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


INCOMPLETE = "Data may be incomplete: this account's last day of the week hasn't finished yet."


def _entry(facts: dict, note: str | None) -> dict:
    return {"client_group_id": facts["client_group_id"], "name": facts["name"], "currency": facts["currency"],
            "zone": facts["zone"], "prev_zone": facts["prev_zone"], "cpl": facts["cpl"], "prev_cpl": facts["prev_cpl"],
            "leads": facts["leads"], "prev_leads": facts["prev_leads"], "spend": facts["spend"],
            "prev_spend": facts["prev_spend"], "red_over": (facts["zones"] or {}).get("red_over"), "note": note}


def _cpl_text(entry: dict, key: str) -> str:
    v = entry.get(key)
    return "no leads" if v is None else _fmt_money(v, entry["currency"])


def _reason(entry: dict) -> str | None:
    """One short line on why a red account is red, from its numbers alone. None when nothing is clear."""
    if entry["cpl"] is None:
        return f"Spent {_fmt_money(entry['spend'], entry['currency'])} and got no leads." if entry["spend"] else None
    change = _pct(entry["prev_cpl"], entry["cpl"])
    if change is None:
        return None
    if round(change) > 0:
        return f"CPL up {change:.0f}% on last week."
    if round(change) < 0 and entry.get("red_over") is not None:
        return (f"CPL improved {abs(change):.0f}% but is still above the red limit of "
                f"{_fmt_money(entry['red_over'], entry['currency'])}.")
    return None


def _block(entry: dict, icon: str, reason: bool) -> str:
    lines = [f"{icon} {entry['name']}",
             f"Leads: {entry['prev_leads']:g} → {entry['leads']:g}",
             f"CPL: {_cpl_text(entry, 'prev_cpl')} → {_cpl_text(entry, 'cpl')}",
             f"Ad Spend: {_fmt_money(entry['prev_spend'], entry['currency'])} → {_fmt_money(entry['spend'], entry['currency'])}"]
    if reason and entry.get("reason"):
        lines.append(entry["reason"])
    if entry.get("note"):
        lines.append(entry["note"])
    return "\n".join(lines)


def _win_block(medal: str, entry: dict) -> str:
    drop = -_pct(entry["prev_cpl"], entry["cpl"])
    return (f"{medal} {entry['name']}\nCPL {_cpl_text(entry, 'prev_cpl')} → {_cpl_text(entry, 'cpl')}, down {drop:.0f}%\n"
            f"Leads {entry['prev_leads']:g} → {entry['leads']:g}")


def _wins(entries: list[dict]) -> list[dict]:
    drops = [(e, -_pct(e["prev_cpl"], e["cpl"])) for e in entries
             if e["cpl"] is not None and e["prev_cpl"] and e["cpl"] < e["prev_cpl"]]
    drops.sort(key=lambda d: (-d[1], d[0]["name"]))
    return [e for e, _ in drops[:MAX_WINS]]


def _comparison(week: tuple, entries: list[dict], not_judged: list[dict]) -> tuple[dict, str]:
    """Totals for the clients the report covers: leads (no currency), and spend and CPL one line per currency."""
    leads, prev_leads = sum(e["leads"] for e in entries), sum(e["prev_leads"] for e in entries)
    by_cur: dict[str, dict] = {}
    for e in entries:
        t = by_cur.setdefault(e["currency"] or "", {"spend": 0.0, "prev_spend": 0.0, "leads": 0.0, "prev_leads": 0.0})
        for k in t:
            t[k] += e[k]
    lines = [f"Comparison: {_day(week[2])} to {_day(week[3])} → {_day(week[0])} to {_day(week[1])}"]
    if entries:
        lead_change = _pct(prev_leads, leads)
        lines.append(f"Leads: {leads:g} vs {prev_leads:g}" + (f" ({lead_change:+.0f}%)" if lead_change is not None else ""))
        for cur, t in sorted(by_cur.items()):
            label = f" ({cur})" if len(by_cur) > 1 and cur else ""
            cpl_now = t["spend"] / t["leads"] if t["leads"] else None
            cpl_prev = t["prev_spend"] / t["prev_leads"] if t["prev_leads"] else None
            lines.append(f"Spend{label}: {_fmt_money(t['spend'], cur)} vs {_fmt_money(t['prev_spend'], cur)}")
            lines.append(f"CPL{label}: {_fmt_money(cpl_now, cur) if cpl_now is not None else 'no leads'} vs "
                         f"{_fmt_money(cpl_prev, cur) if cpl_prev is not None else 'no leads'}")
    else:
        lines.append("No clients had data for this week.")
    if not_judged:
        n = len(not_judged)
        lines.append(f"Not included: {n} client{'s' if n != 1 else ''} without zones or with ad data still syncing.")
    return ({"leads": leads, "prev_leads": prev_leads, "currencies": {cur: t for cur, t in sorted(by_cur.items())}},
            "\n".join(lines))


def _label(z: str) -> str:
    return f"{ZONE_DOTS[z]} {z.title()}"


def _listed(lines: list[str], limit: int = 10) -> list[str]:
    return [*lines[:limit], *([f"and {len(lines) - limit} more"] if len(lines) > limit else [])]


async def _last_weeks_zones(db, agent: dict, week: tuple) -> tuple[dict | None, bool]:
    """({gid: {name, zone}} from the weekly stored for the week before, or None, and whether this agent has any
    earlier weekly at all)."""
    base = {"agent_id": agent["_id"], "kind": {"$ne": PREVIEW}}
    row = await db[RUNS].find_one({**base, "weekly.week.end": week[3].isoformat(), "weekly.zones": {"$type": "object"}},
                                  sort=[("as_of", -1)])
    if row:
        return row["weekly"]["zones"], True
    earlier = await db[RUNS].find_one({**base, "weekly.week.end": {"$lt": week[0].isoformat()}}, {"_id": 1})
    return None, earlier is not None


def _status_check(prior: dict | None, had_earlier: bool, entries: list[dict]) -> tuple[dict, str]:
    """This week against last, for the accounts in last week's stored weekly. Names the ones that changed zone,
    stopped spending, or are no longer reported."""
    if prior is None:
        empty = {"accounts": 0, "last_week": {}, "this_week": {}, "changed": [], "stopped": [], "left_out": []}
        if had_earlier:
            return ({"first_week": False, **empty},
                    "Last week's report isn't available to compare, so there is no status comparison this week.")
        return {"first_week": True, **empty}, "This is the first weekly report, so there is nothing to compare yet."
    now_by = {e["client_group_id"]: e for e in entries}
    last = {z: 0 for z in JUDGED}
    this = {z: 0 for z in JUDGED}
    changed, stopped, left_out = [], [], []
    for gid, p in sorted(prior.items(), key=lambda kv: kv[1].get("name") or kv[0]):
        if p.get("zone") not in JUDGED:
            continue
        name = p.get("name") or gid
        e = now_by.get(gid)
        if e is None:
            left_out.append(name)
            continue
        last[p["zone"]] += 1
        if e["zone"] in JUDGED:
            this[e["zone"]] += 1
            if e["zone"] != p["zone"]:
                changed.append({"name": name, "from": p["zone"], "to": e["zone"]})
        else:
            stopped.append({"name": name, "was": p["zone"]})
    check = {"first_week": False, "accounts": sum(last.values()), "last_week": last, "this_week": this,
             "changed": changed, "stopped": stopped, "left_out": left_out}
    if not check["accounts"] and not left_out:
        return check, "No accounts were judged last week, so there is nothing to compare."
    lines = [f"{ZONE_DOTS[z]} {z.title()}: {last[z]} → {this[z]}" for z in JUDGED]
    if changed:
        lines += ["Changed zone:", *_listed([f"• {c['name']}: {_label(c['from'])} → {_label(c['to'])}" for c in changed])]
    if stopped:
        lines += ["Not spending this week:", *_listed([f"• {s['name']} (was {_label(s['was'])})" for s in stopped])]
    if left_out:
        lines += ["Not compared (on hold, removed or not judged this week):", *_listed([f"• {n}" for n in left_out])]
    return check, "\n".join(lines)


def _account_day_over(now_utc: datetime, tz_name: str | None, end: date) -> bool:
    return now_utc.astimezone(_tz(tz_name)).date() > end


async def build_weekly(db, agent: dict, now_utc: datetime, *, now: datetime | None = None) -> dict:
    """The week's report from the stored ad rows. Never calls Meta or an AI. Safe to build again. `now_utc` (the run's
    start) fixes the week; `now`, when given, is the real time of a rebuild, which decides whether an account's last
    day is over."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    week = last_full_week(now_utc, agency_tz(agent))
    window = Window(*week)
    prior, had_earlier = await _last_weeks_zones(db, agent, week)
    clock = now or now_utc
    entries, not_judged = [], []
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        if (settings["clients"].get(gid) or {}).get("on_hold"):
            continue
        group = await db["client_groups"].find_one({"id": gid, "user_id": agent["user_id"]})
        if group is None:
            not_judged.append({"client_group_id": gid, "name": gid, "reason": "missing"})
            continue
        facts = await client_facts(db, agent, group, window)
        decision, note = facts["decision"], None
        if decision == "data_late":
            # Judged on what it has only while the account's own last day is still going: the rest isn't missing, it
            # hasn't happened. Once that day is over, a gap means the data hasn't synced and can't be judged.
            if _account_day_over(clock, group.get(ACCOUNT_TZ_FIELD), week[1]):
                not_judged.append({"client_group_id": gid, "name": facts["name"], "reason": decision})
                continue
            note = INCOMPLETE
        elif decision in ("no_zones", "no_history") or facts["zones"] is None:
            not_judged.append({"client_group_id": gid, "name": facts["name"], "reason": decision})
            continue
        facts["zone"] = _zone_for(facts["spend"], facts["leads"], facts["zones"])
        facts["prev_zone"] = _zone_for(facts["prev_spend"], facts["prev_leads"], facts["zones"])
        entry = _entry(facts, note)
        entry["reason"] = _reason(entry) if entry["zone"] == "red" else None
        entries.append(entry)

    improved = [e for e in entries if e["zone"] in ("green", "yellow")]
    decreased = [e for e in entries if e["zone"] == "red"]
    wins = _wins(entries)
    compare, comparison = _comparison(week, entries, not_judged)
    check, status_text = _status_check(prior, had_earlier, entries)
    values = {"week": f"{_day(week[0])} to {_day(week[1])}", "comparison": comparison,
              "improved": "\n\n".join(_block(e, "🎯", False) for e in improved) or NOT_THERE,
              "decreased": "\n\n".join(_block(e, "⚠️", True) for e in decreased) or NOT_THERE,
              "status_check": status_text,
              "wins": "\n\n".join(_win_block(m, e) for m, e in zip(MEDALS, wins)) or "No wins this week"}
    return {"week": {"start": week[0].isoformat(), "end": week[1].isoformat(),
                     "prev_start": week[2].isoformat(), "prev_end": week[3].isoformat()},
            "improved": improved, "decreased": decreased, "wins": wins, "comparison": compare, "status_check": check,
            "not_judged": not_judged, "values": values, "built_at": _now(),
            "zones": {e["client_group_id"]: {"name": e["name"], "zone": e["zone"]} for e in entries}}


def weekly_view(run: dict) -> dict | None:
    """What the dashboard gets: the report and whether it is out, never the Slack channel or the claim."""
    w = run.get("weekly")
    if not w:
        return None
    keep = ("week", "improved", "decreased", "wins", "comparison", "status_check", "not_judged", "values", "built_at",
            "posted_by", "posted_at", "post_error")
    return {**{k: w[k] for k in keep if k in w}, "posted": _is_posted(w)}


def _is_posted(w: dict) -> bool:
    return bool((w.get("slack") or {}).get("ts") or w.get("posted_at"))


# ---------------------------------------------------------------------------
# Building later
# ---------------------------------------------------------------------------

def _run_time(run: dict) -> datetime:
    """The instant the run began, which fixes the week; now for a run that has no start."""
    as_of = run.get("as_of")
    return as_of.replace(tzinfo=timezone.utc) if isinstance(as_of, datetime) else datetime.now(timezone.utc)


async def record_build_failure(db, run: dict, error: Exception, now: datetime) -> None:
    """Remember that the weekly isn't built, so retry_weeklies tries again. The first failure starts the day it has."""
    fields = {"weekly_pending": True, "weekly_error": str(error)[:300], "weekly_failed_at": run.get("weekly_failed_at") or now,
              "weekly_retry_at": now + BUILD_RETRY}
    run.update(fields)
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": fields})


async def _store(db, run_id, built: dict) -> None:
    """Put a newly built weekly on the run, field by field so a claim on it stays. A posted one is left as it is."""
    await db[RUNS].update_one({"_id": run_id, "weekly.slack.ts": None, "weekly.posted_at": None},
                              {"$set": {f"weekly.{k}": v for k, v in built.items()}})


async def retry_weeklies(db, now_utc: datetime, deadline: float) -> int:
    """Every agents tick: runs whose weekly didn't build, or whose automatic post didn't go. Builds it, posts it when
    the agent posts on its own, and stops trying a day after the first failure. Never raises. Returns how many are
    finished. The run's own state isn't touched, so this can't change how a run ended."""
    now = now_utc if now_utc.tzinfo is None else now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    rows = await db[RUNS].find({"weekly_pending": True, "kind": {"$ne": PREVIEW}, "weekly_retry_at": {"$lte": now}},
                               {"_id": 1}).sort("weekly_retry_at", 1).limit(RETRY_BATCH).to_list(length=RETRY_BATCH)
    finished = 0
    for row in rows:
        if time.monotonic() >= deadline:
            break
        run = await db[RUNS].find_one({"_id": row["_id"]})
        try:
            finished += await _retry_one(db, run, now)
        except Exception as e:  # noqa: BLE001 - one run must not cost the others theirs
            logger.error("stock run %s: weekly retry failed: %s", row["_id"], e, exc_info=True)
            await db[RUNS].update_one({"_id": row["_id"]}, {"$set": {"weekly_error": str(e)[:300], "weekly_retry_at": now + BUILD_RETRY}})
    return finished


async def _retry_one(db, run: dict, now: datetime) -> int:
    started = run.get("weekly_failed_at") or now
    if now - started > BUILD_GIVE_UP:
        logger.error("stock run %s: giving up on the weekly report: %s", run["_id"], run.get("weekly_error"))
        await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"weekly_pending": False}})
        return 0
    agent = await db[AGENTS].find_one({"_id": run.get("agent_id"), "user_id": run.get("user_id")})
    if agent is None:
        await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"weekly_pending": False}})
        return 0
    if not run.get("weekly"):
        await _store(db, run["_id"], await build_weekly(db, agent, _run_time(run), now=datetime.now(timezone.utc)))
        run = await db[RUNS].find_one({"_id": run["_id"]})
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    if (settings.get("weekly") or {}).get("auto_post") and not _is_posted(run["weekly"]):
        try:
            await post_weekly(db, run, agent, by="auto")
        except Exception as e:  # noqa: BLE001 - tried again next time
            await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"weekly_error": str(e)[:300], "weekly_retry_at": now + BUILD_RETRY}})
            return 0
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {"weekly_pending": False, "weekly_error": None}})
    return 1


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

async def _claim(db, run_id, token: str) -> dict | None:
    """The run's weekly, claimed for this post, or None when it is already out or someone else is posting it."""
    now = _now()
    return await db[RUNS].find_one_and_update(
        {"_id": run_id, "kind": {"$ne": PREVIEW}, "weekly": {"$type": "object"}, "weekly.slack.ts": None,
         "weekly.posted_at": None,
         "$or": [{"weekly.claimed_at": None}, {"weekly.claimed_at": {"$lte": now - CLAIM_STALE}}]},
        {"$set": {"weekly.claimed_at": now, "weekly.claim_token": token}})


async def _release(db, run_id, token: str, **fields) -> None:
    """Free the claim, but only if it is still this poster's."""
    await db[RUNS].update_one({"_id": run_id, "weekly.claim_token": token},
                              {"$set": {"weekly.claimed_at": None, "weekly.claim_token": None,
                                        **{f"weekly.{k}": v for k, v in fields.items()}}})


async def post_weekly(db, run: dict, agent: dict, *, by: str) -> dict:
    """Post the run's weekly message once. {"outcome": posted | already_posted | in_progress | preview | not_built}.
    An unposted weekly is rebuilt first. Raises SlackPublishError when Slack didn't take it; the claim is released,
    so a retry posts it."""
    current = await db[RUNS].find_one({"_id": run["_id"]}, {"weekly": 1, "kind": 1})
    if current is None or not current.get("weekly"):
        return {"outcome": "not_built"}
    if current.get("kind") == PREVIEW or run.get("kind") == PREVIEW:
        return {"outcome": "preview"}
    if _is_posted(current["weekly"]):
        return {"outcome": "already_posted", "ts": (current["weekly"].get("slack") or {}).get("ts")}
    token = str(uuid.uuid4())
    claimed = await _claim(db, run["_id"], token)
    if claimed is None:
        again = ((await db[RUNS].find_one({"_id": run["_id"]}, {"weekly": 1})) or {}).get("weekly") or {}
        return {"outcome": "already_posted", "ts": (again.get("slack") or {}).get("ts")} if _is_posted(again) else {"outcome": "in_progress"}
    try:
        try:
            await _store(db, run["_id"], await build_weekly(db, agent, _run_time(run), now=datetime.now(timezone.utc)))
        except Exception as e:  # noqa: BLE001 - the stored one still has the report
            logger.warning("stock run %s: weekly rebuild before posting failed, using the stored one: %s", run["_id"], e)
        values = ((await db[RUNS].find_one({"_id": run["_id"]}, {"weekly.values": 1})) or {})["weekly"]["values"]
        client, channel = await _connection(db, run, agent)

        def render(template):
            blocks, text, fell_back = render_kind_safe("weekly", template, values, None, _context("weekly", agent))
            return assemble(blocks, None), text, fell_back

        async def send(blocks, text):
            return await _call(client.chat_postMessage, channel=channel, blocks=blocks, text=text)

        resp = await _deliver(db, agent, "weekly", render, send)
    except SlackApiError as e:
        failure = _failure(e, await _note_api_error(db, run["user_id"], e))
        await _release(db, run["_id"], token, post_error=str(failure)[:300])
        raise failure from e
    except Exception as e:  # noqa: BLE001 - release the claim whatever went wrong, then let the caller see it
        await _release(db, run["_id"], token, post_error=str(e)[:300])
        raise
    ts = resp.get("ts")
    # The message is out: record that whatever happened to the claim meanwhile, then free the claim if it is still ours.
    await db[RUNS].update_one({"_id": run["_id"]}, {"$set": {
        "weekly.slack": {"channel_id": channel, "ts": ts}, "weekly.posted_by": by, "weekly.posted_at": _now(),
        "weekly.post_error": None}})
    await _release(db, run["_id"], token)
    return {"outcome": "posted", "ts": ts}


async def post_if_automatic(db, run: dict, agent: dict) -> None:
    """At the end of a publish: post the weekly when the agent is set to. A Slack failure raises SlackPublishError, so
    the run's own late-post retry covers it, and post_weekly makes sure it never goes out twice."""
    settings = merge_settings(DEFAULT_SETTINGS, agent.get("settings") or {})
    if (settings.get("weekly") or {}).get("auto_post") and run.get("weekly"):
        await post_weekly(db, run, agent, by="auto")
