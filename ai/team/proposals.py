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

Undo exists for one kind of applied change: stock card lines Media Buyer applied on approval. The apply
turn records them on the proposal as `applied: {type: "card_lines", card_id, line_ids}`, and undo runs
each line through the card's own undo (ai/agents/stock/apply.undo_line: its window, lease and checks).
Anything else answers `can_undo: false` and 409 cannot_undo.
"""

from datetime import datetime

from pymongo import ReturnDocument

LOG = "ai_conversation_log"
PENDING, APPROVED, DISMISSED, UNDONE, EXPIRED = "pending", "approved", "dismissed", "undone", "expired"
STATUSES = (PENDING, APPROVED, DISMISSED, UNDONE)
ORIGIN_CHAT, ORIGIN_RUN = "chat", "sub_agent_run"
CARD_LINES = "card_lines"
LIST_MAX = 200


def _runs() -> str:
    from ai.agents.sub_agents.schedule import RUNS
    return RUNS


def _collections():
    return ((LOG, ORIGIN_CHAT), (_runs(), ORIGIN_RUN))


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


# ── the card a Media Buyer proposal names ─────────────────────────────────────

def card_ref(proposal: dict) -> tuple[str | None, list[str] | None]:
    """(card_id, line_ids) a Media Buyer proposal names, from its payload."""
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


async def can_undo(db, user_id: str, proposal: dict) -> bool:
    applied = proposal.get("applied")
    if proposal.get("status") != APPROVED or not isinstance(applied, dict) or applied.get("type") != CARD_LINES:
        return False
    return await _card_undoable(db, user_id, applied)


async def undo(db, mongo_client, user_id: str, proposal_id: str) -> tuple[int, dict]:
    """Undo an approved proposal through its change's own undo path. (status code, body)."""
    from ai.agents.stock import apply as stock_apply
    found = await find(db, user_id, proposal_id)
    if found is None:
        return 404, {"detail": "proposal_not_found", "code": "not_found"}
    _, _, proposal = found
    if not await can_undo(db, user_id, proposal):
        return 409, {"detail": "This change can't be undone from here.", "code": "cannot_undo"}
    applied = proposal["applied"]
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
        if p.get("kind") == "sub_agent":
            sid = str(((p.get("payload") or {}).get("sub_agent") or {}).get("id") or "")
            p["status"] = subs.get(sid, p.get("status"))
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
