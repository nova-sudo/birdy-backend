"""
services/dashboard_service.py
-----------------------------
Backs the homepage "Do it for me" suggestion flow that the frontend
(src/app/dashboard/useDashboardData.js) is already wired to call:

    GET    /api/dashboard/summary
    POST   /api/dashboard/suggestions/{id}/apply
    POST   /api/dashboard/suggestions/{id}/undo
    DELETE /api/dashboard/suggestions/{id}

Design notes
============
* **Reversal-record-first.** apply() snapshots each target ad's current status
  and writes the `suggestion_actions` reversal record BEFORE it pauses anything.
  If the process dies mid-apply, undo can still restore every ad, because the
  prior statuses are already persisted.
* **Idempotent.** Re-applying an already-applied suggestion returns the existing
  result; undoing an already-undone (or never-applied) suggestion is a success
  no-op. Safe against double-clicks.
* **Partial failure.** Each ad's pause/restore is recorded individually, so one
  ad failing at Meta never blocks the others and undo still restores whatever was
  actually changed.

Integration point
=================
Suggestions themselves are produced by the AI suggestion pipeline and must be
persisted to the `dashboard_suggestions` collection with a stable `suggestion_id`
and the `target_ad_ids` the action operates on. apply()/undo() act on those ads.
Until the pipeline populates that collection, summary() returns no suggestions
and the frontend keeps showing its bundled placeholder data.

Collections
===========
dashboard_suggestions: { suggestion_id, user_id, status, target_ad_ids: [str],
                         severity, icon, client, platform, title, description,
                         stats, created_at }
suggestion_actions:    { suggestion_id, user_id, state, targets: [{ad_id,
                         prior_status, result}], suggestion_snapshot, applied_at,
                         undone_at }
"""

import logging
from datetime import datetime

from integrations.facebook_utils.facebook import get_facebook_token
# Imported at module level so tests can monkeypatch them with fakes.
from integrations.facebook_utils.facebook_ads_write import get_ad_status, set_ad_status

logger = logging.getLogger(__name__)

SUGGESTIONS = "dashboard_suggestions"
ACTIONS = "suggestion_actions"

# States a reversal record can be in while it is still reversible.
_ACTIVE_STATES = ["applying", "applied"]


class SuggestionNotFound(Exception):
    """No open suggestion with that id for this user."""


class MetaNotConnected(Exception):
    """The user has no usable Meta access token."""


# ── Index setup (call from the app lifespan) ─────────────────────────────────

async def create_dashboard_indexes(mongo_client):
    from core.database import DB_NAME
    db = mongo_client[DB_NAME]
    await db[SUGGESTIONS].create_index([("user_id", 1), ("status", 1)])
    await db[SUGGESTIONS].create_index([("user_id", 1), ("suggestion_id", 1)], unique=True)
    await db[ACTIONS].create_index([("user_id", 1), ("suggestion_id", 1), ("state", 1)])
    await db[ACTIONS].create_index([("user_id", 1), ("applied_at", -1)])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fe_suggestion(doc: dict) -> dict:
    """Map a stored suggestion doc to the shape the dashboard card expects."""
    return {
        "id": doc["suggestion_id"],
        "severity": doc.get("severity", "MEDIUM"),
        "icon": doc.get("icon", "sparkles"),
        "client": doc.get("client", ""),
        "platform": doc.get("platform", "Meta Ads"),
        "title": doc.get("title", ""),
        "description": doc.get("description", ""),
        "stats": doc.get("stats", []),
    }


def _relative_time(dt: datetime | None, now: datetime | None = None) -> str:
    if not dt:
        return "just now"
    now = now or datetime.utcnow()
    secs = max(0, int((now - dt).total_seconds()))
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _activity_from_action(action: dict, now: datetime | None = None) -> dict:
    """Build a "Completed today" activity row from a reversal record."""
    snap = action.get("suggestion_snapshot", {})
    undone = action.get("state") == "undone"
    return {
        "id": f"applied-{action['suggestion_id']}",
        "kind": "action_applied",
        "actor": "birdy",
        "title": snap.get("title", ""),
        "client": snap.get("client", ""),
        "time": _relative_time(action.get("undone_at") if undone else action.get("applied_at"), now),
        "reversible": True,
        "undone": undone,
        "suggestion": snap,  # full snapshot so the client can undo and restore the card
    }


# ── Summary ──────────────────────────────────────────────────────────────────

async def get_summary(db, user_id: str) -> dict:
    """Open suggestions + a completed-actions activity feed for the user."""
    open_docs = await db[SUGGESTIONS].find(
        {"user_id": user_id, "status": "open"}
    ).to_list(None)
    suggestions = [_fe_suggestion(d) for d in open_docs]

    action_docs = (
        await db[ACTIONS].find({"user_id": user_id}).sort("applied_at", -1).limit(20).to_list(None)
    )
    activity = [_activity_from_action(a) for a in action_docs]

    # Alerts and wins have no store yet — return empty so counts are honest.
    alerts, wins = [], []
    return {
        "suggestions": suggestions,
        "alerts": alerts,
        "wins": wins,
        "activity": activity,
        "counts": {"suggestions": len(suggestions), "alerts": len(alerts), "wins": len(wins)},
    }


# ── Apply ────────────────────────────────────────────────────────────────────

async def apply_suggestion(db, user_id: str, suggestion_id: str) -> dict:
    """Pause the suggestion's target ads, recording a reversal record first."""
    suggestion = await db[SUGGESTIONS].find_one(
        {"user_id": user_id, "suggestion_id": suggestion_id}
    )
    if not suggestion:
        raise SuggestionNotFound(suggestion_id)

    # Idempotent: an active (non-undone) record already exists → return it.
    existing = await db[ACTIONS].find_one(
        {"user_id": user_id, "suggestion_id": suggestion_id, "state": {"$in": _ACTIVE_STATES}}
    )
    if existing:
        succeeded = [t["ad_id"] for t in existing.get("targets", []) if t.get("result") == "paused"]
        return {"ok": True, "succeeded": succeeded, "failed": [], "idempotent": True}

    token_doc = await get_facebook_token(user_id, db.client)
    access_token = (token_doc or {}).get("access_token")
    if not access_token:
        raise MetaNotConnected()

    ad_ids = suggestion.get("target_ad_ids", []) or []

    # 1) Snapshot current status of every target ad (so undo restores exactly).
    targets = []
    for ad_id in ad_ids:
        prior = await get_ad_status(ad_id, access_token)
        targets.append({"ad_id": ad_id, "prior_status": prior or "ACTIVE", "result": "pending"})

    # 2) Write the reversal record BEFORE mutating anything at Meta.
    now = datetime.utcnow()
    record = {
        "suggestion_id": suggestion_id,
        "user_id": user_id,
        "state": "applying",
        "targets": targets,
        "suggestion_snapshot": _fe_suggestion(suggestion),
        "applied_at": now,
        "undone_at": None,
    }
    insert = await db[ACTIONS].insert_one(record)

    # 3) Pause each ad, recording per-ad outcome.
    succeeded, failed = [], []
    for t in targets:
        ok, err = await set_ad_status(t["ad_id"], "PAUSED", access_token)
        t["result"] = "paused" if ok else "failed"
        if ok:
            succeeded.append(t["ad_id"])
        else:
            t["error"] = err
            failed.append({"ad_id": t["ad_id"], "error": err})

    # 4) Finalise the record + flip the suggestion to applied.
    await db[ACTIONS].update_one(
        {"_id": insert.inserted_id},
        {"$set": {"state": "applied", "targets": targets}},
    )
    await db[SUGGESTIONS].update_one(
        {"user_id": user_id, "suggestion_id": suggestion_id},
        {"$set": {"status": "applied"}},
    )

    logger.info(
        "apply_suggestion %s for %s: %d paused, %d failed",
        suggestion_id, user_id, len(succeeded), len(failed),
    )
    return {"ok": True, "succeeded": succeeded, "failed": failed}


# ── Undo ─────────────────────────────────────────────────────────────────────

async def undo_suggestion(db, user_id: str, suggestion_id: str) -> dict:
    """Restore each target ad to the status captured at apply time."""
    record = await db[ACTIONS].find_one(
        {"user_id": user_id, "suggestion_id": suggestion_id, "state": {"$in": _ACTIVE_STATES}}
    )
    # Never applied, or already undone → idempotent success no-op.
    if not record:
        return {"ok": True, "restored": [], "idempotent": True}

    token_doc = await get_facebook_token(user_id, db.client)
    access_token = (token_doc or {}).get("access_token")
    if not access_token:
        raise MetaNotConnected()

    restored, failed = [], []
    targets = record.get("targets", [])
    for t in targets:
        # Only restore ads we actually paused; leave failed-to-pause ads alone.
        if t.get("result") not in ("paused", "pending"):
            continue
        prior = t.get("prior_status") or "ACTIVE"
        ok, err = await set_ad_status(t["ad_id"], prior, access_token)
        if ok:
            t["result"] = "restored"
            restored.append(t["ad_id"])
        else:
            failed.append({"ad_id": t["ad_id"], "error": err})

    await db[ACTIONS].update_one(
        {"_id": record["_id"]},
        {"$set": {"state": "undone", "undone_at": datetime.utcnow(), "targets": targets}},
    )
    await db[SUGGESTIONS].update_one(
        {"user_id": user_id, "suggestion_id": suggestion_id},
        {"$set": {"status": "open"}},
    )

    logger.info(
        "undo_suggestion %s for %s: %d restored, %d failed",
        suggestion_id, user_id, len(restored), len(failed),
    )
    return {"ok": True, "restored": restored, "failed": failed}


# ── Dismiss / alerts / wins ──────────────────────────────────────────────────

async def dismiss_suggestion(db, user_id: str, suggestion_id: str) -> dict:
    await db[SUGGESTIONS].update_one(
        {"user_id": user_id, "suggestion_id": suggestion_id},
        {"$set": {"status": "dismissed"}},
    )
    return {"ok": True}


async def run_alert_action(db, user_id: str, alert_id: str, action: str) -> dict:
    # Alerts are display-only for now; this records intent without side effects.
    logger.info("alert action %s=%s for %s", alert_id, action, user_id)
    return {"ok": True}


async def complete_win(db, user_id: str, win_id: str) -> dict:
    logger.info("win completed %s for %s", win_id, user_id)
    return {"ok": True}
