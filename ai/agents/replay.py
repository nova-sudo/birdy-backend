"""
ai/agents/replay.py
-------------------
Replays an agent over past days without touching Meta or writing anything.

  dry_run       what the agent would have done over the last `days` days:
                one action per (rule kind, target), counted on its first fire
                day, with the spend / leads a pause would have cut off.
  sample_cases  practice cases for training, drawn from firing and near-miss
                findings over the window.

Each client's data is loaded once (state + every ad_day row the window needs)
and then sliced per replay day; evaluation goes through
runner.evaluate_loaded, the same function the live run uses.

Historical status approximation
    meta_ad_state is today's state, not the state on past day d. On replay day
    d each ad is overridden (see ads_on_day):
      - paused_at set and paused_at <= d          -> PAUSED, with that paused_at
      - else currently PAUSED (paused_at unset or after d)
            spent on d or d-1                      -> ACTIVE
            otherwise                              -> keeps its current status
      - everything else                            -> unchanged
    Ad set (and campaign) status always stays current, so an ad set paused
    last week counts as paused for the whole window. created_time is not used:
    an ad created after d shows as-is, which is harmless because the evaluators
    need spend rows on or before d.

Simulated actions (dry_run only)
    The dry run also layers the agent's own actions over that approximation
    (see apply_sim): an ad the replay pauses on day d is PAUSED from d+1, and an
    ad it revives is ACTIVE from d+1. So one ad can't fire twice, and a later
    ad-set rollup only covers ads that are still live.
"""

import hashlib
import logging
import random
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta

from ai.agents.runner import HISTORY_DAYS, MISSING_TARGET_AGENT, evaluate_loaded
from ai.agents.snapshot import (
    AdDay,
    AdsetState,
    AdState,
    ClientCtx,
    load_ad_days,
    load_client_ctx,
    load_state,
)
from ai.agents.store import list_memory, list_rules
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS, Finding

logger = logging.getLogger(__name__)

MAX_EXAMPLES = 20
FLAG_KINDS = {"flag_no_spend", "flag_declining"}
LIVE_OPTIONS = ["Pause", "Leave it", "Flag"]
PAUSED_OPTIONS = ["Revive", "Leave it"]
FLAG_OPTIONS = ["Flag", "Leave it"]
LEAVE_IT = "Leave it"


@dataclass
class DryRunResult:
    actions: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    spend_saved: float = 0.0
    leads_at_risk: int = 0
    examples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Replay core
# ---------------------------------------------------------------------------

def ads_on_day(ads: dict[str, AdState], ad_days: dict[str, list[AdDay]], d: date) -> dict[str, AdState]:
    """Approximate each ad's status on past day `d` (see the module docstring)."""
    prev = d - timedelta(days=1)
    out: dict[str, AdState] = {}
    for ad_id, ad in ads.items():
        status = (ad.effective_status or "").upper()
        if ad.paused_at is not None and ad.paused_at <= d:
            out[ad_id] = ad if status == "PAUSED" else replace(ad, effective_status="PAUSED")
        elif status == "PAUSED" and any(r.spend > 0 and r.day in (d, prev) for r in ad_days.get(ad_id, [])):
            out[ad_id] = replace(ad, effective_status="ACTIVE", paused_at=None)
        else:
            out[ad_id] = ad
    return out


def apply_sim(ads: dict[str, AdState], sim: dict[str, tuple[str, date]], d: date) -> dict[str, AdState]:
    """Layer simulated actions over `ads`: sim[ad_id] = (status, effective_from)."""
    if not sim:
        return ads
    out = dict(ads)
    for ad_id, (status, since) in sim.items():
        ad = out.get(ad_id)
        if ad is None or d < since:
            continue
        out[ad_id] = replace(ad, effective_status=status, paused_at=since if status == "PAUSED" else None)
    return out


def _slice(ad_days: dict[str, list[AdDay]], d: date) -> dict[str, list[AdDay]]:
    """The rows a live run on day d would have loaded: [d - HISTORY_DAYS, d]."""
    lo = d - timedelta(days=HISTORY_DAYS)
    out = {}
    for ad_id, rows in ad_days.items():
        kept = [r for r in rows if lo <= r.day <= d]
        if kept:
            out[ad_id] = kept
    return out


@dataclass
class _Client:
    gid: str
    ctx: object
    ad_days: dict
    ads: dict
    adsets: dict


async def _load_clients(db, agent: dict, *, start: date, end: date) -> list[_Client]:
    user_id = agent["user_id"]
    out: list[_Client] = []
    for gid in (agent.get("scope") or {}).get("client_group_ids") or []:
        try:
            group = await db["client_groups"].find_one({"id": gid, "user_id": user_id}) or {}
            if not group.get("meta_ad_account_id"):
                continue
            ctx = await load_client_ctx(db, user_id, gid, agent)
            ad_days = await load_ad_days(db, user_id, gid, start - timedelta(days=HISTORY_DAYS), end)
            ads, adsets = await load_state(db, gid)
            out.append(_Client(gid, ctx, ad_days, ads, adsets))
        except Exception as e:
            logger.error("agent replay: loading %s/%s failed: %s", user_id, gid, e, exc_info=True)
    return out


async def _prepare(db, agent: dict, *, end: date, days: int):
    start = end - timedelta(days=days - 1)
    rules = [r for r in await list_rules(db, agent["_id"]) if r.get("enabled", True)]
    memories = await list_memory(db, agent["_id"], active_only=True)
    clients = await _load_clients(db, agent, start=start, end=end)
    return start, rules, memories, clients


def _evaluate_day(agent: dict, rules: list[dict], memories: list[dict], c: "_Client", d: date, *,
                  near_miss: bool, sim: dict | None = None) -> list[Finding]:
    """One client's findings on replay day d. The missing-target advisory is
    dropped: it is not an action or a case."""
    sliced = _slice(c.ad_days, d)
    ads = apply_sim(ads_on_day(c.ads, sliced, d), sim or {}, d)
    findings = evaluate_loaded(agent, rules, memories, c.ctx, sliced, ads, c.adsets, as_of=d, near_miss=near_miss)
    return [f for f in findings if f.agent != MISSING_TARGET_AGENT]


def _kind(f: Finding) -> str:
    return f.agent.split(":", 1)[-1]


def _target(f: Finding) -> dict:
    """{object_id, object_type, name} the finding is about."""
    if f.action is not None and f.action.targets:
        t = f.action.targets[0]
        return {"object_id": str(t.get("object_id") or ""), "object_type": t.get("object_type") or "",
                "name": t.get("name") or ""}
    raw = f.evidence.raw or {}
    if raw.get("adset_id"):
        return {"object_id": raw["adset_id"], "object_type": "adset", "name": ""}
    if raw.get("campaign_id"):
        return {"object_id": raw["campaign_id"], "object_type": "campaign",
                "name": raw.get("campaign_name") or ""}
    ad_ids = raw.get("ad_ids") or []
    return {"object_id": ad_ids[0] if ad_ids else "", "object_type": "ad", "name": ""}


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def _ad_totals(c: "_Client", ad_id: str, after: date, end: date) -> tuple[float, int]:
    """Spend and leads of one ad from after + 1 through end."""
    rows = [r for r in c.ad_days.get(ad_id, []) if after < r.day <= end]
    return sum(r.spend for r in rows), sum(r.leads for r in rows)


def _affected_ads(f: Finding, target: dict) -> list[str]:
    ids = [str(i) for i in (f.evidence.raw or {}).get("ad_ids") or []]
    return ids or ([target["object_id"]] if target["object_type"] == "ad" else [])


class DryRunTimeout(Exception):
    """The dry run passed its deadline (time.monotonic()) before finishing."""


async def dry_run(db, mongo_client, agent: dict, *, end: date, days: int = 30,
                  deadline: float | None = None) -> DryRunResult:
    """What the agent would have done each day from end - days + 1 through end.

    Each (rule kind, target) counts once, on its first fire day. Near misses
    and the missing-target advisory never count. The agent's own pauses and
    revives are simulated from the day after they fire (module docstring), and
    a pause on an ad set counts only if it pauses an ad not already paused.
    spend_saved / leads_at_risk add each paused ad once, from the day after its
    earliest simulated pause through end; revives and flags add 0.

    `deadline` (a time.monotonic() value) bounds the work: past it the replay
    raises DryRunTimeout between days instead of running into the platform's
    function timeout."""
    start, rules, memories, clients = await _prepare(db, agent, end=end, days=days)
    res = DryRunResult(by_rule={r["kind"]: 0 for r in rules})
    seen: set[tuple[str, str, str]] = set()
    sim: dict[str, dict[str, tuple[str, date]]] = {c.gid: {} for c in clients}
    paused_on: dict[tuple[str, str], date] = {}  # (gid, ad_id) -> earliest simulated pause day
    events = []
    for i in range(days):
        if deadline is not None and time.monotonic() > deadline:
            raise DryRunTimeout(f"stopped after {i} of {days} days")
        d = start + timedelta(days=i)
        for c in clients:
            csim = sim[c.gid]
            for f in _evaluate_day(agent, rules, memories, c, d, near_miss=False, sim=csim):
                if f.near_miss:
                    continue
                kind, target = _kind(f), _target(f)
                key = (kind, target["object_type"], target["object_id"])
                if key in seen:
                    continue
                act = f.action.type if f.action is not None else None
                ads = _affected_ads(f, target)
                if act == ACTION_PAUSE_ADS:
                    fresh = [a for a in ads if (c.gid, a) not in paused_on]
                    if ads and not fresh:
                        continue  # every ad it would pause is already paused
                    for a in fresh:
                        paused_on[(c.gid, a)] = d
                        spend, leads = _ad_totals(c, a, d, end)
                        res.spend_saved += spend
                        res.leads_at_risk += leads
                    for a in ads:
                        csim[a] = ("PAUSED", d + timedelta(days=1))
                elif act == ACTION_ENABLE_ADS:
                    for a in ads:
                        csim[a] = ("ACTIVE", d + timedelta(days=1))
                seen.add(key)
                res.actions += 1
                res.by_rule[kind] = res.by_rule.get(kind, 0) + 1
                events.append({"day": d.isoformat(), "rule_kind": kind, "title": f.title,
                               "client_name": f.client_name, "why": f.description})
    res.spend_saved = round(res.spend_saved, 2)
    # Stable sort: same-day events keep their evaluation order.
    res.examples = sorted(events, key=lambda e: e["day"], reverse=True)[:MAX_EXAMPLES]
    return res


async def fired_targets(db, agent: dict, variants: list[dict], *, end: date, days: int = 30
                        ) -> list[set[tuple[str, str]]]:
    """For each rule variant (the same rule with different params / client
    overrides), the (client_group_id, target object_id) pairs it fires on over
    end - days + 1 .. end. Near misses don't count. Data is loaded once and
    shared by every variant; each variant is evaluated on its own (no other
    rules, no simulated actions), with the agent's active memories applied.
    Used by learning.replay_effect to show what a proposed change removes."""
    start = end - timedelta(days=days - 1)
    memories = await list_memory(db, agent["_id"], active_only=True)
    clients = await _load_clients(db, agent, start=start, end=end)
    out: list[set[tuple[str, str]]] = []
    for rule in variants:
        fired: set[tuple[str, str]] = set()
        for i in range(days):
            d = start + timedelta(days=i)
            for c in clients:
                for f in _evaluate_day(agent, [rule], memories, c, d, near_miss=False):
                    if not f.near_miss:
                        fired.add((c.gid, _target(f)["object_id"]))
        out.append(fired)
    return out


# ---------------------------------------------------------------------------
# Practice cases
# ---------------------------------------------------------------------------

def _case_id(kind: str, object_id: str, d: date) -> str:
    return hashlib.sha1(f"{kind}|{object_id}|{d.isoformat()}".encode()).hexdigest()[:16]


def answer_label(f: Finding | None) -> str:
    """The practice answer a finding stands for. Shared by the sampler's
    birdy_answer and the confidence re-score, so the two can't drift."""
    if f is None or f.near_miss:
        return LEAVE_IT
    if f.action is not None:
        if f.action.type == ACTION_PAUSE_ADS:
            return "Pause"
        if f.action.type == ACTION_ENABLE_ADS:
            return "Revive"
        return LEAVE_IT
    return "Flag" if _kind(f) in FLAG_KINDS else LEAVE_IT


def _options(kind: str) -> list[str]:
    if kind in FLAG_KINDS:
        return list(FLAG_OPTIONS)
    return list(PAUSED_OPTIONS) if kind == "revive_winner" else list(LIVE_OPTIONS)


def _ad_keys(f: Finding, target: dict) -> set[str]:
    """The ads a case is about (the target itself for ad-set / campaign flags)."""
    ids = [str(i) for i in (f.evidence.raw or {}).get("ad_ids") or []]
    return set(ids) or {target["object_id"]}


# ---------------------------------------------------------------------------
# Case snapshots and re-scoring
# ---------------------------------------------------------------------------

def _iso(v) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else v


EXACT_DAYS = 60  # day rows kept as-is in a case snapshot; older ones collapse to one row per ad


def _compact_rows(rows: list[AdDay], d: date) -> list[AdDay]:
    """Non-zero rows on or before d. Rows older than EXACT_DAYS collapse into one
    row on the ad's first spend day carrying their summed spend and leads. That
    keeps everything an evaluator reads: the first spend day and lifetime sums
    (pause patience, revive), and the exact recent days (the pause 7-day window,
    the no-spend and declining windows)."""
    cutoff = d - timedelta(days=EXACT_DAYS - 1)
    kept = [r for r in rows if r.day <= d and (r.spend or r.leads)]
    old = [r for r in kept if r.day < cutoff]
    recent = [r for r in kept if r.day >= cutoff]
    if not old:
        return recent
    first = next((r for r in old if r.spend > 0), old[0])
    merged = replace(first, spend=sum(r.spend for r in old), leads=sum(r.leads for r in old))
    return [merged] + recent


def _neighbourhood(kind: str, f: Finding, ads: dict, sliced: dict) -> set[str]:
    """The ads whose rows are needed to re-score this target, and no others:
    an ad target -> the ad plus the other ads in its ad set (the pause ad-set
    rollup checks them); an ad-set target -> its member ads; a campaign flag
    (declining) -> the ads in that campaign."""
    raw = f.evidence.raw or {}
    ad_ids = {str(a) for a in raw.get("ad_ids") or []}
    if kind == "flag_declining":
        cid = raw.get("campaign_id")
        return {a for a, ad in ads.items() if ad.campaign_id == cid} | \
               {a for a, rows in sliced.items() if any(r.campaign_id == cid for r in rows)}
    if kind == "flag_no_spend":
        sid = raw.get("adset_id")
        return {a for a, ad in ads.items() if ad.adset_id == sid} | \
               {a for a, rows in sliced.items() if any(r.adset_id == sid for r in rows)}
    sets = {ads[a].adset_id for a in ad_ids if a in ads}
    return {a for a, ad in ads.items() if ad.adset_id in sets} | ad_ids


def _eval_snapshot(c: "_Client", d: date, f: Finding, kind: str) -> dict:
    """Everything needed to re-run this case's evaluator on day d without the
    database: the client context, plus state and compacted day rows (see
    _compact_rows) for the target's neighbourhood only (see _neighbourhood),
    and the state of those ads' ad sets. Nothing else in the client is stored."""
    sliced = _slice(c.ad_days, d)
    ads = ads_on_day(c.ads, sliced, d)
    raw = f.evidence.raw or {}
    scope = _neighbourhood(kind, f, ads, sliced)
    adset_ids = {ads[a].adset_id for a in scope if a in ads}
    if raw.get("adset_id"):
        adset_ids.add(raw["adset_id"])
    ctx = c.ctx
    return {
        "ctx": {"client_group_id": ctx.client_group_id, "name": ctx.name,
                "currency_symbol": ctx.currency_symbol, "target_cpl": ctx.target_cpl, "tz": ctx.tz},
        "ads": [{"ad_id": a.ad_id, "adset_id": a.adset_id, "campaign_id": a.campaign_id, "name": a.name,
                 "effective_status": a.effective_status, "created_time": _iso(a.created_time),
                 "paused_at": _iso(a.paused_at)}
                for a in (ads[i] for i in sorted(scope) if i in ads)],
        "adsets": [asdict(c.adsets[s]) for s in sorted(adset_ids) if s in c.adsets],
        "ad_days": [[r.ad_id, r.adset_id, r.campaign_id, r.ad_name, r.campaign_name, r.day.isoformat(),
                     r.spend, r.leads]
                    for a in sorted(scope) for r in _compact_rows(sliced.get(a, []), d)],
    }


def _load_eval(ev: dict):
    ctx = ClientCtx(**ev["ctx"])
    ads = {a["ad_id"]: AdState(**{**a,
                                  "created_time": datetime.fromisoformat(a["created_time"]) if a["created_time"] else None,
                                  "paused_at": date.fromisoformat(a["paused_at"]) if a["paused_at"] else None})
           for a in ev.get("ads") or []}
    adsets = {s["adset_id"]: AdsetState(**s) for s in ev.get("adsets") or []}
    ad_days: dict[str, list[AdDay]] = {}
    for ad_id, adset_id, campaign_id, ad_name, campaign_name, day, spend, leads in ev.get("ad_days") or []:
        ad_days.setdefault(ad_id, []).append(AdDay(
            ad_id=ad_id, adset_id=adset_id, campaign_id=campaign_id, ad_name=ad_name,
            day=date.fromisoformat(day), spend=spend, leads=leads, campaign_name=campaign_name))
    return ctx, ad_days, ads, adsets


def case_ad_keys(case: dict) -> set[str]:
    """The ads a stored case is about (the target itself for ad-set / campaign flags)."""
    snap = case.get("snapshot") or {}
    ids = {str(i) for i in (snap.get("raw") or {}).get("ad_ids") or []}
    return ids or {str((snap.get("target") or {}).get("object_id") or "")} - {""}


def rescore_case(agent: dict, rule: dict, memories: list[dict], case: dict) -> str | None:
    """Re-run the case's evaluator on its stored snapshot with `rule` as it is
    now (current params and client overrides) and return the answer Birdy would
    give, or None when the snapshot predates re-scoring."""
    snap = case.get("snapshot") or {}
    ev = snap.get("eval")
    if not ev:
        return None
    ctx, ad_days, ads, adsets = _load_eval(ev)
    d = date.fromisoformat(snap["day"])
    findings = evaluate_loaded(agent, [rule], memories, ctx, ad_days, ads, adsets, as_of=d, near_miss=True)
    kind, target, keys = snap.get("rule_kind"), snap.get("target") or {}, case_ad_keys(case)
    match = [f for f in findings
             if f.agent != MISSING_TARGET_AGENT and _kind(f) == kind
             and (_target(f)["object_id"] == target.get("object_id") or _ad_keys(f, _target(f)) & keys)]
    # A firing finding outranks a near miss on the same ads.
    match.sort(key=lambda f: f.near_miss)
    return answer_label(match[0] if match else None)


def _case(d: date, c: _Client, f: Finding) -> dict:
    kind, target = _kind(f), _target(f)
    if not target["name"] and target["object_type"] == "adset" and target["object_id"] in c.adsets:
        target["name"] = c.adsets[target["object_id"]].name
    options, answer = _options(kind), answer_label(f)
    return {
        "case_id": _case_id(kind, target["object_id"], d),
        "rule_kind": kind,
        "client_name": f.client_name,
        "title": f.title,
        "facts": [{"label": s.label, "value": s.value} for s in f.evidence.stats],
        "options": options,
        "birdy_answer": answer,
        "why": f.description,
        "snapshot": {
            "rule_kind": kind,
            "client_group_id": c.gid,
            "target": target,
            "day": d.isoformat(),
            "raw": dict(f.evidence.raw or {}),
            "eval": _eval_snapshot(c, d, f, kind),
        },
    }


async def sample_cases(db, mongo_client, agent: dict, *, end: date, days: int = 60, k: int = 3,
                       seed: int, exclude_ad_ids: set[str]) -> list[dict]:
    """k practice cases from firing and near-miss findings over the window.

    Each target contributes its latest occurrence. Cases are drawn round-robin
    across rule kinds, then across clients within a kind, never repeating an ad
    (nor any in exclude_ad_ids). random.Random(seed) is the only randomness, so
    the same data and seed give the same cases."""
    rng = random.Random(seed)
    start, rules, memories, clients = await _prepare(db, agent, end=end, days=days)

    latest: dict[tuple[str, str], tuple[date, _Client, Finding]] = {}
    for i in range(days):  # oldest first: later days overwrite
        d = start + timedelta(days=i)
        for c in clients:
            for f in _evaluate_day(agent, rules, memories, c, d, near_miss=True):
                latest[(_kind(f), _target(f)["object_id"])] = (d, c, f)

    # kind -> client -> candidates, each level in a seeded order.
    buckets: dict[str, dict[str, list]] = {}
    for key in sorted(latest):
        d, c, f = latest[key]
        buckets.setdefault(key[0], {}).setdefault(c.gid, []).append((d, c, f))
    kinds = sorted(buckets)
    rng.shuffle(kinds)
    queues: dict[str, list[list]] = {}
    for kind in kinds:
        gids = sorted(buckets[kind])
        rng.shuffle(gids)
        for gid in gids:
            rng.shuffle(buckets[kind][gid])
        queues[kind] = [buckets[kind][gid] for gid in gids]

    used = {str(a) for a in exclude_ad_ids or ()}
    cases: list[dict] = []
    while len(cases) < k and any(queues.values()):
        for kind in kinds:
            if len(cases) >= k:
                break
            clients = queues[kind]
            picked = None
            while clients and picked is None:
                cands = clients.pop(0)
                while cands:
                    d, c, f = cands.pop(0)
                    keys = _ad_keys(f, _target(f))
                    if not keys & used:
                        picked = (d, c, f, keys)
                        break
                if cands:
                    clients.append(cands)  # rotate this client to the back
            if picked is not None:
                d, c, f, keys = picked
                used |= keys
                cases.append(_case(d, c, f))
    return cases
