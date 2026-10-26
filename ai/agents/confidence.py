"""
ai/agents/confidence.py
-----------------------
The agent's confidence score (0-100): coverage + practice.

  coverage  +10 for each of targets / pause / revive / flags (max 40).
            targets: every watched client has a CPL target > 0 (no watched
            clients -> false). The other three come from training.topics.
  practice  60 x wilson_lower(agreed, answered), 0 when answered < 6.
            Before activation: the user's practice cases (agent_practice).
            After activation (status "active"): the last 30 decisions on
            ask-mode rules (approved = success, denied or undone = failure),
            once there are at least 6 of them; until then the practice cases
            keep counting, so the score does not drop at launch.

ready = score >= 80 and every watched client has a target.

Re-scoring is lazy: a case the user said they were right about (user_right) is
re-run on its stored snapshot whenever its rule has a newer version than when
it was answered; if Birdy's answer now matches the user's, it becomes agreed.
"""

import logging
import math

from ai.agents import replay
from ai.agents.store import PRACTICE, list_memory, list_rules
from ai.suggestions import store as sstore

logger = logging.getLogger(__name__)

answer_label = replay.answer_label  # one mapping, shared with the sampler

Z = 1.28
MIN_ANSWERED = 6
READY_SCORE = 80
TOPIC_POINTS = 10
PRACTICE_POINTS = 60
ROLLING_DECISIONS = 30
TOPICS = ("targets", "pause", "revive", "flags")
TOPIC_MISSING = {
    "pause": "Tell Birdy when to pause ads",
    "revive": "Tell Birdy when to revive old winners",
    "flags": "Tell Birdy what to flag",
}
AGREED = ("agreed", "birdy_right")
ANSWERED = ("agreed", "birdy_right", "user_right", "pending")
ASK_KINDS = ("pause_over_target", "revive_winner")
FLAG_KINDS = ("flag_no_spend", "flag_declining")


def wilson_lower(k: int, n: int, z: float = Z) -> float:
    """Lower bound of the Wilson score interval for k successes out of n."""
    if n <= 0:
        return 0.0
    p = k / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre - spread) / (1 + z2 / n)


def _practice_points(agreed: int, answered: int) -> float:
    if answered < MIN_ANSWERED:
        return 0.0
    return PRACTICE_POINTS * wilson_lower(agreed, answered)


async def _watched_targets(db, agent: dict) -> tuple[bool, list[str]]:
    """(every watched client has a CPL target, names of those without one)."""
    gids = list((agent.get("scope") or {}).get("client_group_ids") or [])
    if not gids:
        return False, []
    groups = {g["id"]: g for g in await db["client_groups"].find(
        {"user_id": agent["user_id"], "id": {"$in": gids}}).to_list(length=None)}
    lacking = []
    for gid in gids:
        g = groups.get(gid) or {}
        try:
            cpl = float((g.get("targets") or {}).get("cpl") or 0)
        except (TypeError, ValueError):
            cpl = 0.0
        if cpl <= 0:
            lacking.append(g.get("name") or gid)
    return not lacking, lacking


async def _rescore_user_right(db, agent: dict, docs: list[dict]) -> None:
    """Flip user_right cases to agreed when the edited rule now answers like the user."""
    candidates = [d for d in docs if d.get("resolution") == "user_right" and d.get("rule_id")]
    if not candidates:
        return
    rules = {r["_id"]: r for r in await list_rules(db, agent["_id"])}
    memories = None
    for doc in candidates:
        rule = rules.get(doc["rule_id"])
        if rule is None:
            continue
        version = int(rule.get("version") or 0)
        if version <= int(doc.get("rule_version_at_answer") or 0) or version <= int(doc.get("rescored_version") or 0):
            continue
        if memories is None:
            memories = await list_memory(db, agent["_id"], active_only=True)
        try:
            answer = replay.rescore_case(agent, rule, memories, doc.get("case") or {})
        except Exception as e:
            logger.error("confidence: re-scoring %s failed: %s", doc["_id"], e, exc_info=True)
            continue
        update = {"rescored_version": version}
        if answer is not None and answer == doc.get("user_answer"):
            update["resolution"] = "agreed"
            doc["resolution"] = "agreed"
        await db[PRACTICE].update_one({"_id": doc["_id"], "resolution": "user_right"}, {"$set": update})


async def _practice_counts(db, agent: dict) -> tuple[int, int]:
    docs = await db[PRACTICE].find({"agent_id": agent["_id"], "resolution": {"$in": list(ANSWERED)}}).to_list(
        length=None)
    await _rescore_user_right(db, agent, docs)
    return sum(1 for d in docs if d.get("resolution") in AGREED), len(docs)


async def _decision_counts(db, agent: dict) -> tuple[int, int]:
    """(approved, decided) over the last 30 decisions on ask-mode rules.
    An approval that was later undone counts as a failure."""
    ask_ids = [r["_id"] for r in await list_rules(db, agent["_id"]) if r.get("mode") == "ask"]
    if not ask_ids:
        return 0, 0
    rows = await db[sstore.ACTIVITY].find({
        "agent_id": agent["_id"],
        "rule_id": {"$in": ask_ids},
        "$or": [
            {"kind": sstore.KIND_ACTION_APPLIED, "actor": sstore.ACTOR_USER},
            {"kind": sstore.KIND_SUGGESTION_DISMISSED},
        ],
    }).sort("created_at", -1).limit(ROLLING_DECISIONS).to_list(length=ROLLING_DECISIONS)
    applied_refs = [r["ref_id"] for r in rows if r["kind"] == sstore.KIND_ACTION_APPLIED and r.get("ref_id")]
    undone: set = set()
    if applied_refs:
        undone = {u["ref_id"] for u in await db[sstore.ACTIVITY].find(
            {"user_id": agent["user_id"], "kind": sstore.KIND_ACTION_UNDONE, "ref_id": {"$in": applied_refs}},
            {"ref_id": 1}).to_list(length=None)}
    approved = sum(1 for r in rows if r["kind"] == sstore.KIND_ACTION_APPLIED and r.get("ref_id") not in undone)
    return approved, len(rows)


async def compute_confidence(db, agent: dict) -> dict:
    topics = ((agent.get("training") or {}).get("topics") or {})
    targets_ok, lacking = await _watched_targets(db, agent)
    parts = {"targets": targets_ok, **{t: bool(topics.get(t)) for t in TOPICS if t != "targets"}}
    coverage = TOPIC_POINTS * sum(1 for t in TOPICS if parts[t])

    practice_source = "practice"
    agreed = answered = 0
    if agent.get("status") == "active":
        agreed, answered = await _decision_counts(db, agent)
        if answered >= MIN_ANSWERED:
            practice_source = "decisions"
    if practice_source == "practice":
        agreed, answered = await _practice_counts(db, agent)
    practice = _practice_points(agreed, answered)
    score = round(coverage + practice)
    parts.update(practice_agreed=agreed, practice_answered=answered)

    missing = [TOPIC_MISSING[t] for t in ("pause", "revive", "flags") if not parts[t]]
    if not (agent.get("scope") or {}).get("client_group_ids"):
        missing.append("Choose at least one client to watch")
    missing += [f"Set a CPL target for {name}" for name in lacking]
    if answered < MIN_ANSWERED:
        missing.append(f"Answer at least {MIN_ANSWERED} practice cases")
    if score < READY_SCORE:
        missing.append(f"Get the score to {READY_SCORE}")

    return {
        "score": score,
        "coverage": coverage,
        "practice": round(practice),
        "parts": parts,
        "practice_source": practice_source,
        "ready": score >= READY_SCORE and targets_ok,
        "missing": missing,
    }
