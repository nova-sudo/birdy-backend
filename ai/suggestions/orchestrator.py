"""
ai/suggestions/orchestrator.py
------------------------------
Drives a suggestion "pass": for a user + time window, run every registered
subagent over each of the user's clients, compose copy for each finding, persist
it, and write the activity feed. This is the fixed plumbing — it knows nothing
about *how* any subagent decides.

Two entry points:
  run_pass_for_user(...)  — one user (on-demand refresh, or a unit of cron work).
  run_pass_all_users(...) — every user, within a wall-clock budget (cron).

A "pass" logs an analysis_pass activity per client (so the feed shows what Birdy
looked at and when), then per finding: composes copy, upserts the suggestion, and
logs suggestion_created for genuinely new ones. After each client+window it
reconciles — auto-closing open suggestions the pass no longer reproduces.
"""

import logging
import time

from core.database import DB_NAME
from ai.suggestions.agents import REGISTRY
from ai.suggestions.composer import compose, get_composer_provider
from ai.suggestions.contracts import AnalyzerContext, WINDOWS
from ai.suggestions import store

logger = logging.getLogger(__name__)

# Presets we must project off client_groups.facebook_cache to evaluate a window.
# (Superset across agents; keeps the query small vs. loading the whole cache.)
_WINDOW_LABEL = {"weekly": "weekly", "monthly": "monthly"}
_PROJECTION = {
    "id": 1,
    "user_id": 1,
    "name": 1,
    "ad_account_currency": 1,
    "facebook_cache.last_7d": 1,
    "facebook_cache.last_30d": 1,
}


async def run_pass_for_user(
    db,
    user_id: str,
    window: str,
    *,
    mongo_client=None,
    agents=REGISTRY,
    client_group_ids: list[str] | None = None,
    notify: bool = True,
) -> dict:
    """Run one suggestion pass for a single user + window. Never raises per-client."""
    if window not in WINDOWS:
        raise ValueError(f"unknown window: {window}")

    # Only analyze ACTIVE clients. Missing status defaults to Active (matching the
    # rest of the app's `client_status ?? "Active"`), so we exclude only explicit
    # Inactive clients.
    query = {"user_id": user_id, "client_status": {"$ne": "Inactive"}}
    if client_group_ids:
        query["id"] = {"$in": client_group_ids}
    groups = await db["client_groups"].find(query, _PROJECTION).to_list(length=500)

    # One provider per user (BYOK, else global key, else None → template copy).
    provider = await get_composer_provider(user_id, db)
    ctx = AnalyzerContext(db=db, user_id=user_id, mongo_client=mongo_client)

    stats = {"clients": 0, "analyzed": 0, "findings": 0, "created": 0, "resolved": 0}
    created_docs: list[dict] = []  # brand-new suggestions, to post to Slack after the pass
    updated_docs: list[dict] = []  # same-window refreshes with a Slack message, to keep in sync
    keep_by_agent: dict[str, set] = {}  # agent → union of kept suggestion ids across ALL clients

    for group in groups:
        stats["clients"] += 1
        client_name = group.get("name") or "Client"
        client_id = group.get("id")

        # Skip clients with no data for this window — nothing to analyze.
        if not _has_window_data(group, window):
            continue
        stats["analyzed"] += 1

        await store.log_activity(
            db, user_id,
            actor=store.ACTOR_BIRDY,
            kind=store.KIND_ANALYSIS_PASS,
            title=f"Analyzed {client_name} — {_WINDOW_LABEL.get(window, window)} performance",
            client_name=client_name,
            client_group_id=client_id,
            window=window,
            label="Analyzed by Birdy",
        )

        for agent in agents:
            try:
                findings = await agent.analyze(ctx, group, window)
            except Exception as e:
                logger.error("suggestions: %s.analyze failed for %s/%s: %s",
                             getattr(agent, "name", agent), user_id, client_id, e, exc_info=True)
                continue

            agent_name = getattr(agent, "name", "")
            for finding in findings:
                stats["findings"] += 1
                composed = await compose(provider, finding)
                finding.title = composed["title"]
                finding.description = composed["description"]
                doc, created, content_changed = await store.upsert_finding(
                    db, user_id, finding, composer=composed["composer"]
                )
                if doc is None:
                    continue  # suppressed (applied / recently dismissed)
                keep_by_agent.setdefault(agent_name, set()).add(doc["_id"])
                if created:
                    stats["created"] += 1
                    created_docs.append(doc)
                    await store.log_activity(
                        db, user_id,
                        actor=store.ACTOR_BIRDY,
                        kind=store.KIND_SUGGESTION_CREATED,
                        title=finding.title,
                        client_name=client_name,
                        client_group_id=client_id,
                        ref_id=doc["_id"],
                        window=window,
                        label="Suggested by Birdy",
                    )
                elif content_changed and doc.get("slack_ts"):
                    updated_docs.append(doc)  # keep the existing Slack message in sync

    # Reconcile once across the WHOLE pass (not per client-group): close open
    # suggestions this agent no longer produced for this window in ANY client, so
    # a shared ad seen under several client-groups isn't wrongly auto-closed.
    for agent in agents:
        agent_name = getattr(agent, "name", "")
        closed = await store.close_stale_open(
            db, user_id, agent_name, window, keep_by_agent.get(agent_name, set())
        )
        stats["resolved"] += closed

    # Push brand-new suggestions to the user's Slack channel (no-op if the user
    # hasn't connected Slack or chosen a channel).
    if notify and created_docs:
        try:
            from services.slack_suggestion_notifier import post_new_suggestions
            posted = await post_new_suggestions(db, user_id, created_docs)
            if posted:
                stats["slack_posted"] = posted
        except Exception as e:
            logger.warning("suggestions: Slack notify failed for %s: %s", user_id, e)

    # Keep already-posted Slack messages in sync when their content is refreshed.
    if notify and updated_docs:
        try:
            from services.slack_suggestion_notifier import update_suggestions
            await update_suggestions(db, user_id, updated_docs)
        except Exception as e:
            logger.warning("suggestions: Slack sync failed for %s: %s", user_id, e)

    logger.info("suggestions pass user=%s window=%s → %s", user_id, window, stats)
    return stats


def _has_window_data(group: dict, window: str) -> bool:
    preset = "last_7d" if window == "weekly" else "last_30d"
    cache = (group.get("facebook_cache") or {}).get(preset) or {}
    return bool(cache.get("ads"))


async def run_pass_all_users(
    mongo_client,
    window: str,
    *,
    budget_seconds: float = 240.0,
    max_users: int | None = None,
) -> dict:
    """
    Run a pass for every user with clients, within a wall-clock budget so it fits
    a single serverless invocation. Users not reached this run are picked up by
    the next cron tick (the pass is idempotent).
    """
    db = mongo_client[DB_NAME]
    start = time.monotonic()
    user_ids = await db["client_groups"].distinct("user_id")
    totals = {"users": 0, "clients": 0, "analyzed": 0, "findings": 0, "created": 0, "resolved": 0}

    for i, user_id in enumerate(user_ids):
        if max_users is not None and i >= max_users:
            break
        if time.monotonic() - start > budget_seconds:
            logger.info("suggestions: budget hit after %d/%d users", i, len(user_ids))
            totals["budget_hit"] = True
            break
        try:
            s = await run_pass_for_user(db, user_id, window, mongo_client=mongo_client)
        except Exception as e:
            logger.error("suggestions: pass failed for user %s: %s", user_id, e, exc_info=True)
            continue
        totals["users"] += 1
        for k in ("clients", "analyzed", "findings", "created", "resolved"):
            totals[k] += s.get(k, 0)

    totals["elapsed_seconds"] = round(time.monotonic() - start, 2)
    totals["total_users"] = len(user_ids)
    return totals
