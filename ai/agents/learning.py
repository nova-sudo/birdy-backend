"""
ai/agents/learning.py
---------------------
How a BirdyHub agent learns from the user's decisions on its work items.

  record_decision  one decision on an agent item -> the rule's stats. Atomic
                   Mongo updates only ($inc / $push with $slice), so concurrent
                   decisions never lose a count.
                     approved     streak += 1, last10 += ok
                     denied       streak = 0,  last10 += no  (+ memory from the note)
                     undone_user  streak = 0,  last10 += undo
                     undone_auto  streak = 0,  last10 += undo, undone_auto += now;
                                  2+ auto-undos within 7 days on an auto rule
                                  -> the brake (rule back to ask + a notice)
  pending_offers   graduation offers (computed from rule stats) + brake notices
                   (stored on agents.offers).
  graduate         the user's answer to an offer: auto_notify | auto_silent |
                   keep_asking.
  memory_from_note one LLM call turns a deny note into a memory: a structured
                   exception when it maps onto a supported filter, else a fact.
                   Never raises.
  scan_proposals   repeated deny reasons on one rule -> a rule-change proposal
                   with its 30-day replay effect. accept_proposal applies it
                   through store.update_rule (versioned) and logs it.

Offer shapes (GET /api/agents/{id}/offers):
  {"id": "graduation:<rule_id>", "type": "graduation", "rule_id", "rule_kind",
   "rule_label", "streak", "last10", "choices": [auto_notify, auto_silent, keep_asking]}
  {"id": "brake:<rule_id>", "type": "brake", "rule_id", "rule_kind", "rule_label",
   "undos", "at", "choices": [keep_asking]}
"""

import asyncio
import copy
import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta

from pymongo import ReturnDocument

from ai.agents import replay, validation
from ai.agents.defaults import DEFAULT_PARAMS, RULE_KINDS
from ai.agents.guardrails import agent_tz, local_today
from ai.agents.memory_filters import KNOWN_KEYS
from ai.agents.store import (
    AGENTS,
    PROPOSALS,
    RULES,
    add_memory,
    list_rules,
    sync_open_rule_mode,
    update_rule,
)
from ai.suggestions import store as sstore

logger = logging.getLogger(__name__)

DECISIONS = ("approved", "denied", "undone_user", "undone_auto")
CHOICES = ("auto_notify", "auto_silent", "keep_asking")
OFFER_AFTER = 10            # approvals in a row before graduation is offered
KEEP_ASKING_MORE = 10       # "keep asking" offers again after this many more
LAST_N = 10
UNDONE_AUTO_KEEP = 20       # auto-undo timestamps kept per rule (only the last 7 days matter)
BRAKE_UNDOS = 2
BRAKE_WINDOW = timedelta(days=7)

PROPOSAL_WINDOW = timedelta(days=14)
PROPOSAL_MIN = 3
REJECT_COOLDOWN = timedelta(days=30)
REPLAY_DAYS = 30
PROPOSAL_REASONS = ("creative_stale", "still_learning", "target_wrong")

KIND_BRAKE = "agent_brake"
KIND_GRADUATED = "rule_graduated"
KIND_PROPOSAL_ACCEPTED = "proposal_accepted"

RULE_LABELS = {
    "pause_over_target": "Pause over target",
    "revive_winner": "Revive old winners",
    "flag_no_spend": "Flag no spend",
    "flag_declining": "Flag declining performance",
}

NOTE_MAX = 1000
LLM_TIMEOUT_S = 8
MIN_AD_NAME_MATCH = 3      # shorter ad-name matches would exclude almost everything
MEMORY_TEXT_MAX = 280


class NoOffer(Exception):
    """graduate() was called for a rule with no matching pending offer."""


class ProposalNotFound(Exception):
    pass


class RuleChanged(Exception):
    """The rule was edited after the proposal was made; the proposal is out of date."""


class ProposalClosed(Exception):
    """The proposal is no longer open (already accepted or rejected)."""


def _now() -> datetime:
    return datetime.utcnow()


def _label(kind: str | None) -> str:
    return RULE_LABELS.get(kind or "", kind or "rule")


# ---------------------------------------------------------------------------
# Decisions, streaks and the brake
# ---------------------------------------------------------------------------

def _push_last(value: str) -> dict:
    return {"$each": [value], "$slice": -LAST_N}


async def record_decision(db, agent_id: str, rule_id: str, decision: str, *,
                          reason_code: str | None = None, note: str | None = None,
                          now: datetime | None = None, doc: dict | None = None) -> dict:
    """Fold one decision into the rule's stats. Returns
    {"rule": <updated rule or None>, "memory": <memory from the note or None>,
     "brake": <True when this decision tripped the brake>}."""
    if decision not in DECISIONS:
        raise ValueError(f"unknown decision: {decision}")
    now = now or _now()
    out: dict = {"rule": None, "memory": None, "brake": False}

    # A broken streak starts over: the next offer comes after OFFER_AFTER in a
    # row again, however many times "keep asking" raised the bar before.
    if decision == "approved":
        update = {"$inc": {"stats.streak": 1, "stats.approved": 1},
                  "$push": {"stats.last10": _push_last("ok")}}
    elif decision == "denied":
        update = {"$set": {"stats.streak": 0, "stats.offer_after": OFFER_AFTER}, "$inc": {"stats.denied": 1},
                  "$push": {"stats.last10": _push_last("no")}}
    elif decision == "undone_user":
        update = {"$set": {"stats.streak": 0, "stats.offer_after": OFFER_AFTER},
                  "$push": {"stats.last10": _push_last("undo")}}
    else:
        update = {"$set": {"stats.streak": 0, "stats.offer_after": OFFER_AFTER},
                  "$push": {"stats.last10": _push_last("undo"),
                            "stats.undone_auto": {"$each": [now], "$slice": -UNDONE_AUTO_KEEP}}}

    rule = await db[RULES].find_one_and_update(
        {"_id": rule_id, "agent_id": agent_id}, update, return_document=ReturnDocument.AFTER)
    if rule is None:
        return out
    out["rule"] = rule

    if decision == "undone_auto":
        out["brake"] = await _maybe_brake(db, agent_id, rule, now)
    if decision == "denied" and note and note.strip():
        agent = await db[AGENTS].find_one({"_id": agent_id})
        if agent is not None:
            out["memory"] = await memory_from_note(db, agent, note, doc or {})
    return out


async def _maybe_brake(db, agent_id: str, rule: dict, now: datetime) -> bool:
    stamps = (rule.get("stats") or {}).get("undone_auto") or []
    # Only undos of this auto-run stint count: older ones (before a re-graduation) don't.
    since = now - BRAKE_WINDOW
    auto_since = rule.get("auto_since")
    if isinstance(auto_since, datetime) and auto_since > since:
        since = auto_since
    recent = [t for t in stamps if isinstance(t, datetime) and since <= t <= now]
    if len(recent) < BRAKE_UNDOS:
        return False
    # Only the decision that flips auto -> ask brakes; a concurrent one finds ask.
    flipped = await db[RULES].find_one_and_update(
        {"_id": rule["_id"], "agent_id": agent_id, "mode": "auto"},
        {"$set": {"mode": "ask", "braked_at": now}})
    if flipped is None:
        return False
    await sync_open_rule_mode(db, rule["_id"], "ask")
    kind = rule.get("kind")
    notice = {"id": f"brake:{rule['_id']}", "type": "brake", "rule_id": rule["_id"],
              "rule_kind": kind, "undos": len(recent), "at": now}
    await db[AGENTS].update_one({"_id": agent_id}, {"$pull": {"offers": {"rule_id": rule["_id"]}}})
    await db[AGENTS].update_one({"_id": agent_id}, {"$push": {"offers": notice}})
    agent = await db[AGENTS].find_one({"_id": agent_id})
    if agent is not None:
        await sstore.log_activity(
            db, agent["user_id"], actor=sstore.ACTOR_BIRDY, kind=KIND_BRAKE,
            title=f"{_label(kind)} is back on Ask me",
            label=f"You undid {len(recent)} of its auto-run actions in 7 days, so Birdy will ask first again",
            agent_id=agent_id, rule_id=rule["_id"])
    return True


# ---------------------------------------------------------------------------
# Offers and graduation
# ---------------------------------------------------------------------------

def _kind_order(rule: dict) -> int:
    kind = rule.get("kind")
    return RULE_KINDS.index(kind) if kind in RULE_KINDS else len(RULE_KINDS)


def _graduation_offer(rule: dict) -> dict | None:
    stats = rule.get("stats") or {}
    streak = int(stats.get("streak") or 0)
    if rule.get("mode") != "ask" or not rule.get("enabled", True):
        return None
    if streak < int(stats.get("offer_after") or OFFER_AFTER):
        return None
    return {"id": f"graduation:{rule['_id']}", "type": "graduation", "rule_id": rule["_id"],
            "rule_kind": rule.get("kind"), "rule_label": _label(rule.get("kind")),
            "streak": streak, "last10": list(stats.get("last10") or []), "choices": list(CHOICES)}


async def pending_offers(db, agent: dict) -> list[dict]:
    """Graduation offers, then brake notices."""
    fresh = await db[AGENTS].find_one({"_id": agent["_id"]}) or agent
    rules = sorted(await list_rules(db, agent["_id"]), key=_kind_order)
    out = [o for o in (_graduation_offer(r) for r in rules) if o]
    for n in fresh.get("offers") or []:
        if n.get("type") == "brake":
            out.append({**n, "rule_label": _label(n.get("rule_kind")), "choices": ["keep_asking"]})
    return out


async def graduate(db, agent: dict, rule_id: str, choice: str, *, now: datetime | None = None) -> dict:
    """Answer a pending offer for this rule. A graduation offer takes any
    choice; a brake notice alone takes keep_asking (it acknowledges the notice).
    Either way the rule's brake notice is dismissed. Raises KeyError for an
    unknown rule, NoOffer when nothing is pending for it."""
    if choice not in CHOICES:
        raise ValueError(f"choice must be one of {list(CHOICES)}")
    now = now or _now()
    agent_id = agent["_id"]
    rule = await db[RULES].find_one({"_id": rule_id, "agent_id": agent_id})
    if rule is None:
        raise KeyError(f"rule {rule_id} not found")
    offers = [o for o in await pending_offers(db, agent) if o["rule_id"] == rule_id]
    has_grad = any(o["type"] == "graduation" for o in offers)
    has_brake = any(o["type"] == "brake" for o in offers)
    if not (has_grad or (has_brake and choice == "keep_asking")):
        raise NoOffer(f"no pending offer for rule {rule_id}")

    if choice == "keep_asking":
        streak = int((rule.get("stats") or {}).get("streak") or 0)
        await db[RULES].update_one({"_id": rule_id, "agent_id": agent_id},
                                   {"$set": {"stats.offer_after": streak + KEEP_ASKING_MORE}})
    else:
        done = await db[RULES].find_one_and_update(
            {"_id": rule_id, "agent_id": agent_id, "mode": "ask"},
            {"$set": {"mode": "auto", "auto_since": now, "notify": choice == "auto_notify"}})
        if done is None:
            raise NoOffer(f"rule {rule_id} is no longer on ask")
        await sync_open_rule_mode(db, rule_id, "auto")
        await sstore.log_activity(
            db, agent["user_id"], actor=sstore.ACTOR_USER, kind=KIND_GRADUATED,
            title=f"{_label(rule.get('kind'))} now runs on its own",
            label="Auto-run, tell me after" if choice == "auto_notify" else "Auto-run silently",
            agent_id=agent_id, rule_id=rule_id)
    await db[AGENTS].update_one({"_id": agent_id}, {"$pull": {"offers": {"rule_id": rule_id}}})
    return await db[RULES].find_one({"_id": rule_id, "agent_id": agent_id})


# ---------------------------------------------------------------------------
# Memory from deny notes
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You turn a media buyer's note, written when they declined an action proposed by an "
    "ad-management agent, into one memory for that agent. Reply with JSON only, no prose:\n"
    '{"kind": "exception" | "fact", "text": "<one short sentence: what to remember>", "filter": {...}}\n'
    'Use kind "exception" only when the note asks the agent to leave something alone in a way '
    "these filter keys can express (every key given must match):\n"
    "  client_group_id   one of the client ids listed in the context\n"
    "  ad_name_contains  text matched case-insensitively against ad names\n"
    "  weekdays          list of integers, Monday = 0 ... Sunday = 6\n"
    'Otherwise use kind "fact" and omit filter.'
)


def _parse_json(text) -> dict | None:
    if not isinstance(text, str):
        return None
    s = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None
    return out if isinstance(out, dict) else None


def _clean_filter(flt, allowed_gids: set[str]) -> dict | None:
    """A filter memory_filters can apply, or None if any part is unusable."""
    if not isinstance(flt, dict) or not flt:
        return None
    out: dict = {}
    for key, val in flt.items():
        if key not in KNOWN_KEYS:
            return None
        if key == "client_group_id":
            if not isinstance(val, str) or val not in allowed_gids:
                return None
            out[key] = val
        elif key == "ad_name_contains":
            if not isinstance(val, str) or len(val.strip()) < MIN_AD_NAME_MATCH:
                return None
            out[key] = val.strip()[:100]
        else:  # weekdays
            if not isinstance(val, list) or not val:
                return None
            days = []
            for d in val:
                if isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6:
                    return None
                days.append(d)
            out[key] = sorted(set(days))
    return out


async def _context(db, agent: dict, note: str, doc: dict) -> tuple[str, set[str]]:
    gids = list((agent.get("scope") or {}).get("client_group_ids") or [])
    if doc.get("client_group_id") and doc["client_group_id"] not in gids:
        gids.append(doc["client_group_id"])
    names = {}
    if gids:
        async for g in db["client_groups"].find({"id": {"$in": gids}, "user_id": agent.get("user_id")}):
            names[g["id"]] = g.get("name") or g["id"]
    targets = ((doc.get("action") or {}).get("targets") or [])
    ctx = {
        "note": note,
        "declined_item": {
            "title": doc.get("title"),
            "rule": (doc.get("agent") or "").split(":", 1)[-1] or None,
            "client_group_id": doc.get("client_group_id"),
            "client_name": doc.get("client_name"),
            "ad_names": [t.get("name") for t in targets if t.get("name")],
        },
        "clients": [{"id": g, "name": names.get(g, g)} for g in gids],
    }
    return json.dumps(ctx, default=str), set(gids)


async def _meter(db, user_id: str, provider, resp) -> None:
    """Bill the call like the other server-side LLM calls. Never raises."""
    try:
        from credits import record_usage
        usage = getattr(resp, "usage", None)
        await record_usage(
            db, user_id, model=getattr(provider, "model", None),
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
            source="agents", feature="agent_note_memory")
    except Exception as e:
        logger.warning("learning: metering the note call failed for %s: %s", user_id, e)


async def memory_from_note(db, agent: dict, note: str, doc: dict) -> dict:
    """Store the note as a memory. One LLM call decides between a structured
    exception (a filter memory_filters can apply) and a free-text fact. Any
    error, or output that can't be used, stores the note as a fact. Never raises."""
    note = (note or "").strip()[:NOTE_MAX]
    text, kind, flt = note, "fact", None
    try:
        from ai import provider_factory
        ctx, allowed = await _context(db, agent, note, doc or {})
        provider = await provider_factory.get_provider_for_user(agent["user_id"], db)
        resp = await asyncio.wait_for(provider.chat_completion(
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": ctx}],
            temperature=0, max_tokens=300), timeout=LLM_TIMEOUT_S)
        await _meter(db, agent["user_id"], provider, resp)
        parsed = _parse_json(getattr(resp, "content", None))
        if parsed is not None:
            if isinstance(parsed.get("text"), str) and parsed["text"].strip():
                text = parsed["text"].strip()[:MEMORY_TEXT_MAX]
            if parsed.get("kind") == "exception":
                flt = _clean_filter(parsed.get("filter"), allowed)
                kind = "exception" if flt else "fact"
    except Exception as e:
        logger.warning("learning: note -> memory fell back to a fact for agent %s: %s",
                       agent.get("_id"), e)
        text, kind, flt = note, "fact", None
    try:
        return await add_memory(db, agent["_id"], text=text, kind=kind, source="denial",
                                source_ref=(doc or {}).get("_id"), filter=flt)
    except Exception as e:
        logger.error("learning: could not store memory for agent %s: %s", agent.get("_id"), e)
        return {"text": text, "kind": kind, "filter": flt, "source": "denial",
                "source_ref": (doc or {}).get("_id"), "active": False}


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def _ceil_tenth(x: float) -> float:
    return round(math.ceil(round(x * 10, 6)) / 10, 1)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _effective(rule: dict) -> dict:
    return {**DEFAULT_PARAMS.get(rule.get("kind"), {}), **(rule.get("params") or {})}


def derive_change(rule: dict, reason_code: str, docs: list[dict]) -> dict | None:
    """The change that would have excluded these denied items, or None.
      creative_stale -> max_creative_age_days = min denied creative age, floored to 10s
      still_learning -> patience_spend and patience_days x 1.5 (half-up)
      target_wrong   -> client override cpl_multiple for the client with the most
                        denials = its highest observed CPL / target, rounded up to 0.1
    A reason the rule has no matching param for yields None."""
    kind = rule.get("kind")
    defaults = DEFAULT_PARAMS.get(kind, {})
    params = _effective(rule)

    if reason_code == "creative_stale":
        key = "max_creative_age_days"
        ages = [_num((d.get("evidence") or {}).get("creative_age_days")) for d in docs]
        ages = [a for a in ages if a is not None]
        if key not in defaults or not ages:
            return None
        new = int(min(ages) // 10 * 10)
        if new <= 0 or new >= params[key]:
            return None
        return {"kind": "param_change", "before": {key: params[key]}, "after": {key: new}}

    if reason_code == "still_learning":
        keys = ("patience_spend", "patience_days")
        if not all(k in defaults for k in keys):
            return None
        return {"kind": "param_change",
                "before": {k: params[k] for k in keys},
                "after": {k: _round_half_up(params[k] * 1.5) for k in keys}}

    if reason_code == "target_wrong":
        key = "cpl_multiple"
        if key not in defaults:
            return None
        by_client: dict[str, list[dict]] = {}
        for d in docs:
            if d.get("client_group_id"):
                by_client.setdefault(d["client_group_id"], []).append(d)
        if not by_client:
            return None
        gid = sorted(by_client, key=lambda g: (-len(by_client[g]), g))[0]
        multiples = []
        for d in by_client[gid]:
            raw = d.get("evidence") or {}
            cpl, target = _num(raw.get("cpl")), _num(raw.get("target_cpl"))
            if cpl is not None and target:
                multiples.append(cpl / target)
        if not multiples:
            return None
        new = _ceil_tenth(max(multiples))
        cur = {**params, **((rule.get("client_overrides") or {}).get(gid) or {})}[key]
        if new <= cur:
            return None
        return {"kind": "client_override", "client_group_id": gid,
                "client_name": by_client[gid][0].get("client_name"),
                "before": {key: cur}, "after": {key: new}}
    return None


def apply_change(rule: dict, change: dict) -> dict:
    """The rule as it would be with the change applied (not persisted)."""
    out = copy.deepcopy(rule)
    if change["kind"] == "client_override":
        ov = out.setdefault("client_overrides", {}) or {}
        gid = change["client_group_id"]
        ov[gid] = {**(ov.get(gid) or {}), **change["after"]}
        out["client_overrides"] = ov
    else:
        out["params"] = {**(out.get("params") or {}), **change["after"]}
    return out


async def replay_effect(db, mongo_client, agent: dict, rule: dict, changed: dict, denied_docs: list[dict], *,
                        end, days: int = REPLAY_DAYS) -> dict:
    """{removed, of, removed_were_denied}: of the targets the rule fires on over
    the last `days` days (of), how many the changed rule no longer fires on
    (removed), and how many of those the user had denied."""
    before, after = await replay.fired_targets(db, agent, [rule, changed], end=end, days=days)
    removed = before - after
    denied = {(d.get("client_group_id"), str(t.get("object_id")))
              for d in denied_docs for t in ((d.get("action") or {}).get("targets") or [])}
    return {"removed": len(removed), "of": len(before), "removed_were_denied": len(removed & denied)}


async def scan_proposals(db, mongo_client, agent: dict, *, now: datetime | None = None) -> list[dict]:
    """Open a proposal for each (rule, reason_code) denied at least 3 times in
    the last 14 days. Skipped while one is open for that pair, and for 30 days
    after a rejection unless the count is at least double the rejected one's.
    Denials before the pair's last accepted proposal don't count again.
    Returns the proposals opened by this scan."""
    now = now or _now()
    agent_id = agent["_id"]
    rows = await db[sstore.ACTIVITY].find({
        "agent_id": agent_id, "kind": sstore.KIND_SUGGESTION_DISMISSED,
        "reason_code": {"$in": list(PROPOSAL_REASONS)}, "created_at": {"$gte": now - PROPOSAL_WINDOW},
    }).sort("created_at", 1).to_list(length=None)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r.get("rule_id") and r.get("ref_id"):
            groups.setdefault((r["rule_id"], r["reason_code"]), []).append(r)
    if not any(len({r["ref_id"] for r in g}) >= PROPOSAL_MIN for g in groups.values()):
        return []

    rules = {r["_id"]: r for r in await list_rules(db, agent_id)}
    end = local_today(now, agent_tz(agent)) - timedelta(days=1)
    opened: list[dict] = []
    for (rule_id, reason), group in groups.items():
        rule = rules.get(rule_id)
        if rule is None:
            continue
        key = {"agent_id": agent_id, "rule_id": rule_id, "reason_code": reason}
        accepted = await db[PROPOSALS].find(
            {**key, "status": "accepted"}).sort("decided_at", -1).limit(1).to_list(length=1)
        if accepted and accepted[0].get("decided_at"):
            group = [r for r in group if r["created_at"] > accepted[0]["decided_at"]]
        item_ids = list(dict.fromkeys(r["ref_id"] for r in group))
        count = len(item_ids)
        if count < PROPOSAL_MIN:
            continue
        if await db[PROPOSALS].find_one({**key, "status": "open"}):
            continue
        rejected = await db[PROPOSALS].find(
            {**key, "status": "rejected", "decided_at": {"$gte": now - REJECT_COOLDOWN}}
        ).sort("decided_at", -1).limit(1).to_list(length=1)
        if rejected and count < 2 * int(rejected[0].get("count_at_rejection") or 0):
            continue

        docs = await db[sstore.SUGGESTIONS].find({"_id": {"$in": item_ids}}).to_list(length=None)
        change = derive_change(rule, reason, docs)
        if change is None:
            continue
        try:
            effect = await replay_effect(db, mongo_client, agent, rule, apply_change(rule, change), docs, end=end)
        except Exception as e:
            logger.error("learning: replay for proposal on rule %s failed: %s", rule_id, e, exc_info=True)
            effect = None
        proposal = {
            "_id": uuid.uuid4().hex,
            "agent_id": agent_id,
            "rule_id": rule_id,
            "rule_kind": rule.get("kind"),
            "rule_version": rule.get("version"),
            "reason_code": reason,
            **change,
            "evidence": {"reason_code": reason, "count": count, "item_ids": item_ids},
            "replay_effect": effect,
            "status": "open",
            "created_at": now,
        }
        await db[PROPOSALS].insert_one(proposal)
        opened.append(proposal)
    return opened


async def _proposal(db, agent_id: str, proposal_id: str) -> dict:
    p = await db[PROPOSALS].find_one({"_id": proposal_id, "agent_id": agent_id})
    if p is None:
        raise ProposalNotFound(proposal_id)
    if p.get("status") != "open":
        raise ProposalClosed(p.get("status"))
    return p


def _change_text(p: dict) -> str:
    parts = [f"{k} {p['before'].get(k)} → {v}" for k, v in (p.get("after") or {}).items()]
    text = ", ".join(parts)
    if p.get("kind") == "client_override":
        text += f" for {p.get('client_name') or p.get('client_group_id')}"
    return text


async def accept_proposal(db, agent: dict, proposal_id: str, *, now: datetime | None = None) -> tuple[dict, dict]:
    """Apply the proposal through update_rule (a new rule version) and log it.
    Returns (proposal, rule). Raises ProposalNotFound / ProposalClosed /
    RuleChanged (the rule's version moved since the proposal) / ValueError (the change no longer validates)."""
    now = now or _now()
    agent_id = agent["_id"]
    p = await _proposal(db, agent_id, proposal_id)
    rule = await db[RULES].find_one({"_id": p["rule_id"], "agent_id": agent_id})
    if rule is None:
        raise ProposalNotFound(proposal_id)
    if p.get("rule_version") is not None and rule.get("version") != p["rule_version"]:
        # Edited since the proposal was made: its before/after no longer describe the rule.
        raise RuleChanged(proposal_id)
    changed = apply_change(rule, p)
    kind = rule["kind"]
    if p["kind"] == "client_override":
        err = await validation.owned_groups_error(db, agent["user_id"], [p["client_group_id"]]) \
            or validation.params_error(kind, changed["client_overrides"][p["client_group_id"]])
    else:
        err = validation.params_error(kind, changed["params"])
    if err:
        raise ValueError(err)

    # Claim first so a double click applies the change once.
    claimed = await db[PROPOSALS].find_one_and_update(
        {"_id": proposal_id, "agent_id": agent_id, "status": "open"},
        {"$set": {"status": "accepted", "decided_at": now}}, return_document=ReturnDocument.AFTER)
    if claimed is None:
        raise ProposalClosed("accepted")
    try:
        if p["kind"] == "client_override":
            updated = await update_rule(db, agent_id, rule["_id"], client_overrides=changed["client_overrides"])
        else:
            updated = await update_rule(db, agent_id, rule["_id"], params=changed["params"])
    except BaseException:
        await db[PROPOSALS].update_one({"_id": proposal_id}, {"$set": {"status": "open"},
                                                              "$unset": {"decided_at": ""}})
        raise
    await db[PROPOSALS].update_one({"_id": proposal_id}, {"$set": {"applied_version": updated["version"]}})
    claimed["applied_version"] = updated["version"]
    try:
        await sstore.log_activity(
            db, agent["user_id"], actor=sstore.ACTOR_USER, kind=KIND_PROPOSAL_ACCEPTED,
            title=f"{_label(kind)} updated: {_change_text(p)}", label="Accepted by you",
            client_name=p.get("client_name"), client_group_id=p.get("client_group_id"),
            ref_id=proposal_id, agent_id=agent_id, rule_id=rule["_id"], reason_code=p.get("reason_code"))
    except Exception as e:
        logger.error("learning: could not log accepted proposal %s: %s", proposal_id, e)
    return claimed, updated


async def reject_proposal(db, agent: dict, proposal_id: str, *, now: datetime | None = None) -> dict:
    """Close the proposal, keeping its count for the 30-day / doubling rule."""
    now = now or _now()
    p = await _proposal(db, agent["_id"], proposal_id)
    done = await db[PROPOSALS].find_one_and_update(
        {"_id": proposal_id, "agent_id": agent["_id"], "status": "open"},
        {"$set": {"status": "rejected", "decided_at": now,
                  "count_at_rejection": int((p.get("evidence") or {}).get("count") or 0)}},
        return_document=ReturnDocument.AFTER)
    if done is None:
        raise ProposalClosed("rejected")
    return done
