"""
routers/dashboard.py
--------------------
Backend for the redesigned homepage (src/app/dashboard on the frontend). Serves
the single summary the page needs and the action endpoints behind each card.

Contract (the frontend calls these directly via apiRequest — there is no Next
proxy layer):

  GET    /api/dashboard/summary              -> {suggestions, alerts, wins, activity, counts}
  POST   /api/dashboard/suggestions/{id}/apply
  DELETE /api/dashboard/suggestions/{id}
  POST   /api/dashboard/alerts/{id}/action   body: {action}
  POST   /api/dashboard/wins/{id}/complete
  POST   /api/dashboard/refresh              body: {window?}  (on-demand pass)

Suggestions + the activity feed come from the ai/suggestions engine. Alerts and
wins are mapped from the existing alerts system (triggered warnings vs. triggered
"win" alerts) so the other two tabs show real data too.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from ai.suggestions import actions, store
from ai.suggestions.contracts import WINDOWS
from ai.suggestions.orchestrator import run_pass_for_user
from ai.suggestions.agents.useless_ad_purger import STRICTNESS_LEVELS, DEFAULT_STRICTNESS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _relative_time(dt: datetime | None) -> str:
    if not dt:
        return ""
    secs = (datetime.utcnow() - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _suggestion_to_api(doc: dict) -> dict:
    return {
        "id": doc.get("id"),
        "severity": doc.get("severity"),
        "icon": doc.get("icon", "sparkles"),
        "client": doc.get("client_name"),
        "platform": doc.get("platform", "Meta Ads"),
        "title": doc.get("title"),
        "description": doc.get("description"),
        "stats": doc.get("stats") or [],
        "window": doc.get("window"),
    }


def _activity_to_api(doc: dict) -> dict:
    return {
        "id": doc.get("id"),
        "actor": doc.get("actor", "birdy"),
        "kind": doc.get("kind"),
        "title": doc.get("title"),
        "client": doc.get("client"),
        "label": doc.get("label"),
        "source": doc.get("source"),  # "dashboard" | "slack" | None
        "time": _relative_time(doc.get("created_at")),
    }


def _alert_client_label(names: list) -> str:
    if not names:
        return "All clients"
    if len(names) == 1:
        return names[0]
    return f"{len(names)} clients"


def _alert_to_api(doc: dict) -> dict:
    msg = (doc.get("last_eval_result") or {}).get("message")
    return {
        "id": doc.get("id"),
        "color": "red",
        "client": _alert_client_label(doc.get("target_group_names") or []),
        "title": doc.get("name") or msg or "Alert triggered",
        "badge": "Needs approval",
        "badgeTone": "gray",
        "cta": "View all",
        "ctaVariant": "outline",
        "actionKey": "view_all",
    }


def _win_to_api(doc: dict) -> dict:
    names = doc.get("target_group_names") or []
    return {
        "id": doc.get("id"),
        "client": names[0] if names else "Client",
        "title": doc.get("name") or "Client win",
        "description": (doc.get("last_eval_result") or {}).get("message") or "",
    }


# ---------------------------------------------------------------------------
# GET /summary
# ---------------------------------------------------------------------------

@router.get("/summary")
async def summary(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]

        suggestions = await store.list_open_suggestions(db, current_user, limit=50)
        activity = await store.list_recent_activity(db, current_user, limit=30)

        triggered = await db["alerts"].find(
            {"user_id": current_user, "status": "triggered"}
        ).sort("last_triggered_at", -1).to_list(length=50)
        alert_docs = [a for a in triggered if a.get("type") != "win"]
        win_docs = [a for a in triggered if a.get("type") == "win"]

        suggestions_api = [_suggestion_to_api(s) for s in suggestions]
        alerts_api = [_alert_to_api(a) for a in alert_docs]
        wins_api = [_win_to_api(w) for w in win_docs]

        return {
            "suggestions": suggestions_api,
            "alerts": alerts_api,
            "wins": wins_api,
            "activity": [_activity_to_api(a) for a in activity],
            "counts": {
                "suggestions": len(suggestions_api),
                "alerts": len(alerts_api),
                "wins": len(wins_api),
            },
        }


# ---------------------------------------------------------------------------
# Suggestion actions — shared with the Slack button handler via
# ai.suggestions.actions so "Do it for me" behaves identically in both places.
# ---------------------------------------------------------------------------

@router.post("/suggestions/{suggestion_id}/apply")
async def apply_suggestion(suggestion_id: str, current_user: str = Depends(get_current_user)):
    """Apply a suggestion's action (approval-only — the user is approving it)."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        res = await actions.apply_suggestion(
            db, mongo_client, current_user, suggestion_id,
            actor=store.ACTOR_USER, source="dashboard",
        )
        if res["outcome"] == "not_found":
            raise HTTPException(status_code=404, detail="Suggestion not found")
        if res["outcome"] == "failed":
            raise HTTPException(status_code=502, detail=res.get("detail", "Action failed"))
        return {"ok": True, "outcome": res["outcome"],
                "succeeded": res.get("succeeded", []), "failed": res.get("failed", [])}


@router.delete("/suggestions/{suggestion_id}")
async def dismiss_suggestion(suggestion_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        res = await actions.dismiss_suggestion(
            db, mongo_client, current_user, suggestion_id,
            actor=store.ACTOR_USER, source="dashboard",
        )
        if res["outcome"] == "not_found":
            raise HTTPException(status_code=404, detail="Suggestion not found")
        return {"ok": True}


@router.post("/suggestions/{suggestion_id}/undo")
async def undo_suggestion(suggestion_id: str, current_user: str = Depends(get_current_user)):
    """Reverse an applied suggestion — re-enables exactly the ads it paused."""
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        res = await actions.undo_suggestion(
            db, mongo_client, current_user, suggestion_id,
            actor=store.ACTOR_USER, source="dashboard",
        )
        if res["outcome"] == "not_found":
            raise HTTPException(status_code=404, detail="Suggestion not found")
        if res["outcome"] == "not_applied":
            raise HTTPException(status_code=409, detail="Suggestion is not in an applied state")
        if res["outcome"] == "failed":
            raise HTTPException(status_code=502, detail=res.get("detail", "Undo failed"))
        return {"ok": True, "outcome": res["outcome"], "succeeded": res.get("succeeded", [])}


# ---------------------------------------------------------------------------
# Alert + win actions (lightweight; the alerts system owns the heavy lifting)
# ---------------------------------------------------------------------------

class AlertActionRequest(BaseModel):
    action: str  # "open_client" | "view_all"


@router.post("/alerts/{alert_id}/action")
async def alert_action(alert_id: str, body: AlertActionRequest,
                       current_user: str = Depends(get_current_user)):
    # Navigational actions are handled client-side; this endpoint just
    # acknowledges (and is where analytics/logging can hook in later).
    return {"ok": True, "alert_id": alert_id, "action": body.action}


@router.post("/wins/{win_id}/complete")
async def complete_win(win_id: str, current_user: str = Depends(get_current_user)):
    return {"ok": True, "win_id": win_id}


# ---------------------------------------------------------------------------
# On-demand refresh (runs a pass for the current user right now)
# ---------------------------------------------------------------------------

class RefreshRequest(BaseModel):
    window: str | None = None  # "weekly" | "monthly"; None runs both


@router.post("/refresh")
async def refresh(body: RefreshRequest | None = None,
                  current_user: str = Depends(get_current_user)):
    windows = [body.window] if (body and body.window) else list(WINDOWS)
    for w in windows:
        if w not in WINDOWS:
            raise HTTPException(status_code=400, detail=f"Unknown window: {w}")

    results = {}
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        for w in windows:
            results[w] = await run_pass_for_user(db, current_user, w, mongo_client=mongo_client)
    return {"ok": True, "results": results}


# ---------------------------------------------------------------------------
# Suggestion settings (how strict the analyzer is)
# ---------------------------------------------------------------------------

class SettingsRequest(BaseModel):
    strictness: str  # "lenient" | "balanced" | "strict"


@router.get("/settings")
async def get_settings(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        strictness = await store.get_user_strictness(db, current_user)
    return {
        "strictness": strictness,
        "options": list(STRICTNESS_LEVELS),
        "default": DEFAULT_STRICTNESS,
    }


@router.put("/settings")
async def put_settings(body: SettingsRequest,
                       current_user: str = Depends(get_current_user)):
    if body.strictness not in STRICTNESS_LEVELS:
        raise HTTPException(status_code=400, detail=f"strictness must be one of {list(STRICTNESS_LEVELS)}")
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await store.set_user_strictness(db, current_user, body.strictness)
    return {"ok": True, "strictness": body.strictness}
