"""
ai/suggestions/actions.py
-------------------------
Apply / dismiss a suggestion — the shared core used by BOTH the dashboard HTTP
endpoints (routers/dashboard.py) and the Slack button handler
(routers/slack_interactions.py). Extracted so "Do it for me" behaves identically
whether it's clicked in the app or in Slack.

These functions never raise for expected cases; they return an outcome dict so
each caller can map it to its own surface (HTTP status vs. a Slack message):

    {"ok": bool, "outcome": "applied"|"partial"|"already"|"dismissed"
                            |"not_found"|"failed"|"noop",
     "doc": <suggestion doc or None>, "succeeded": [...], "failed": [...],
     "detail": <str, on failure>}
"""

import logging

from ai.suggestions import store
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from services.meta_status_service import MetaStatusError, set_object_status

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


async def apply_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                           actor: str = store.ACTOR_USER, source: str | None = None) -> dict:
    """
    Approve + execute a suggestion. actor=ACTOR_USER means a human approved it
    ("Approved by you"); actor=ACTOR_BIRDY would be an autonomous run.
    """
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        return {"ok": False, "outcome": "not_found", "doc": None}
    if doc.get("status") == store.STATUS_APPLIED:
        return {"ok": True, "outcome": "already", "doc": doc}

    action = doc.get("action")
    succeeded, failed = [], []
    if action:
        res = await execute_action(mongo_client, user_id, action)
        succeeded, failed = res["succeeded"], res["failed"]
        # Every target failed → don't mark applied; report the failure.
        if action.get("targets") and not succeeded:
            return {"ok": False, "outcome": "failed", "doc": doc,
                    "detail": failed[0]["error"] if failed else "Action failed",
                    "succeeded": succeeded, "failed": failed}

    await store.mark_applied(db, user_id, suggestion_id)
    await store.log_activity(
        db, user_id,
        actor=actor,
        kind=store.KIND_ACTION_APPLIED,
        title=doc.get("title") or "Applied suggestion",
        client_name=doc.get("client_name"),
        client_group_id=doc.get("client_group_id"),
        ref_id=suggestion_id,
        window=doc.get("window"),
        label="Approved by you" if actor == store.ACTOR_USER else "Auto-run by Birdy",
        source=source,
    )
    return {"ok": True, "outcome": "partial" if failed else "applied", "doc": doc,
            "succeeded": succeeded, "failed": failed}


async def dismiss_suggestion(db, mongo_client, user_id: str, suggestion_id: str, *,
                             actor: str = store.ACTOR_USER, source: str | None = None) -> dict:
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
    return {"ok": True, "outcome": "dismissed", "doc": doc}
