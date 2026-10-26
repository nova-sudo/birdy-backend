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
    last week counts as paused for the whole window.
"""

import hashlib
import logging
import random
from dataclasses import asdict, dataclass, field, replace
from datetime import date, timedelta

from ai.agents.runner import HISTORY_DAYS, MISSING_TARGET_AGENT, evaluate_loaded
from ai.agents.snapshot import AdDay, AdState, load_ad_days, load_client_ctx, load_state
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


async def _replay(db, agent: dict, *, end: date, days: int, near_miss: bool):
    """(day, client, findings) for every replay day (oldest first) and client.
    The missing-target advisory is dropped: it is not an action or a case."""
    start = end - timedelta(days=days - 1)
    rules = [r for r in await list_rules(db, agent["_id"]) if r.get("enabled", True)]
    memories = await list_memory(db, agent["_id"], active_only=True)
    clients = await _load_clients(db, agent, start=start, end=end)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        for c in clients:
            sliced = _slice(c.ad_days, d)
            findings = evaluate_loaded(agent, rules, memories, c.ctx, sliced, ads_on_day(c.ads, sliced, d),
                                       c.adsets, as_of=d, near_miss=near_miss)
            out.append((d, c, [f for f in findings if f.agent != MISSING_TARGET_AGENT]))
    return rules, out


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

def _after(c: _Client, target: dict, fire_day: date, end: date) -> tuple[float, int]:
    """Spend and leads of the target's ads from fire_day + 1 through end."""
    oid, otype = target["object_id"], target["object_type"]
    spend, leads = 0.0, 0
    for ad_id, rows in c.ad_days.items():
        for r in rows:
            if not (fire_day < r.day <= end):
                continue
            if (otype == "adset" and r.adset_id == oid) or (otype != "adset" and ad_id == oid):
                spend += r.spend
                leads += r.leads
    return spend, leads


async def dry_run(db, mongo_client, agent: dict, *, end: date, days: int = 30) -> DryRunResult:
    """What the agent would have done each day from end - days + 1 through end.

    Each (rule kind, target) counts once, on its first fire day. Near misses
    and the missing-target advisory never count. Pauses add the target's spend
    and leads after the fire day; revives and flags add 0."""
    rules, replayed = await _replay(db, agent, end=end, days=days, near_miss=False)
    res = DryRunResult(by_rule={r["kind"]: 0 for r in rules})
    seen: set[tuple[str, str]] = set()
    events = []
    for d, c, findings in replayed:
        for f in findings:
            if f.near_miss:
                continue
            kind, target = _kind(f), _target(f)
            key = (kind, target["object_id"])
            if key in seen:
                continue
            seen.add(key)
            res.actions += 1
            res.by_rule[kind] = res.by_rule.get(kind, 0) + 1
            if f.action is not None and f.action.type == ACTION_PAUSE_ADS:
                spend, leads = _after(c, target, d, end)
                res.spend_saved += spend
                res.leads_at_risk += leads
            events.append({"day": d.isoformat(), "rule_kind": kind, "title": f.title,
                           "client_name": f.client_name, "why": f.description})
    res.spend_saved = round(res.spend_saved, 2)
    # Stable sort: same-day events keep their evaluation order.
    res.examples = sorted(events, key=lambda e: e["day"], reverse=True)[:MAX_EXAMPLES]
    return res


# ---------------------------------------------------------------------------
# Practice cases
# ---------------------------------------------------------------------------

def _case_id(kind: str, object_id: str, d: date) -> str:
    return hashlib.sha1(f"{kind}|{object_id}|{d.isoformat()}".encode()).hexdigest()[:16]


def _options_and_answer(kind: str, f: Finding) -> tuple[list[str], str]:
    if kind in FLAG_KINDS:
        return list(FLAG_OPTIONS), "Flag"
    options = list(PAUSED_OPTIONS) if kind == "revive_winner" else list(LIVE_OPTIONS)
    if f.near_miss or f.action is None:
        return options, LEAVE_IT
    if f.action.type == ACTION_PAUSE_ADS:
        return options, "Pause"
    if f.action.type == ACTION_ENABLE_ADS:
        return options, "Revive"
    return options, LEAVE_IT


def _ad_keys(f: Finding, target: dict) -> set[str]:
    """The ads a case is about (the target itself for ad-set / campaign flags)."""
    ids = [str(i) for i in (f.evidence.raw or {}).get("ad_ids") or []]
    return set(ids) or {target["object_id"]}


def _case(d: date, c: _Client, f: Finding) -> dict:
    kind, target = _kind(f), _target(f)
    if not target["name"] and target["object_type"] == "adset" and target["object_id"] in c.adsets:
        target["name"] = c.adsets[target["object_id"]].name
    options, answer = _options_and_answer(kind, f)
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
    _, replayed = await _replay(db, agent, end=end, days=days, near_miss=True)

    latest: dict[tuple[str, str], tuple[date, _Client, Finding]] = {}
    for d, c, findings in replayed:  # oldest first: later days overwrite
        for f in findings:
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
