"""
ai/team/proposals.py
--------------------
Where agents' proposals live and how they move (design section 6). Nothing here makes a change: an
approval still hands the proposal to its owning agent's own chat turn (routers/chat.apply_proposal).

Stored on the row that made them, so a reopened conversation shows each one's current status:
  ai_conversation_log   an agent reply's `proposals`            origin "chat"           (session_id)
  sub_agent_runs        a scheduled/owner-pressed run's          origin "sub_agent_run"  (sub_agent_id)

Statuses: pending -> approved | dismissed; approved -> undone (only through an existing undo path).
A "sub_agent" proposal (a new sub-agent) is decided by POST /api/agents/sub/{id}/approve|reject, so its
status is read from the sub-agent record: approved once it exists past "proposed", dismissed once it is
gone, expired once it lapsed.

Undo, per kind of applied change:
- Stock card lines Marketing Guru applied on approval: recorded as `applied: {type: "card_lines", card_id,
  line_ids}`; undo runs each line through the card's own undo (ai/agents/stock/apply.undo_line: its
  window, lease and checks).
- Everything else the owning agent changes while applying (the approval turn and later turns in that
  same chat, e.g. after the owner's "yes"): each write tool records what it did with `record()` while a
  journal is open, and the proposal keeps them as `applied: {type: "changes", changes: [...]}`. Undo
  reverses each exactly: a saved lesson is removed, an edited lesson gets its previous words back, a
  removed lesson comes back, a new creative-test draft is archived, an edited draft or changed test
  targets get their previous values back. It refuses (cannot_undo) when the thing was changed again
  since, and for changes with no exact reverse: a test posted or scaled on Meta, ads paused on Meta,
  calls already transcribed (credits spent). Those are recorded too, so undo never claims to have
  reversed them.
- A new sub-agent ("sub_agent" proposal, approved on its own card): undo pauses it, if it is still
  active. Pausing keeps everything (instructions, lessons, runs); deleting would lose them.
Anything else answers `can_undo: false` and 409 cannot_undo.
"""

from contextvars import ContextVar
from datetime import datetime

from pymongo import ReturnDocument

LOG = "ai_conversation_log"
PENDING, APPROVED, DISMISSED, UNDONE, EXPIRED = "pending", "approved", "dismissed", "undone", "expired"
STATUSES = (PENDING, APPROVED, DISMISSED, UNDONE)
ORIGIN_CHAT, ORIGIN_RUN = "chat", "sub_agent_run"
CARD_LINES = "card_lines"
CHANGES = "changes"
SUB_AGENT_KIND = "sub_agent"
LIST_MAX = 200

# What a write tool did, as `applied.changes` entries ({"op": ..., what its undo needs}).
LESSON_SAVED, LESSON_EDITED, LESSON_REMOVED = "lesson_saved", "lesson_edited", "lesson_removed"
DRAFT_CREATED, DRAFT_EDITED, TARGETS_CHANGED = "creative_draft_created", "creative_draft_edited", "creative_targets"
TEST_POSTED, TEST_SCALED, ADS_PAUSED, CALLS_ANALYZED = ("creative_test_posted", "creative_test_scaled",
                                                       "creative_ads_paused", "calls_analyzed")
# Changes with no exact reverse, and why.
NO_REVERSE = {
    TEST_POSTED: "The test was posted to Meta; take it down from the test's page instead.",
    TEST_SCALED: "Ads were added on Meta and may have spent; pause them from the test's page instead.",
    ADS_PAUSED: "Ads were paused on Meta; turning them back on spends money, so ask Marketing Guru to do it.",
    CALLS_ANALYZED: "The calls were already transcribed and the credits spent.",
}


def _runs() -> str:
    from ai.agents.sub_agents.schedule import RUNS
    return RUNS


def _collections():
    return ((LOG, ORIGIN_CHAT), (_runs(), ORIGIN_RUN))


# ── the journal: what the owning agent changed while applying ────────────────
# routers/chat.py opens one (a list) around the approval turn and around later turns in the chat that
# applied it; write tools append to it with record(). None means no proposal is being applied.
current_journal: ContextVar[list | None] = ContextVar("current_proposal_journal", default=None)


def record(change: dict) -> None:
    """A write tool says what it changed. Does nothing outside a proposal's apply turns."""
    journal = current_journal.get()
    if journal is not None:
        journal.append(dict(change))


async def mark_apply_session(db, user_id: str, session_id: str | None, proposal_id: str) -> None:
    """Remember which proposal this chat applied, so later turns in it (the owner's "yes") are kept too."""
    if session_id:
        await db[LOG].update_many({"user_id": user_id, "session_id": session_id},
                                  {"$set": {"applies_proposal": proposal_id}})


async def applying_in(db, user_id: str, session_id: str | None) -> str | None:
    """The approved proposal this chat is applying, or None."""
    if not session_id:
        return None
    row = await db[LOG].find_one({"user_id": user_id, "session_id": session_id,
                                  "applies_proposal": {"$exists": True}}, {"applies_proposal": 1})
    return (row or {}).get("applies_proposal")


async def add_changes(db, user_id: str, proposal_id: str, changes: list[dict]) -> None:
    """Keep changes made while applying an approved proposal. A card-lines proposal keeps its own record."""
    if not changes:
        return
    found = await find(db, user_id, proposal_id)
    if found is None:
        return
    proposal = found[2]
    if proposal.get("status") != APPROVED:
        return
    applied = proposal.get("applied") if isinstance(proposal.get("applied"), dict) else None
    if applied and applied.get("type") != CHANGES:
        return
    applied = {"type": CHANGES, "changes": [*((applied or {}).get("changes") or []), *changes]}
    await set_fields(db, user_id, proposal_id, {"applied": applied})


# ── finding and moving one proposal ───────────────────────────────────────────

async def find(db, user_id: str, proposal_id: str) -> tuple[str, dict, dict] | None:
    """(collection, row, proposal) for one of the user's proposals, or None."""
    for coll, _ in _collections():
        row = await db[coll].find_one({"user_id": user_id, "proposals.id": proposal_id})
        if row:
            return coll, row, next(p for p in row["proposals"] if p.get("id") == proposal_id)
    return None


async def claim(db, user_id: str, proposal_id: str, from_status: str, to_status: str) -> dict | None:
    """Move one proposal from `from_status` to `to_status`, atomically. The proposal after, or None."""
    for coll, _ in _collections():
        row = await db[coll].find_one_and_update(
            {"user_id": user_id, "proposals": {"$elemMatch": {"id": proposal_id, "status": from_status}}},
            {"$set": {"proposals.$.status": to_status, "proposals.$.decided_at": datetime.utcnow()}},
            return_document=ReturnDocument.AFTER)
        if row:
            return next(p for p in row["proposals"] if p.get("id") == proposal_id)
    return None


async def set_fields(db, user_id: str, proposal_id: str, fields: dict) -> None:
    for coll, _ in _collections():
        res = await db[coll].update_one({"user_id": user_id, "proposals.id": proposal_id},
                                        {"$set": {f"proposals.$.{k}": v for k, v in fields.items()}})
        if res.matched_count:
            return


# ── the card a Marketing Guru proposal names ─────────────────────────────────────

def card_ref(proposal: dict) -> tuple[str | None, list[str] | None]:
    """(card_id, line_ids) a Marketing Guru proposal names, from its payload."""
    payload = proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {}
    card_id = payload.get("card_id")
    lines = payload.get("line_ids")
    if not lines and payload.get("line_id"):
        lines = [payload["line_id"]]
    lines = [str(x) for x in lines] if isinstance(lines, list) else None
    return (str(card_id) if card_id else None), (lines or None)


def line_states(card: dict | None) -> dict:
    return {ln.get("id"): ln.get("state") for ln in (card or {}).get("lines") or [] if isinstance(ln, dict)}


def applied_lines(before: dict, after: dict) -> list[str]:
    """Lines that went out during the approval turn (applied, or on their way)."""
    from ai.agents.stock.cards import STATE_APPLIED, STATE_APPLYING, STATE_QUEUED
    going = (STATE_APPLIED, STATE_QUEUED, STATE_APPLYING)
    return [lid for lid, state in after.items() if state in going and before.get(lid) != state]


async def _card_undoable(db, user_id: str, applied: dict) -> bool:
    from ai.agents.outcomes import UNDO_AUTO_WINDOW
    from ai.agents.stock.apply import WRITABLE_KINDS
    from ai.agents.stock.cards import STATE_APPLIED
    from ai.agents.store import CARDS
    card = await db[CARDS].find_one({"_id": applied.get("card_id"), "user_id": user_id}, {"lines": 1})
    now = datetime.utcnow()
    for ln in (card or {}).get("lines") or []:
        if ln.get("id") not in (applied.get("line_ids") or []):
            continue
        if ln.get("state") != STATE_APPLIED or ln.get("kind") not in WRITABLE_KINDS:
            continue
        until = ln.get("undo_until") or ((ln.get("applied_at") or datetime.min) + UNDO_AUTO_WINDOW)
        if now <= until:
            return True
    return False


# ── undoing recorded changes ──────────────────────────────────────────────────

def _lesson_q(change: dict) -> dict:
    return {"_id": change.get("lesson_id"), "agent_id": change.get("agent_id")}


async def _check_change(db, user_id: str, c: dict) -> str | None:
    """None when this change can still be reversed exactly; otherwise why not."""
    from ai.agents.store import MEMORY
    op = c.get("op")
    if c.get("undone_at"):
        return None
    if op in NO_REVERSE:
        return NO_REVERSE[op]
    if op in (LESSON_SAVED, LESSON_EDITED, LESSON_REMOVED):
        doc = await db[MEMORY].find_one(_lesson_q(c), {"text": 1, "active": 1, "status": 1})
        if doc is None:
            return "That lesson is gone."
        if op == LESSON_SAVED:
            if not doc.get("active"):
                return "That lesson was removed since."
            return None if doc.get("text") == c.get("text") else "That lesson was edited since."
        if op == LESSON_EDITED:
            return None if doc.get("text") == c.get("after_text") else "That lesson was edited again since."
        if doc.get("active"):
            return "That lesson is back already."
        same = doc.get("text") == c.get("text") and doc.get("status") == c.get("status")
        return None if same else "That lesson was changed since."
    if op in (DRAFT_CREATED, DRAFT_EDITED, TARGETS_CHANGED):
        from services.creatives import store as ct_store
        test = await db[ct_store.TESTS].find_one({"id": c.get("test_id"), "user_id": user_id},
                                                 {"status": 1, "updated_at": 1})
        if test is None:
            return "That creative test is gone."
        if op != TARGETS_CHANGED and test.get("status") != ct_store.DRAFT:
            return "That draft has been posted or archived since."
        if op == TARGETS_CHANGED and test.get("status") == ct_store.ARCHIVED:
            return "That creative test is archived."
        return None if test.get("updated_at") == c.get("updated_at") else "That creative test was changed since."
    return "This change can't be undone from here."


async def _reverse_change(db, user_id: str, c: dict) -> bool:
    """Reverse one change, only if it is still exactly as the apply left it. True when reversed."""
    from ai.agents.store import MEMORY
    op, now = c.get("op"), datetime.utcnow()
    if op == LESSON_SAVED:
        res = await db[MEMORY].update_one({**_lesson_q(c), "active": True, "text": c.get("text")},
                                          {"$set": {"active": False, "undone_at": now}})
        return res.modified_count > 0
    if op == LESSON_EDITED:
        before = c.get("before") or {}
        res = await db[MEMORY].update_one(
            {**_lesson_q(c), "text": c.get("after_text")},
            {"$set": {"text": before.get("text"), "source": before.get("source"), "edited_at": now},
             "$push": {"previous_texts": {"text": c.get("after_text"), "source": "chat_edit",
                                          "replaced_at": now, "undone": True}}})
        return res.modified_count > 0
    if op == LESSON_REMOVED:
        res = await db[MEMORY].update_one(
            {**_lesson_q(c), "active": False, "text": c.get("text"), "status": c.get("status")},
            {"$set": {"active": True}})
        return res.modified_count > 0
    if op in (DRAFT_CREATED, DRAFT_EDITED, TARGETS_CHANGED):
        from services.creatives import store as ct_store
        q = {"id": c.get("test_id"), "user_id": user_id, "updated_at": c.get("updated_at")}
        if op == DRAFT_CREATED:
            update = {"$set": {"status": ct_store.ARCHIVED, "archived_at": now, "updated_at": now}}
        else:
            before = dict(c.get("before") or {})
            # the previous updated_at too, so an older change to this test still matches what it left
            update = {"$set": {**before, "updated_at": c.get("before_updated_at") or now}}
            if c.get("unset"):
                update["$unset"] = {k: "" for k in c["unset"]}
        q["status"] = ct_store.DRAFT if op != TARGETS_CHANGED else {"$ne": ct_store.ARCHIVED}
        res = await db[ct_store.TESTS].update_one(q, update)
        return res.modified_count > 0
    return False


async def _changes_refusal(db, user_id: str, applied: dict) -> str | None:
    changes = applied.get("changes") or []
    if not changes or all(c.get("undone_at") for c in changes):
        return "Nothing is left to undo."
    # Only the newest change to each thing is checked against what is stored now: undo unwinds newest
    # first, so each older change finds its thing exactly as it left it (saved, then edited, then removed).
    seen = set()
    for c in reversed(changes):
        if c.get("undone_at"):
            continue
        key = (c.get("lesson_id"), c.get("test_id"))
        if key != (None, None) and key in seen and c.get("op") not in NO_REVERSE:
            continue
        seen.add(key)
        why = await _check_change(db, user_id, c)
        if why:
            return why
    return None


# ── a new sub-agent ───────────────────────────────────────────────────────────

def _sub_id(proposal: dict) -> str:
    return str(((proposal.get("payload") or {}).get("sub_agent") or {}).get("id") or "")


async def _sub_refusal(db, user_id: str, proposal: dict) -> str | None:
    from ai.agents.sub_agents.core import ACTIVE, SUB_AGENTS
    if proposal.get("status") == UNDONE:
        return "This change can't be undone from here."
    doc = await db[SUB_AGENTS].find_one({"_id": _sub_id(proposal), "user_id": user_id}, {"status": 1})
    if doc is None:
        return "That sub-agent is gone."
    if doc.get("status") != ACTIVE:
        return "That sub-agent is not active (still a proposal, or paused since)."
    return None


# ── can it be undone, and undo ────────────────────────────────────────────────

async def refusal(db, user_id: str, proposal: dict) -> str | None:
    """None when this proposal's change can be undone now; otherwise why not."""
    if proposal.get("kind") == SUB_AGENT_KIND:
        return await _sub_refusal(db, user_id, proposal)
    applied = proposal.get("applied")
    if proposal.get("status") != APPROVED or not isinstance(applied, dict):
        return "This change can't be undone from here."
    if applied.get("type") == CARD_LINES:
        return None if await _card_undoable(db, user_id, applied) else "This change can't be undone from here."
    if applied.get("type") == CHANGES:
        return await _changes_refusal(db, user_id, applied)
    return "This change can't be undone from here."


async def can_undo(db, user_id: str, proposal: dict) -> bool:
    return await refusal(db, user_id, proposal) is None


async def _undo_sub_agent(db, user_id: str, proposal_id: str, proposal: dict) -> tuple[int, dict]:
    from ai.agents.sub_agents.core import SubAgentError, pause_sub_agent
    try:
        await pause_sub_agent(db, user_id, _sub_id(proposal))
    except SubAgentError as e:
        return 409, {"detail": e.body.get("detail") or "The sub-agent could not be paused.", "code": "undo_failed"}
    await set_fields(db, user_id, proposal_id, {"status": UNDONE, "decided_at": datetime.utcnow()})
    return 200, {"ok": True, "id": proposal_id, "status": UNDONE, "sub_agent": {"id": _sub_id(proposal),
                                                                               "status": "paused"}}


async def _undo_changes(db, user_id: str, proposal_id: str, applied: dict) -> tuple[int, dict]:
    changes = [dict(c) for c in applied.get("changes") or []]
    out = []
    # newest first, so a lesson saved then edited in the same chat unwinds in order
    for c in reversed(changes):
        if c.get("undone_at"):
            continue
        ok = await _reverse_change(db, user_id, c)
        if ok:
            c["undone_at"] = datetime.utcnow()
        out.append({"op": c.get("op"), "ok": ok,
                    "reason": None if ok else "It was changed while undoing; nothing more was changed."})
    await set_fields(db, user_id, proposal_id, {"applied": {**applied, "changes": changes}})
    if not all(x["ok"] for x in out):
        return 409, {"detail": next(x["reason"] for x in out if not x["ok"]), "code": "undo_failed", "changes": out}
    await claim(db, user_id, proposal_id, APPROVED, UNDONE)
    return 200, {"ok": True, "id": proposal_id, "status": UNDONE, "changes": out}


async def undo(db, mongo_client, user_id: str, proposal_id: str) -> tuple[int, dict]:
    """Undo an approved proposal through its change's own undo path. (status code, body)."""
    from ai.agents.stock import apply as stock_apply
    found = await find(db, user_id, proposal_id)
    if found is None:
        return 404, {"detail": "proposal_not_found", "code": "not_found"}
    _, _, proposal = found
    why = await refusal(db, user_id, proposal)
    if why is not None:
        return 409, {"detail": why, "code": "cannot_undo"}
    if proposal.get("kind") == SUB_AGENT_KIND:
        return await _undo_sub_agent(db, user_id, proposal_id, proposal)
    applied = proposal["applied"]
    if applied.get("type") == CHANGES:
        return await _undo_changes(db, user_id, proposal_id, applied)
    lines = []
    for line_id in applied.get("line_ids") or []:
        res = await stock_apply.undo_line(db, mongo_client, user_id, applied["card_id"], line_id,
                                          by=user_id, source="chat")
        lines.append({"line_id": line_id, "ok": bool(res.get("ok")), "outcome": res.get("outcome"),
                      "reason": res.get("reason")})
    if not any(x["outcome"] in ("undone", "queued", "already") for x in lines):
        reason = next((x["reason"] for x in lines if x["reason"]), None) or "The change could not be undone."
        return 409, {"detail": reason, "code": "undo_failed", "lines": lines}
    await claim(db, user_id, proposal_id, APPROVED, UNDONE)
    return 200, {"ok": True, "id": proposal_id, "status": UNDONE, "lines": lines}


# ── presenting proposals (lists and reopened conversations) ───────────────────

async def _sub_agent_statuses(db, user_id: str, proposals: list[dict]) -> dict:
    """sub-agent id -> the status its "sub_agent" proposal now has."""
    from ai.agents.sub_agents.core import PROPOSED, SUB_AGENTS, lapsed
    ids = [str(((p.get("payload") or {}).get("sub_agent") or {}).get("id") or "")
           for p in proposals if p.get("kind") == "sub_agent"]
    ids = [i for i in ids if i]
    if not ids:
        return {}
    docs = {d["_id"]: d for d in await db[SUB_AGENTS].find(
        {"_id": {"$in": ids}, "user_id": user_id}, {"status": 1, "created_at": 1}).to_list(length=None)}
    out = {}
    for i in ids:
        d = docs.get(i)
        if d is None:
            out[i] = DISMISSED
        elif lapsed(d):
            out[i] = EXPIRED
        elif d.get("status") == PROPOSED:
            out[i] = PENDING
        else:
            out[i] = APPROVED
    return out


async def present(db, user_id: str, proposals: list[dict], **origin) -> list[dict]:
    """Proposals as the app gets them: current status, `can_undo`, and where they came from."""
    subs = await _sub_agent_statuses(db, user_id, proposals)
    out = []
    for p in proposals or []:
        p = dict(p)
        if p.get("kind") == SUB_AGENT_KIND and p.get("status") != UNDONE:
            p["status"] = subs.get(_sub_id(p), p.get("status"))
        p["can_undo"] = await can_undo(db, user_id, p)
        p.update(origin)
        out.append(p)
    return out


async def list_proposals(db, user_id: str, *, status: str = PENDING, agent: str | None = None) -> list[dict]:
    """The user's proposals in `status` (from chats and from sub-agent runs), newest first."""
    rows = []
    q = {"user_id": user_id, "proposals.0": {"$exists": True}}
    async for r in db[LOG].find({**q, "role": "assistant"}, {"session_id": 1, "proposals": 1, "created_at": 1}):
        rows.append((r.get("created_at"), r["proposals"], {"origin": ORIGIN_CHAT, "session_id": r.get("session_id")}))
    async for r in db[_runs()].find(q, {"sub_agent_id": 1, "proposals": 1, "finished_at": 1, "claimed_at": 1}):
        rows.append((r.get("finished_at") or r.get("claimed_at"), r["proposals"],
                     {"origin": ORIGIN_RUN, "sub_agent_id": r.get("sub_agent_id"), "run_id": r["_id"]}))
    rows.sort(key=lambda x: x[0] or datetime.min, reverse=True)
    out = []
    for _, props, origin in rows:
        mine = [p for p in props if not agent or p.get("agent") == agent]
        for p in await present(db, user_id, mine, **origin):
            if p.get("status") == status:
                out.append(p)
        if len(out) >= LIST_MAX:
            break
    return out[:LIST_MAX]
