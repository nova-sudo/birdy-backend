"""
ai/suggestions/store.py
-----------------------
Mongo-backed persistence for the suggestion engine. Two collections:

  ai_suggestions — one doc per live/decided suggestion. `_id` is the finding's
                   dedup_key so a re-run refreshes the same doc in place rather
                   than duplicating. A dismissed suggestion is not recreated for
                   DISMISS_COOLDOWN_DAYS (we respect the user's decline); an
                   applied one is never recreated.

  ai_activity   — the activity feed. Append-only, TTL-expired after
                  ACTIVITY_TTL_DAYS so it can't grow unbounded. Records analysis
                  passes, suggestions created, and actions applied / declined.

Follows the same per-request Motor pattern as the rest of the backend (raw dicts,
no ODM). Index creation mirrors ai/session_store.py and is wired into main.py's
lifespan.
"""

import logging
import uuid
from datetime import datetime, timedelta

from core.database import DB_NAME
from ai.suggestions.contracts import Finding

logger = logging.getLogger(__name__)

SUGGESTIONS = "ai_suggestions"
ACTIVITY = "ai_activity"

# How long a dismissed suggestion stays suppressed before the same finding may
# resurface. Long enough that a user isn't nagged, short enough that a genuinely
# persistent problem eventually comes back.
DISMISS_COOLDOWN_DAYS = 14

# Activity feed retention.
ACTIVITY_TTL_DAYS = 60

STATUS_OPEN = "open"
STATUS_APPLIED = "applied"
STATUS_DISMISSED = "dismissed"
# Auto-closed by a later pass because the finding no longer reproduces (e.g. the
# ad improved or was already paused). Distinct from a user "dismissed".
STATUS_RESOLVED = "resolved"

# Activity kinds → (actor, human label). actor keeps the frontend's existing
# birdy/user split working; label is the richer, kind-specific caption.
ACTOR_BIRDY = "birdy"
ACTOR_USER = "user"

KIND_ANALYSIS_PASS = "analysis_pass"
KIND_SUGGESTION_CREATED = "suggestion_created"
KIND_ACTION_APPLIED = "action_applied"
KIND_SUGGESTION_DISMISSED = "suggestion_dismissed"
KIND_ACTION_UNDONE = "action_undone"


def _now() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def build_suggestion_doc(user_id: str, finding: Finding, *, composer: str = "template") -> dict:
    """Turn a Finding (+ its already-composed copy) into a persistable document."""
    key = finding.compute_dedup_key()
    now = _now()
    return {
        "_id": key,
        "id": key,
        "user_id": user_id,
        "client_group_id": finding.client_group_id,
        "client_name": finding.client_name,
        "agent": finding.agent,
        "window": finding.evidence.window,
        "origin_window": finding.evidence.window,  # immutable; `window` may change on refresh
        "severity": finding.severity,
        "platform": finding.platform,
        "icon": finding.icon,
        "title": finding.title,
        "description": finding.description,
        "stats": finding.evidence.stats_as_dicts(),
        "action": finding.action.to_dict() if finding.action else None,
        "evidence": finding.evidence.raw,
        "confidence": finding.confidence,
        "composer": composer,
        "status": STATUS_OPEN,
        "created_at": now,
        "updated_at": now,
    }


async def upsert_finding(db, user_id: str, finding: Finding, *, composer: str = "template") -> tuple[dict | None, bool, bool]:
    """
    Persist a finding, honouring dedup + decline history.

    Returns (doc, created, content_changed):
      * created         — a brand-new suggestion was inserted (or a cooled-down
                          dismissed one was reopened) → log + post to Slack.
      * content_changed — the visible copy/stats were (re)written → keep Slack in
                          sync via chat.update.
    Returns (None, False, False) when suppressed (already applied, or dismissed
    within the cooldown).

    Content ownership: a suggestion's copy/stats belong to the window that CREATED
    it (origin_window). Another window (e.g. the monthly pass touching an ad the
    weekly pass already flagged) only keeps it alive — it does NOT overwrite the
    content. This stops the same suggestion flipping between the weekly and
    monthly view (and diverging from what was posted to Slack).
    """
    key = finding.compute_dedup_key()
    now = _now()
    existing = await db[SUGGESTIONS].find_one({"_id": key})

    if existing:
        status = existing.get("status")
        if status == STATUS_APPLIED:
            return None, False, False  # already acted on — never resurface
        reopening = False
        if status == STATUS_DISMISSED:
            dismissed_at = existing.get("dismissed_at")
            if dismissed_at and dismissed_at > now - timedelta(days=DISMISS_COOLDOWN_DAYS):
                return None, False, False  # respect the recent decline
            reopening = True  # cooldown elapsed → reopen with fresh content

        finding_window = finding.evidence.window
        owns_content = reopening or finding_window == existing.get("origin_window")

        if not owns_content:
            # A non-origin window → keep it alive but leave the content untouched.
            await db[SUGGESTIONS].update_one(
                {"_id": key}, {"$set": {"status": STATUS_OPEN, "updated_at": now}}
            )
            return {**existing, "status": STATUS_OPEN}, False, False

        update = {
            "severity": finding.severity,
            "title": finding.title,
            "description": finding.description,
            "stats": finding.evidence.stats_as_dicts(),
            "action": finding.action.to_dict() if finding.action else None,
            "evidence": finding.evidence.raw,
            "confidence": finding.confidence,
            "icon": finding.icon,
            "composer": composer,
            "status": STATUS_OPEN,
            "updated_at": now,
        }
        if reopening:
            update["origin_window"] = finding_window  # a reopen is a fresh start
        await db[SUGGESTIONS].update_one({"_id": key}, {"$set": update, "$unset": {"dismissed_at": "", "applied_at": ""}})
        refreshed = {**existing, **update}
        refreshed.pop("dismissed_at", None)
        refreshed.pop("applied_at", None)
        return refreshed, reopening, True

    doc = build_suggestion_doc(user_id, finding, composer=composer)
    await db[SUGGESTIONS].insert_one(doc)
    return doc, True, True


async def list_open_suggestions(db, user_id: str, limit: int = 50) -> list[dict]:
    cursor = db[SUGGESTIONS].find(
        {"user_id": user_id, "status": STATUS_OPEN}
    ).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def list_recent_applied(db, user_id: str, *, within_hours: int = 24,
                              limit: int = 20) -> list[dict]:
    """
    Recently-applied suggestions, so the dashboard can keep showing them with a
    lasting Undo (mirroring the persistent Undo button on the Slack card).
    """
    since = _now() - timedelta(hours=within_hours)
    cursor = db[SUGGESTIONS].find(
        {"user_id": user_id, "status": STATUS_APPLIED, "applied_at": {"$gte": since}}
    ).sort("applied_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_suggestion(db, user_id: str, suggestion_id: str) -> dict | None:
    return await db[SUGGESTIONS].find_one({"_id": suggestion_id, "user_id": user_id})


async def mark_applied(db, user_id: str, suggestion_id: str, *,
                       applied_targets: list | None = None,
                       applied_action_type: str | None = None) -> bool:
    """
    Mark applied, recording exactly which objects were changed so the action can
    be UNDONE later (undo re-applies the inverse to these targets only).
    """
    now = _now()
    update = {"status": STATUS_APPLIED, "applied_at": now, "updated_at": now}
    if applied_targets is not None:
        update["applied_targets"] = applied_targets
    if applied_action_type:
        update["applied_action_type"] = applied_action_type
    res = await db[SUGGESTIONS].update_one(
        {"_id": suggestion_id, "user_id": user_id}, {"$set": update}
    )
    return res.modified_count > 0


async def mark_undone(db, user_id: str, suggestion_id: str) -> bool:
    """Revert an applied suggestion back to open once its action was undone."""
    now = _now()
    res = await db[SUGGESTIONS].update_one(
        {"_id": suggestion_id, "user_id": user_id},
        {
            "$set": {"status": STATUS_OPEN, "updated_at": now},
            "$unset": {"applied_at": "", "applied_targets": "", "applied_action_type": ""},
        },
    )
    return res.modified_count > 0


async def mark_dismissed(db, user_id: str, suggestion_id: str) -> bool:
    res = await db[SUGGESTIONS].update_one(
        {"_id": suggestion_id, "user_id": user_id},
        {"$set": {"status": STATUS_DISMISSED, "dismissed_at": _now(), "updated_at": _now()}},
    )
    return res.modified_count > 0


async def set_slack_message(db, user_id: str, suggestion_id: str, channel: str, ts: str) -> None:
    """Remember where a suggestion was posted in Slack (for later cross-surface sync)."""
    await db[SUGGESTIONS].update_one(
        {"_id": suggestion_id, "user_id": user_id},
        {"$set": {"slack_channel": channel, "slack_ts": ts, "updated_at": _now()}},
    )


async def close_stale_open(db, user_id: str, agent: str, origin_window: str,
                           keep_ids: set) -> int:
    """
    Auto-close open suggestions this pass no longer produced (the ad improved, or
    was paused). Scoped per (user, agent, origin_window) across ALL of the user's
    clients — NOT per client-group — because a suggestion dedupes by ad and a
    shared ad may be evaluated under several client-groups in one pass. `keep_ids`
    must therefore be the UNION of everything kept across every client this pass.
    origin_window (the window that CREATED the suggestion) keeps weekly and
    monthly from closing each other's findings.
    """
    res = await db[SUGGESTIONS].update_many(
        {
            "user_id": user_id,
            "agent": agent,
            "origin_window": origin_window,
            "status": STATUS_OPEN,
            "_id": {"$nin": list(keep_ids)},
        },
        {"$set": {"status": STATUS_RESOLVED, "resolved_at": _now(), "updated_at": _now()}},
    )
    return res.modified_count


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------

async def log_activity(
    db,
    user_id: str,
    *,
    actor: str,
    kind: str,
    title: str,
    client_name: str | None = None,
    client_group_id: str | None = None,
    ref_id: str | None = None,
    window: str | None = None,
    label: str | None = None,
    source: str | None = None,
) -> dict:
    """Append one entry to the activity feed (TTL-expired after ACTIVITY_TTL_DAYS)."""
    now = _now()
    doc = {
        "_id": "act_" + uuid.uuid4().hex[:16],
        "id": None,  # set below to match _id
        "user_id": user_id,
        "actor": actor,
        "kind": kind,
        "title": title,
        "client": client_name,
        "client_group_id": client_group_id,
        "ref_id": ref_id,
        "window": window,
        "label": label,
        "source": source,  # "dashboard" | "slack" | None
        "created_at": now,
        "expires_at": now + timedelta(days=ACTIVITY_TTL_DAYS),
    }
    doc["id"] = doc["_id"]
    await db[ACTIVITY].insert_one(doc)
    return doc


async def list_recent_activity(db, user_id: str, limit: int = 30) -> list[dict]:
    cursor = db[ACTIVITY].find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


# ---------------------------------------------------------------------------
# Indexes (wired into main.py lifespan)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Per-user suggestion settings (strictness)
# ---------------------------------------------------------------------------

async def get_user_strictness(db, user_id: str) -> str:
    """The account's chosen strictness level, defaulting to 'balanced'."""
    doc = await db["users"].find_one({"user_id": user_id}, projection={"suggestion_strictness": 1})
    return (doc or {}).get("suggestion_strictness") or "balanced"


async def set_user_strictness(db, user_id: str, level: str) -> None:
    await db["users"].update_one(
        {"user_id": user_id}, {"$set": {"suggestion_strictness": level}}
    )


async def create_suggestion_indexes(mongo_client):
    db = mongo_client[DB_NAME]
    await db[SUGGESTIONS].create_index([("user_id", 1), ("status", 1), ("created_at", -1)])
    await db[SUGGESTIONS].create_index("client_group_id")
    await db[ACTIVITY].create_index([("user_id", 1), ("created_at", -1)])
    # TTL index — Mongo drops activity docs once expires_at passes.
    await db[ACTIVITY].create_index("expires_at", expireAfterSeconds=0)
