"""
ai/suggestions/actions.py
-------------------------
Apply / dismiss a suggestion — the shared core used by BOTH the dashboard HTTP
endpoints (routers/dashboard.py) and the Slack button handler
(routers/slack_interactions.py). Extracted so "Do it for me" behaves identically
whether it's clicked in the app or in Slack.

These functions never raise for expected cases; they return an outcome dict so
each caller can map it to its own surface (HTTP status vs. a Slack message):

    {"ok": bool, "outcome": "applied"|"partial"|"already"|"already_in_meta"
                            |"blocked"|"busy"|"dismissed"|"not_found"|"failed"|"noop",
     "doc": <suggestion doc or None>, "succeeded": [...], "failed": [...],
     "detail": <str, on failure>,
     "decided_by": <dict, on "already">, "blocked_reason": <{code, message}, on "blocked">}

  already          someone else already decided it (or is applying it right now)
  already_in_meta  every target was already in the desired state (or deleted) in
                   Meta, so nothing was written; the suggestion is marked applied
  blocked          an agent guardrail stopped it; it stays open with blocked_reason
  busy             (dismiss) an apply holds a live claim on it right now; nothing
                   changed — try again in a moment

Apply order (see apply_suggestion): claim → guardrails → re-read → write →
mark_applied. The claim comes first so concurrent applies make one Meta write.
"""

import logging
from datetime import datetime

from ai.agents.guardrails import FLAG_ONLY, FLAG_ONLY_MESSAGE, agent_tz, check_guardrails, local_today
from ai.agents.meta_state import record_status_write
from ai.agents.store import get_agent
from ai.suggestions import store
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from services.meta_status_service import MetaStatusError, get_object_state, set_object_status

logger = logging.getLogger(__name__)


async def execute_action(mongo_client, user_id: str, action: dict) -> dict:
    """Execute a suggestion's action against Meta. Returns {succeeded, failed}."""
    atype = action.get("type")
    targets = action.get("targets") or []
    if atype not in (ACTION_PAUSE_ADS, ACTION_ENABLE_ADS):
        return {"succeeded": [], "failed": [{"object_id": None, "error": f"Unsupported action type: {atype}"}]}

    desired = "PAUSED" if atype == ACTION_PAUSE_ADS else "ACTIVE"
    succeeded, failed = [], []
    for t in targets:
        object_id = t.get("object_id")
        object_type = t.get("object_type", "ad")
        try:
            await set_object_status(user_id, object_id, object_type, desired, mongo_client)
            succeeded.append(object_id)
        except MetaStatusError as e:
            logger.warning("apply: %s %s failed: %s", desired, object_id, e.message)
            failed.append({"object_id": object_id, "error": e.message})
    return {"succeeded": succeeded, "failed": failed}


async def _sync_slack(db, user_id: str, suggestion_id: str, by: str | None = None) -> None:
    """
    Best-effort: re-render the suggestion's Slack card so it reflects the new
    state. Called after EVERY state change, which is what keeps an action taken
    in the dashboard from leaving a stale card in Slack (and vice-versa).
    """
    try:
        from services.slack_suggestion_notifier import sync_suggestion_message
        fresh = await store.get_suggestion(db, user_id, suggestion_id)
        if fresh:
            await sync_suggestion_message(db, user_id, fresh, by=by)
    except Exception as e:
        logger.warning("actions: slack sync failed for %s: %s", suggestion_id, e)


# A pause is only "already done" when the object ITSELF is off. ADSET_PAUSED,
# CAMPAIGN_PAUSED, PENDING_REVIEW, WITH_ISSUES etc. leave the ad's own status
# ACTIVE, so it would start spending again once its parent is re-enabled or it
# clears review: those still get written.
_PAUSED_DONE = frozenset({"PAUSED", "ARCHIVED", "DELETED"})


def _is_desired(atype: str, state: dict | None) -> bool:
    """
    Is the object already where this action would put it? None = deleted in
    Meta, which counts as done.
      pause   decided from the configured `status` (PAUSED / ARCHIVED /
              DELETED); if Meta omitted it, effective_status must be one of those.
      enable  only an effective ACTIVE counts — ADSET_PAUSED etc. are not.
    """
    if state is None:
        return True
    if atype == ACTION_PAUSE_ADS:
        own = state.get("status")
        if own:
            return own in _PAUSED_DONE
        return state.get("effective_status") in _PAUSED_DONE
    return (state.get("effective_status") or state.get("status")) == "ACTIVE"


async def _targets_needing_write(mongo_client, user_id: str, action: dict) -> list[dict]:
    """
    Re-read each target in Meta right before writing and keep only those not
    already in the desired state. A failed read keeps the target (fail-open to
    the write, which is idempotent and surfaces the real error if there is one).
    """
    atype = action.get("type")
    pending = []
    for t in action.get("targets") or []:
        try:
            state = await get_object_state(user_id, t.get("object_id"),
                                           t.get("object_type", "ad"), mongo_client)
        except Exception as e:
            logger.warning("apply: re-read of %s failed, writing anyway: %s", t.get("object_id"), e)
            pending.append(t)
            continue
        if not _is_desired(atype, state):
            pending.append(t)
    return pending


async def _record_meta_state(db, user_id: str, doc: dict, targets: list[dict],
                             object_ids: list, desired: str, *, agent: dict | None = None) -> None:
    """
    Best-effort: mirror what we just wrote into meta_ad_state so the next
    guardrail check (protect_last_live_ad) sees it before the next refresh.
    """
    try:
        types = {str(t.get("object_id")): t.get("object_type", "ad") for t in targets}
        ad_ids = [str(o) for o in object_ids if types.get(str(o), "ad") == "ad"]
        if not ad_ids or not doc.get("client_group_id"):
            return
        if agent is None and doc.get("agent_id"):
            agent = await get_agent(db, user_id, doc["agent_id"])
        today = local_today(datetime.utcnow(), agent_tz(agent))
        await record_status_write(db, doc["client_group_id"], ad_ids, desired, today=today)
    except Exception as e:
        logger.warning("apply: meta_ad_state write-back failed for %s: %s", doc.get("_id"), e)


async def _learn(db, doc: dict, decision: str, **kw) -> dict | None:
    """Best-effort: feed a decision on an agent item to the learning engine.
    A learning failure never fails the user's apply / deny / undo."""
    if not (doc.get("agent_id") and doc.get("rule_id")):
        return None
    try:
        from ai.agents import learning  # lazy: learning -> replay -> runner -> actions
        return await learning.record_decision(db, doc["agent_id"], doc["rule_id"], decision, doc=doc, **kw)
    except Exception as e:
        logger.error("actions: learning (%s) failed for %s: %s", decision, doc.get("_id"), e, exc_info=True)
        return None


async def _already(db, user_id: str, suggestion_id: str, fallback: dict) -> dict:
    cur = await store.get_suggestion(db, user_id, suggestion_id) or fallback
    if not store.has_live_claim(cur):
        # Re-render a stale card (e.g. Apply on a resolved item) to the real state.
        # Skipped mid-apply: the winner syncs when it finishes.
        await _sync_slack(db, user_id, suggestion_id)
    return {"ok": True, "outcome": "already", "doc": cur, "decided_by": cur.get("decided_by")}


async def apply_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                           actor: str = store.ACTOR_USER, source: str | None = None,
                           by: str | None = None) -> dict:
    """
    Approve + execute a suggestion. actor=ACTOR_USER means a human approved it
    ("Approved by you"); actor=ACTOR_BIRDY is an autonomous agent run.

    Order matters — this writes to live ad accounts:
      1. claim      atomically reserve the open suggestion (applying_at), so of
                    N concurrent applies exactly one reaches Meta; losers get
                    "already". A claim older than 5 minutes may be taken over.
      2. guardrails agent suggestions only; blocked → release, stay open.
      3. re-read    each target's live status; skip what's already done.
      4. write      set_object_status for the remaining targets.
      5. mark       atomic open → applied (clears the claim).
    Blocked, failed or raising → the claim is released so it can be retried.
    """
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        return {"ok": False, "outcome": "not_found", "doc": None}
    if doc.get("status") == store.STATUS_APPLIED:
        return {"ok": True, "outcome": "already", "doc": doc, "decided_by": doc.get("decided_by")}

    token = await store.claim_for_apply(db, user_id, suggestion_id)
    if not token:
        return await _already(db, user_id, suggestion_id, doc)

    try:
        return await _apply_claimed(db, mongo_client, user_id, suggestion_id, doc, token,
                                    actor=actor, source=source, by=by)
    except BaseException:
        await store.release_claim(db, user_id, suggestion_id, token)
        raise


async def _apply_claimed(db, mongo_client, user_id: str, suggestion_id: str, doc: dict, token: str,
                         *, actor: str, source: str | None, by: str | None) -> dict:
    action = doc.get("action")
    atype = (action or {}).get("type")
    agent_id = doc.get("agent_id")
    agent = None

    # 1b. A flag-mode rule only flags: its item never writes, whoever asks.
    # An item with nothing to write is just acknowledged (marked applied).
    if doc.get("rule_mode") == "flag" and action:
        reason = {"code": FLAG_ONLY, "message": FLAG_ONLY_MESSAGE}
        await store.release_claim(db, user_id, suggestion_id, token, blocked_reason=reason)
        await _sync_slack(db, user_id, suggestion_id, by=by)
        return {"ok": False, "outcome": "blocked", "doc": doc, "blocked_reason": reason,
                "detail": reason["message"]}

    # 2. Guardrails — agent suggestions only.
    if agent_id and action:
        agent = await get_agent(db, user_id, agent_id)
        if agent is None:
            reason = {"code": "no_permission", "message": "The agent behind this suggestion no longer exists."}
        else:
            g = await check_guardrails(db, agent, doc, now_utc=datetime.utcnow())
            reason = None if g.ok else {"code": g.reason, "message": g.message}
        if reason:
            await store.release_claim(db, user_id, suggestion_id, token, blocked_reason=reason)
            await _sync_slack(db, user_id, suggestion_id, by=by)
            return {"ok": False, "outcome": "blocked", "doc": doc, "blocked_reason": reason,
                    "detail": reason["message"]}

    # 3. Re-read, then 4. write only what still needs it.
    succeeded, failed = [], []
    already_in_meta = False
    if action and action.get("targets") and atype in (ACTION_PAUSE_ADS, ACTION_ENABLE_ADS):
        pending = await _targets_needing_write(mongo_client, user_id, action)
        if not pending:
            already_in_meta = True
        else:
            res = await execute_action(mongo_client, user_id, {**action, "targets": pending})
            succeeded, failed = res["succeeded"], res["failed"]
            if succeeded:
                await _record_meta_state(db, user_id, doc, pending, succeeded,
                                         "PAUSED" if atype == ACTION_PAUSE_ADS else "ACTIVE",
                                         agent=agent)
            if not succeeded:
                await store.release_claim(db, user_id, suggestion_id, token)
                return {"ok": False, "outcome": "failed", "doc": doc,
                        "detail": failed[0]["error"] if failed else "Action failed",
                        "succeeded": succeeded, "failed": failed}
    elif action:
        res = await execute_action(mongo_client, user_id, action)
        succeeded, failed = res["succeeded"], res["failed"]
        if action.get("targets") and not succeeded:
            await store.release_claim(db, user_id, suggestion_id, token)
            return {"ok": False, "outcome": "failed", "doc": doc,
                    "detail": failed[0]["error"] if failed else "Action failed",
                    "succeeded": succeeded, "failed": failed}

    # Record exactly which objects changed so this can be undone later.
    targets_by_id = {str(t.get("object_id")): t for t in ((action or {}).get("targets") or [])}
    applied_targets = [
        {
            "object_id": oid,
            "object_type": (targets_by_id.get(str(oid)) or {}).get("object_type", "ad"),
            "name": (targets_by_id.get(str(oid)) or {}).get("name"),
        }
        for oid in succeeded
    ]
    decided_by = {"actor": actor, "source": source, "by": by, "at": datetime.utcnow()}

    # 5. Atomic open → applied.
    marked = await store.mark_applied(
        db, user_id, suggestion_id,
        applied_targets=applied_targets,
        applied_action_type=atype,
        decided_by=decided_by,
        already_in_meta=already_in_meta,
    )
    if marked is None:
        # Moved out of "open" while we held the claim (e.g. dismissed or
        # auto-resolved mid-apply, or our claim went stale and was taken over).
        if succeeded:
            logger.warning("apply: %s wrote %s to Meta but the suggestion was no longer open",
                           suggestion_id, succeeded)
        await store.release_claim(db, user_id, suggestion_id, token)
        return await _already(db, user_id, suggestion_id, doc)

    label = "Approved by you" if actor == store.ACTOR_USER else "Auto-run by Birdy"
    await store.log_activity(
        db, user_id,
        actor=actor,
        kind=store.KIND_ACTION_APPLIED,
        title=doc.get("title") or "Applied suggestion",
        client_name=doc.get("client_name"),
        client_group_id=doc.get("client_group_id"),
        ref_id=suggestion_id,
        window=doc.get("window"),
        label=label,
        source=source,
        agent_id=agent_id,
        rule_id=doc.get("rule_id"),
        # Only a real write counts towards the daily pause cap.
        applied_action_type=atype if succeeded else None,
        note="Already done in Meta — nothing changed" if already_in_meta else None,
    )
    if actor == store.ACTOR_USER:
        # Reached once per item: only the request that moved it open -> applied.
        await _learn(db, doc, "approved")
    await _sync_slack(db, user_id, suggestion_id, by=by)
    outcome = "already_in_meta" if already_in_meta else ("partial" if failed else "applied")
    return {"ok": True, "outcome": outcome, "doc": marked,
            "succeeded": succeeded, "failed": failed}


async def dismiss_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                             actor: str = store.ACTOR_USER, source: str | None = None,
                             by: str | None = None, reason_code: str | None = None,
                             note: str | None = None) -> dict:
    """Decline a suggestion (respects the store's dismiss cooldown)."""
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        return {"ok": False, "outcome": "not_found", "doc": None}

    changed = await store.mark_dismissed(db, user_id, suggestion_id)
    if not changed:
        fresh = await store.get_suggestion(db, user_id, suggestion_id)
        if store.has_live_claim(fresh):
            # An apply is mid-flight; dismissing now would orphan its Meta write.
            return {"ok": False, "outcome": "busy", "doc": fresh,
                    "detail": "Applying right now — try again in a moment"}
    await store.log_activity(
        db, user_id,
        actor=actor,
        kind=store.KIND_SUGGESTION_DISMISSED,
        title=f"Dismissed: {doc.get('title') or 'suggestion'}",
        client_name=doc.get("client_name"),
        client_group_id=doc.get("client_group_id"),
        ref_id=suggestion_id,
        window=doc.get("window"),
        label="Declined by you",
        source=source,
        agent_id=doc.get("agent_id"),
        rule_id=doc.get("rule_id"),
        reason_code=reason_code,
        note=note,
    )
    memory = None
    # Only the request that actually dismissed it counts, and only items with an
    # action: acknowledging a flag-only item is not a denial.
    if changed and doc.get("action"):
        learned = await _learn(db, doc, "denied", reason_code=reason_code, note=note)
        memory = (learned or {}).get("memory")
    await _sync_slack(db, user_id, suggestion_id, by=by)
    return {"ok": True, "outcome": "dismissed", "doc": doc, "memory": memory}


async def undo_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                          actor: str = store.ACTOR_USER, source: str | None = None,
                          by: str | None = None) -> dict:
    """
    Reverse a previously-applied suggestion: re-enable the exact ads that were
    paused (from applied_targets) and put the suggestion back to open so it can
    be acted on again.
    """
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        return {"ok": False, "outcome": "not_found", "doc": None}
    if doc.get("status") != store.STATUS_APPLIED:
        # Idempotent: nothing is currently applied to reverse. Either it was
        # already undone, or this is a historical / seeded activity row whose
        # suggestion is no longer in an applied state. Treat undo as a benign
        # no-op instead of a 409 so the UI settles quietly (no "couldn't undo"
        # error toast) — undoing an un-applied thing is a success by definition.
        return {"ok": True, "outcome": "noop", "doc": doc, "succeeded": []}

    targets = doc.get("applied_targets") or []
    applied_type = doc.get("applied_action_type") or (doc.get("action") or {}).get("type")
    inverse = ACTION_ENABLE_ADS if applied_type == ACTION_PAUSE_ADS else ACTION_PAUSE_ADS

    succeeded, failed = [], []
    if targets:
        res = await execute_action(mongo_client, user_id, {"type": inverse, "targets": targets})
        succeeded, failed = res["succeeded"], res["failed"]
        if succeeded:
            await _record_meta_state(db, user_id, doc, targets, succeeded,
                                     "PAUSED" if inverse == ACTION_PAUSE_ADS else "ACTIVE")
        if not succeeded:
            return {"ok": False, "outcome": "failed", "doc": doc,
                    "detail": failed[0]["error"] if failed else "Undo failed",
                    "succeeded": succeeded, "failed": failed}

    undone = await store.mark_undone(db, user_id, suggestion_id,
                                     undone_by={"actor": actor, "source": source, "by": by})
    await store.log_activity(
        db, user_id,
        actor=actor,
        kind=store.KIND_ACTION_UNDONE,
        title=f"Undid: {doc.get('title') or 'suggestion'}",
        client_name=doc.get("client_name"),
        client_group_id=doc.get("client_group_id"),
        ref_id=suggestion_id,
        window=doc.get("window"),
        label="Undone by you",
        source=source,
        agent_id=doc.get("agent_id"),
        rule_id=doc.get("rule_id"),
    )
    if undone:
        # Who applied it decides what the undo teaches: undoing Birdy's own
        # auto-run counts towards the brake. (mark_undone clears decided_by, so
        # it is read from the doc as it was before the undo.)
        applied_by = (doc.get("decided_by") or {}).get("actor")
        await _learn(db, doc, "undone_auto" if applied_by == store.ACTOR_BIRDY else "undone_user")
    await _sync_slack(db, user_id, suggestion_id, by=by)
    return {"ok": True, "outcome": "undone", "doc": doc,
            "succeeded": succeeded, "failed": failed}
