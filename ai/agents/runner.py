"""
ai/agents/runner.py
-------------------
Runs one BirdyHub agent: every enabled rule over every watched client.

  live    refresh Meta state, evaluate, upsert work-list items (window "agent"),
          auto-apply auto-mode rules as Birdy, close stale items, post new
          items to Slack, stamp last_run_at.
  replay  read existing state only: no Meta calls, no writes. Near misses may
          be requested (dry run / practice cases).

Errors are per client and never fatal. Clients — and auto-applies within a
client — run strictly one at a time: the guardrails' last-live-ad and daily
pause-cap checks read state the previous apply wrote.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from ai.agents import history as ad_history
from ai.agents import memory_filters
from ai.agents.defaults import DEFAULT_GUARDRAILS, DEFAULT_PERMISSIONS, RULE_KINDS
from ai.agents.evaluators import REGISTRY
from ai.agents.meta_state import MetaTokenError, backfill_paused_at, refresh_meta_state
from ai.agents.snapshot import (
    AdDay,
    AdsetState,
    AdState,
    ClientCtx,
    load_ad_days,
    load_client_ctx,
    load_state,
)
from ai.agents.store import AGENTS, list_memory, list_rules
from ai.suggestions import actions
from ai.suggestions import store as sstore
from ai.suggestions.contracts import SEVERITY_MEDIUM, Evidence, Finding

logger = logging.getLogger(__name__)

HISTORY_DAYS = 365          # evaluators compute lifetime spend / patience from these rows
AGENT_WINDOW = "agent"
MISSING_TARGET_AGENT = "media_buying:missing_target"
TARGET_ONLY_RULES = {"revive_winner"}
APPLIED_OUTCOMES = {"applied", "already_in_meta", "partial"}
NEUTRAL_OUTCOMES = {"already", "closed"}  # someone else decided or closed it / holds the claim


@dataclass
class RunResult:
    findings: list[Finding] = field(default_factory=list)
    created: int = 0
    auto_applied: int = 0
    blocked: int = 0
    skipped: list[str] = field(default_factory=list)
    stale: bool = False


def _rule_agent(kind: str) -> str:
    return f"media_buying:{kind}"


def _missing_target_finding(agent: dict, ctx: ClientCtx) -> Finding:
    name = ctx.name or "this client"
    return Finding(
        agent=MISSING_TARGET_AGENT,
        client_group_id=ctx.client_group_id,
        client_name=ctx.name,
        severity=SEVERITY_MEDIUM,
        title=f"Set a CPL target for {name}",
        description=("Without a cost-per-lead target Birdy only pauses ads with no leads. "
                     "Over-target pauses and reviving old winners are skipped until you set one."),
        evidence=Evidence(window=AGENT_WINDOW, raw={"reason": "missing_target"}),
        action=None,
        confidence=1.0,
        icon="target",
        agent_id=str(agent.get("_id") or ""),
    )


def evaluate_loaded(agent: dict, rules: list[dict], memories: list[dict], ctx: ClientCtx,
                    ad_days: dict[str, list[AdDay]], ads: dict[str, AdState], adsets: dict[str, AdsetState],
                    *, as_of: date, near_miss: bool) -> list[Finding]:
    """Findings for one client from preloaded data: the missing-target advisory,
    every enabled rule's evaluator, then memory filters. Pure: no I/O. Shared by
    run_agent and the dry-run replay (ai.agents.replay) so they cannot drift."""
    findings: list[Finding] = []
    protected_tags = {**DEFAULT_GUARDRAILS, **(agent.get("guardrails") or {})}.get("protected_tags") or []
    if isinstance(protected_tags, str):
        protected_tags = [protected_tags]
    adsets_allowed = bool({**DEFAULT_PERMISSIONS, **(agent.get("permissions") or {})}.get("adsets"))
    if ctx.target_cpl is None:
        findings.append(_missing_target_finding(agent, ctx))
    for rule in rules:
        kind = rule.get("kind")
        evaluate = REGISTRY.get(kind)
        if evaluate is None:
            continue
        if ctx.target_cpl is None and kind in TARGET_ONLY_RULES:
            continue
        rule = {**rule, "agent_id": rule.get("agent_id") or agent.get("_id"),
                "protected_tags": protected_tags, "adsets_allowed": adsets_allowed}
        findings.extend(evaluate(rule, ctx, ad_days, ads, adsets, as_of, near_miss=near_miss))
    return memory_filters.apply(findings, memories, as_of=as_of)


async def _evaluate_client(db, mongo_client, agent: dict, rules: list[dict], memories: list[dict],
                           gid: str, *, as_of: date, live: bool, near_miss: bool, result: RunResult):
    """(ctx, findings) for one client, or None when it was skipped."""
    user_id = agent["user_id"]
    group = await db["client_groups"].find_one({"id": gid, "user_id": user_id}) or {}
    account = group.get("meta_ad_account_id")
    if not account:
        result.skipped.append(f"no_meta:{gid}")
        return None

    if live and not ad_history.is_ready((await ad_history.statuses(db, [gid]))[gid]):
        # Still pulling this client's ad history: a run on partial data would call
        # real problems "no findings" or act on a thin window.
        result.skipped.append(f"history:{gid}")
        return None

    if live:
        await refresh_meta_state(db, mongo_client, user_id, gid, account, today=as_of)
        await backfill_paused_at(db, user_id, gid)

    ctx = await load_client_ctx(db, user_id, gid, agent)
    ad_days = await load_ad_days(db, user_id, gid, as_of - timedelta(days=HISTORY_DAYS), as_of)
    ads, adsets = await load_state(db, gid)

    latest = max((rows[-1].day for rows in ad_days.values() if rows), default=None)
    if latest is not None and latest < as_of - timedelta(days=1):
        result.stale = True

    findings = evaluate_loaded(agent, rules, memories, ctx, ad_days, ads, adsets,
                               as_of=as_of, near_miss=near_miss)
    if live:
        findings = [f for f in findings if not f.near_miss]
    return ctx, findings


def _silent_auto_run(doc: dict, rules_by_id: dict) -> bool:
    """Birdy auto-applied this item on a rule graduated with auto_silent."""
    rule = rules_by_id.get(str(doc.get("rule_id"))) or {}
    return (doc.get("status") == sstore.STATUS_APPLIED
            and (doc.get("decided_by") or {}).get("actor") == sstore.ACTOR_BIRDY
            and rule.get("notify") is False)


async def _auto_apply(db, mongo_client, user_id: str, suggestion_id: str, result: RunResult) -> None:
    try:
        res = await actions.apply_suggestion(db, mongo_client, user_id, suggestion_id,
                                             actor=sstore.ACTOR_BIRDY, source="agent")
        outcome = (res or {}).get("outcome")
    except Exception as e:
        logger.error("agent runner: auto-apply raised for %s: %s", suggestion_id, e, exc_info=True)
        outcome = None
    if outcome == "blocked":
        result.blocked += 1
    elif outcome in APPLIED_OUTCOMES:
        result.auto_applied += 1
    elif outcome not in NEUTRAL_OUTCOMES:
        await db[sstore.SUGGESTIONS].update_one(
            {"_id": suggestion_id, "user_id": user_id},
            {"$set": {"auto_failed": True, "updated_at": datetime.utcnow()}},
        )


async def run_agent(db, mongo_client, agent: dict, *, as_of: date,
                    mode: Literal["live", "replay"] = "live", near_miss: bool = False,
                    force_stale: bool = False) -> RunResult:
    if mode not in ("live", "replay"):
        raise ValueError(f"unknown mode: {mode}")
    live = mode == "live"
    user_id = agent["user_id"]
    agent_id = agent["_id"]
    gids = list((agent.get("scope") or {}).get("client_group_ids") or [])
    rules = [r for r in await list_rules(db, agent_id) if r.get("enabled", True)]
    rules_by_id = {str(r["_id"]): r for r in rules}
    memories = await list_memory(db, agent_id, active_only=True)
    result = RunResult(stale=force_stale)  # force_stale: caller knows the data is old -> no auto-apply

    # 1. Evaluate every client (live: refresh its Meta state first). The stale
    #    flag must be known before anything is auto-applied.
    evaluated: list[tuple[str, ClientCtx, list[Finding]]] = []
    for gid in gids:
        try:
            out = await _evaluate_client(db, mongo_client, agent, rules, memories, gid, as_of=as_of,
                                         live=live, near_miss=near_miss and not live, result=result)
        except MetaTokenError as e:
            logger.warning("agent runner: Meta token problem for %s/%s: %s", user_id, gid, e)
            result.skipped.append(f"meta_token:{gid}")
            continue
        except Exception as e:
            logger.error("agent runner: client %s/%s failed: %s", user_id, gid, e, exc_info=True)
            result.skipped.append(f"error:{gid}")
            continue
        if out is None:
            continue
        ctx, findings = out
        evaluated.append((gid, ctx, findings))
        result.findings.extend(findings)

    if not live:
        return result

    # 2. Persist + auto-apply, one client at a time, one apply at a time.
    keep: dict[str, set] = {}
    done_gids: list[str] = []
    created_ids: list[str] = []
    updated_docs: list[dict] = []
    for gid, ctx, findings in evaluated:
        try:
            to_apply: list[str] = []
            for f in findings:
                f.evidence.window = AGENT_WINDOW
                doc, created, changed = await sstore.upsert_finding(db, user_id, f)
                if doc is None:
                    continue  # suppressed (applied / recently dismissed)
                keep.setdefault(f.agent, set()).add(doc["_id"])
                rule = rules_by_id.get(f.rule_id)
                rule_mode = rule.get("mode") if rule else None
                if rule_mode:
                    await db[sstore.SUGGESTIONS].update_one({"_id": doc["_id"]}, {"$set": {"rule_mode": rule_mode}})
                if created:
                    result.created += 1
                    created_ids.append(doc["_id"])
                    await sstore.log_activity(
                        db, user_id, actor=sstore.ACTOR_BIRDY, kind=sstore.KIND_SUGGESTION_CREATED,
                        title=f.title, client_name=ctx.name, client_group_id=gid, ref_id=doc["_id"],
                        window=AGENT_WINDOW, label="Suggested by Birdy",
                        agent_id=agent_id, rule_id=f.rule_id or None,
                    )
                elif changed and doc.get("slack_ts"):
                    updated_docs.append(doc)
                if (rule_mode == "auto" and f.action is not None and not result.stale
                        and doc.get("status") == sstore.STATUS_OPEN and not doc.get("auto_failed")
                        and not doc.get("undone_at")):
                    to_apply.append(doc["_id"])
            for sid in to_apply:
                await _auto_apply(db, mongo_client, user_id, sid, result)
            done_gids.append(gid)
        except Exception as e:
            logger.error("agent runner: writing items for %s/%s failed: %s", user_id, gid, e, exc_info=True)
            result.skipped.append(f"error:{gid}")

    # 3. Close what this run no longer produces — per rule, only for clients
    #    evaluated and written cleanly, only this agent's own items.
    for agent_name in [_rule_agent(k) for k in RULE_KINDS] + [MISSING_TARGET_AGENT]:
        try:
            await sstore.close_stale_open(db, user_id, agent_name, AGENT_WINDOW, keep.get(agent_name, set()),
                                          client_group_ids=done_gids, agent_id=agent_id)
        except Exception as e:
            logger.error("agent runner: close-stale failed for %s/%s: %s", user_id, agent_name, e)

    # 4. Slack: post brand-new items (re-read, so auto-applied ones show applied).
    if created_ids:
        try:
            from services.slack_suggestion_notifier import post_new_suggestions
            docs = await db[sstore.SUGGESTIONS].find({"_id": {"$in": created_ids}}).to_list(length=None)
            order = {sid: i for i, sid in enumerate(created_ids)}
            docs.sort(key=lambda d: order.get(d["_id"], 0))
            # "Auto-run silently": what Birdy ran itself on such a rule stays off
            # Slack (it still shows in the work list's auto-run section).
            docs = [d for d in docs if not _silent_auto_run(d, rules_by_id)]
            if docs:
                await post_new_suggestions(db, user_id, docs)
        except Exception as e:
            logger.warning("agent runner: Slack notify failed for %s: %s", user_id, e)
    if updated_docs:
        try:
            from services.slack_suggestion_notifier import update_suggestions
            await update_suggestions(db, user_id, updated_docs)
        except Exception as e:
            logger.warning("agent runner: Slack sync failed for %s: %s", user_id, e)

    await db[AGENTS].update_one({"_id": agent_id}, {"$set": {"last_run_at": datetime.utcnow()}})
    logger.info("agent run user=%s agent=%s → created=%d auto=%d blocked=%d skipped=%s stale=%s",
                user_id, agent_id, result.created, result.auto_applied, result.blocked,
                result.skipped, result.stale)
    return result
