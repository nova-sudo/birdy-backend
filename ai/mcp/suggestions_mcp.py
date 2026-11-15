"""
ai/mcp/suggestions_mcp.py
--------------------------
Tools for the owner's dashboard suggestions (the work items Birdy's agents raise): list them (read), decline one
(write), and approve or undo one (live, two-step). They use the same service as the dashboard routes and the Slack
buttons (ai/suggestions/actions.py) and the same words (ai/suggestions/views.py).
"""

from typing import Literal

from fastmcp.exceptions import ToolError

from ai.agents.defaults import DENY_REASONS
from ai.mcp.access import LIVE, READ, WRITE
from ai.mcp.confirm import two_step
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.suggestions import actions, store, views
from ai.suggestions.contracts import ACTION_ENABLE_ADS, ACTION_PAUSE_ADS
from core.database import get_db
from core.mongo_client import get_shared_mongo_client

SOURCE = "mcp"
_INVERSE = {ACTION_PAUSE_ADS: ACTION_ENABLE_ADS, ACTION_ENABLE_ADS: ACTION_PAUSE_ADS}


def _error(outcome: str, message: str, **extra) -> dict:
    return {"ok": False, "outcome": outcome, "error": message, **extra}


async def _find(db, user_id: str, suggestion_id: str) -> dict:
    doc = await store.get_suggestion(db, user_id, suggestion_id)
    if not doc:
        raise ToolError(views.NOT_FOUND_MESSAGE)
    return doc


def _touches(targets: list) -> list[dict]:
    return [{"id": t.get("object_id"), "type": t.get("object_type"), "name": t.get("name")} for t in targets or []]


def _refusal(res: dict, fallback: str) -> dict:
    """A service outcome that did not go through, as an error dict with the routes' wording."""
    outcome = res.get("outcome")
    if outcome == "not_found":
        raise ToolError(views.NOT_FOUND_MESSAGE)
    if outcome == "busy":
        return _error(outcome, views.BUSY_MESSAGE)
    extra = {"status": res["status"]} if res.get("status") else {}
    if res.get("blocked_reason"):
        extra["blocked_reason"] = res["blocked_reason"]
    if res.get("decided_by"):
        extra["decided_by"] = views.decided_by(res)
    if outcome == "closed":
        return _error(outcome, views.conflict_detail(res), **extra)
    return _error(outcome, res.get("detail") or fallback, **extra)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@mcp.tool(**READ)
async def get_suggestions(status: Literal["open", "applied", "all"] = "open") -> list[dict]:
    """The owner's dashboard suggestions, newest first: what Birdy's agents found, with a title, description, key stats and whether there is an action to approve.

    Use it to find a suggestion's id before apply_suggestion, deny_suggestion or undo_suggestion.

    Args:
        status: open (default, waiting on the owner), applied (applied in the last 24 hours, can still be undone) or all.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    docs = []
    if status in ("open", "all"):
        docs += await views.list_open(db, user_id)
    if status in ("applied", "all"):
        docs += await views.list_recent_applied(db, user_id)
    return [views.suggestion_to_api(d) for d in docs]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

@mcp.tool(**WRITE)
async def deny_suggestion(suggestion_id: str, reason_code: str, note: str | None = None) -> dict:
    """Decline a suggestion with a reason. Nothing changes in Meta. If it came from an agent, the reason (and note) teach that agent.

    reason_code must be one of: still_learning, client_wants_on, creative_stale, target_wrong, other. A suggestion that is already closed or applied returns `ok: false` with the reason.

    Args:
        suggestion_id: The suggestion's id (from get_suggestions).
        reason_code: still_learning, client_wants_on, creative_stale, target_wrong or other.
        note: Optional extra context in the owner's words (up to 2000 characters), remembered by the agent.
    """
    if reason_code not in DENY_REASONS:
        raise ToolError(f"reason_code must be one of {list(DENY_REASONS)}")
    if note is not None and len(note) > 2000:
        raise ToolError("note is too long (2000 characters at most)")
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    await _find(db, user_id, suggestion_id)
    res = await actions.dismiss_suggestion(db, get_shared_mongo_client(), user_id, suggestion_id,
                                           actor=store.ACTOR_USER, source=SOURCE, reason_code=reason_code, note=note)
    if not res.get("ok"):
        return _refusal(res, views.HANDLED_MESSAGE)
    mem = res.get("memory")
    memory = ({"id": mem.get("_id"), "text": mem.get("text"), "kind": mem.get("kind"), "filter": mem.get("filter")}
              if mem else None)
    return {"ok": True, "outcome": res.get("outcome"), "memory": memory, "summary": "Declined the suggestion."}


# ---------------------------------------------------------------------------
# Live (two steps)
# ---------------------------------------------------------------------------

@mcp.tool(**LIVE)
async def apply_suggestion(suggestion_id: str, confirm_code: str | None = None) -> dict:
    """Approve a suggestion and run its action in the owner's live Meta ads (for example pausing the ads it names).

    Two steps. Called without confirm_code it changes nothing and returns a preview (the action and the ads or campaigns it touches, with the stats behind it) and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. A suggestion that is already closed or being applied right now returns `ok: false` with the reason and no code. A flag-only suggestion (nothing to write in Meta) is just acknowledged: the preview says so and approving marks it done. A guardrail on the agent can also block it (`blocked_reason`); the suggestion then stays open.

    Args:
        suggestion_id: The suggestion's id (from get_suggestions).
        confirm_code: The code from the preview, once the user has agreed.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    doc = await _find(db, user_id, suggestion_id)
    if not confirm_code:
        # Say it now if it can't work, so the user is never asked to confirm something that will be refused.
        status = doc.get("status")
        if status in (store.STATUS_DISMISSED, store.STATUS_RESOLVED):
            return _error("closed", actions.CLOSED_MESSAGE, status=status)
        if status == store.STATUS_APPLIED:
            return {"ok": True, "outcome": "already", "status": status, "summary": "It was already applied; nothing to confirm."}
        if store.has_live_claim(doc):
            return _error("busy", views.BUSY_MESSAGE)

    async def preview():
        action = doc.get("action") or {}
        flag_only = not action
        return {"suggestion_id": suggestion_id, "title": doc.get("title"), "client": doc.get("client_name"),
                "description": doc.get("description"), "action": action.get("type"),
                **({"summary": "Acknowledge: nothing changes in Meta"} if flag_only else {}),
                "touches": _touches(action.get("targets")), "params": action.get("params") or {},
                "stats": doc.get("stats") or [], "window": doc.get("window")}

    async def execute():
        res = await actions.apply_suggestion(db, get_shared_mongo_client(), user_id, suggestion_id,
                                             actor=store.ACTOR_USER, source=SOURCE)
        if res["outcome"] in ("not_found", "failed", "closed", "busy", "blocked") or not res.get("ok"):
            return _refusal(res, views.APPLY_FAILED_MESSAGE)
        return {"ok": True, "outcome": res["outcome"], "succeeded": res.get("succeeded", []),
                "failed": res.get("failed", []), "status": res.get("status"),
                "decided_by": views.decided_by(res), "redirect": res.get("redirect"),
                "summary": "Applied the suggestion in Meta." if res["outcome"] != "already" else "It was already applied."}

    return await two_step(db, user_id, "apply_suggestion", {"suggestion_id": suggestion_id}, confirm_code, preview, execute)


@mcp.tool(**LIVE)
async def undo_suggestion(suggestion_id: str, confirm_code: str | None = None) -> dict:
    """Reverse an applied suggestion: turn back on exactly the ads it paused (or pause what it turned on) and reopen the suggestion.

    Two steps. Called without confirm_code it changes nothing and returns a preview of what will change and a one-time confirm_code. Show the preview to the user and only call again with confirm_code after they say yes.
    The second call must repeat the same arguments. Something that was never applied is a no-op, and an action that can't be reversed (such as a creative post) returns `ok: false` with the reason and no code.

    Args:
        suggestion_id: The suggestion's id (from get_suggestions with status applied).
        confirm_code: The code from the preview, once the user has agreed.
    """
    user_id = _current_user_id()
    db = get_db(get_shared_mongo_client())
    doc = await _find(db, user_id, suggestion_id)
    applied_type = doc.get("applied_action_type") or (doc.get("action") or {}).get("type")
    if not confirm_code:
        if doc.get("status") != store.STATUS_APPLIED:
            return {"ok": True, "outcome": "noop", "succeeded": [], "summary": "Nothing is applied, so there is nothing to undo."}
        if applied_type is not None and applied_type not in _INVERSE:
            return _error("not_undoable", views.NOTHING_TO_UNDO_MESSAGE)
        if applied_type is None and doc.get("applied_targets"):
            return _error("not_undoable", views.NOTHING_TO_UNDO_MESSAGE)

    async def preview():
        inverse = _INVERSE.get(applied_type)
        return {"suggestion_id": suggestion_id, "title": doc.get("title"), "client": doc.get("client_name"),
                "action": inverse, **({"summary": "Reopen: nothing changes in Meta"} if inverse is None else {}), "touches": _touches(doc.get("applied_targets")), "stats": doc.get("stats") or [],
                "applied_at": doc.get("applied_at")}

    async def execute():
        res = await actions.undo_suggestion(db, get_shared_mongo_client(), user_id, suggestion_id,
                                            actor=store.ACTOR_USER, source=SOURCE)
        if not res.get("ok"):
            return _refusal(res, views.UNDO_FAILED_MESSAGE)
        return {"ok": True, "outcome": res["outcome"], "succeeded": res.get("succeeded", []),
                "summary": "Undid the suggestion in Meta." if res["outcome"] == "undone" else "Nothing was applied; nothing changed."}

    return await two_step(db, user_id, "undo_suggestion", {"suggestion_id": suggestion_id}, confirm_code, preview, execute)
