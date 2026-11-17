"""
ai/suggestions/views.py
------------------------
How a suggestion is shown to the outside (dashboard route, MCP tools) and the words used when a decision on it
can't go ahead. routers/dashboard.py and ai/mcp/suggestions_mcp.py both use this so the two never drift.
"""

from ai.suggestions import store

NOT_FOUND_MESSAGE = "Suggestion not found"
APPLY_FAILED_MESSAGE = "Action failed"
UNDO_FAILED_MESSAGE = "Undo failed"
BUSY_MESSAGE = "Applying right now — try again in a moment"
HANDLED_MESSAGE = "This item was already handled"
NOTHING_TO_UNDO_MESSAGE = "Nothing to undo"

# Recently applied suggestions stay listed (with a lasting Undo) for this long.
RECENT_APPLIED_HOURS = 24


def rule_kind(agent: str | None) -> str | None:
    """'media_buying:pause_over_target' -> 'pause_over_target'; None for legacy agents."""
    if agent and agent.startswith("media_buying:"):
        return agent.split(":", 1)[1]
    return None


def suggestion_to_api(doc: dict) -> dict:
    return {
        "id": doc.get("id"),
        "status": doc.get("status", store.STATUS_OPEN),
        "applied_count": len(doc.get("applied_targets") or []),
        "severity": doc.get("severity"),
        "icon": doc.get("icon", "sparkles"),
        "client": doc.get("client_name"),
        "platform": doc.get("platform", "Meta Ads"),
        "title": doc.get("title"),
        "description": doc.get("description"),
        "stats": doc.get("stats") or [],
        "window": doc.get("window"),
        "agent_id": doc.get("agent_id"),
        "rule_kind": rule_kind(doc.get("agent")),
        "rule_mode": doc.get("rule_mode"),
        # False for flag-only items (nothing to write in Meta) — the work list
        # shows "Got it" instead of Approve/Deny.
        "has_action": bool(doc.get("action")),
        "blocked_reason": doc.get("blocked_reason"),
        "auto_failed": bool(doc.get("auto_failed")),
        "undone_at": doc.get("undone_at"),
        "applied_at": doc.get("applied_at"),
        "creative_test_id": (doc.get("evidence") or {}).get("creative_test_id"),
        "rule_id": doc.get("rule_id"),
    }


async def list_open(db, user_id: str, limit: int = 50) -> list[dict]:
    return await store.list_open_suggestions(db, user_id, limit=limit)


async def list_recent_applied(db, user_id: str) -> list[dict]:
    return await store.list_recent_applied(db, user_id, within_hours=RECENT_APPLIED_HOURS, limit=20)


def decided_by(res: dict) -> dict | None:
    d = res.get("decided_by")
    return {"actor": d.get("actor"), "source": d.get("source")} if isinstance(d, dict) else None


def conflict_detail(res: dict) -> str:
    """The message for a decision on an item that is no longer open."""
    return res.get("detail") or HANDLED_MESSAGE
