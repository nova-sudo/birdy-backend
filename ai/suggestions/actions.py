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
                            |"blocked"|"dismissed"|"not_found"|"failed"|"noop",
     "doc": <suggestion doc or None>, "succeeded": [...], "failed": [...],
     "detail": <str, on failure>,
     "decided_by": <dict, on "already">, "blocked_reason": <{code, message}, on "blocked">}

  already          someone else already decided it (or is applying it right now)
  already_in_meta  every target was already in the desired state (or deleted) in
                   Meta, so nothing was written; the suggestion is marked applied
  blocked          an agent guardrail stopped it; it stays open with blocked_reason

Apply order (see apply_suggestion): claim → guardrails → re-read → write →
mark_applied. The claim comes first so concurrent applies make one Meta write.
"""

import logging
from datetime import datetime

from ai.agents.guardrails import check_guardrails
from ai.agents.store import get_agent
from ai.suggestions import store
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from services.meta_status_service import MetaStatusError, get_object_status, set_object_status

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


def _is_desired(atype: str, status: str | None) -> bool:
    """
    Is the object already where this action would put it? None = deleted in Meta,
    which counts as done. Pause: anything not ACTIVE is already not delivering.
    Enable: only ACTIVE counts — CAMPAIGN_PAUSED / ADSET_PAUSED etc. are not.
    """
    if status is None:
        return True
    if atype == ACTION_PAUSE_ADS:
        return status != "ACTIVE"
    return status == "ACTIVE"


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
            status = await get_object_status(user_id, t.get("object_id"),
                                             t.get("object_type", "ad"), mongo_client)
        except Exception as e:
            logger.warning("apply: re-read of %s failed, writing anyway: %s", t.get("object_id"), e)
            pending.append(t)
            continue
        if not _is_desired(atype, status):
            pending.append(t)
    return pending


async def _already(db, user_id: str, suggestion_id: str, fallback: dict) -> dict:
    cur = await store.get_suggestion(db, user_id, suggestion_id) or fallback
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
    await _sync_slack(db, user_id, suggestion_id, by=by)
    outcome = "already_in_meta" if already_in_meta else ("partial" if failed else "applied")
    return {"ok": True, "outcome": outcome, "doc": marked,
            "succeeded": succeeded, "failed": failed}


async def dismiss_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                             actor: str = store.ACTOR_USER, source: str | None = None,
                             by: str | None = None) -> dict:
    """Decline a suggestion (respects the store's dismiss cooldown)."""
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        return {"ok": False, "outcome": "not_found", "doc": None}

    await store.mark_dismissed(db, user_id, suggestion_id)
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
    )
    await _sync_slack(db, user_id, suggestion_id, by=by)
    return {"ok": True, "outcome": "dismissed", "doc": doc}


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
        if not succeeded:
            return {"ok": False, "outcome": "failed", "doc": doc,
                    "detail": failed[0]["error"] if failed else "Undo failed",
                    "succeeded": succeeded, "failed": failed}

    await store.mark_undone(db, user_id, suggestion_id)
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
    )
    await _sync_slack(db, user_id, suggestion_id, by=by)
    return {"ok": True, "outcome": "undone", "doc": doc,
            "succeeded": succeeded, "failed": failed}
