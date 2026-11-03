"""
ai/agents/stock/cards.py
------------------------
The stock agent's cards (spec section 7): one per client per run. `build_card` is pure. Fixed code
writes every no-changes reason, so a green or held client never needs AI, and a line's mode is
resolved here, once, per line.
"""

import copy
from datetime import datetime

from ai.agents.stock.settings import resolve_mode
from ai.agents.stock.writer import _fmt_money
from ai.agents.store import CARDS

STATUS_OPEN, STATUS_FLAGGED, STATUS_SUPERSEDED = "open", "flagged", "superseded"
STATE_PROPOSED, STATE_FLAGGED = "proposed", "flagged"
CHANGE_DECISION = "propose"
_EDITABLE = ("lines", "additional_changes", "thought_process", "next_steps")


def no_change_reason(facts: dict, decision: str) -> str:
    """The plain sentence on a card with no changes. Always says why, and gives the CPL when there
    is one to judge."""
    cur = facts.get("currency")
    cpl, prev = _fmt_money(facts.get("cpl"), cur), _fmt_money(facts.get("prev_cpl"), cur)
    zone = facts.get("zone")
    if decision == "spend_stopped":
        return "Spend has stopped: nothing was spent on the last 2 full days. No changes."
    if zone == "no_data":
        return "No data: this client had no spend in the window. No changes."
    if facts.get("cpl") is None:
        return f"Spent {_fmt_money(facts.get('spend'), cur)} with no leads, which is in the red zone. No changes."
    if zone == "green":
        return (f"CPL is {cpl}, in the green zone (under "
                f"{_fmt_money((facts.get('zones') or {}).get('green_under'), cur)}). No changes.")
    if decision == "hold_downtrend":
        return f"CPL is {cpl}, in the red zone but on a major downtrend from {prev}. No changes."
    if decision == CHANGE_DECISION:
        return f"CPL is {cpl}, in the {zone} zone. Birdy found no change worth making."
    if facts.get("prev_cpl") is None:
        return f"CPL is {cpl}, in the {zone} zone, with no earlier CPL to compare. No changes."
    return f"CPL is {cpl}, in the {zone} zone and not rising (it was {prev}). No changes."


def _next_if_it_stalls(candidates: dict | None) -> str:
    """Red on a major downtrend: what goes in next if the trend stalls."""
    best = ((candidates or {}).get("add") or []) + ((candidates or {}).get("revive") or [])
    if best:
        return f"If it stalls, {best[0].get('ad_name') or 'the top allowed ad'} goes in next."
    return "If it stalls, Birdy will propose changes on the next batch day."


def _line(n: int, kind: str, ad_id: str | None, name: str | None, mode: str, flags: list[dict], **extra) -> dict:
    return {"id": f"l{n}", "kind": kind, "ad_name": name or ad_id or "", "mode": mode,
            "state": STATE_FLAGGED if mode == "flag" else STATE_PROPOSED, "error": None,
            "meta_ids": [ad_id] if ad_id else [], "flags": flags, **extra}


def _lines(facts: dict, draft: dict, settings: dict, candidates: dict | None, flag_only: bool) -> list[dict]:
    gid = facts["client_group_id"]
    cands = candidates or {}
    names = {a.get("ad_id"): a.get("name") for a in facts.get("ads") or []}
    notes: dict[str, list[dict]] = {}
    for f in draft.get("flags") or []:
        notes.setdefault(f.get("id"), []).append({"kind": f.get("kind"), "note": f.get("note")})

    def mode(kind: str) -> str:
        return "flag" if flag_only else resolve_mode(settings, gid, kind)

    out: list[dict] = []
    by_candidate = {c.get("candidate_id"): c for c in cands.get("add") or []}
    for cid in draft.get("add") or []:
        c = by_candidate.get(cid) or {}
        # No ad id yet: Add creates one. Everything the post needs later travels on the line.
        out.append(_line(len(out) + 1, "add", None, c.get("ad_name") or cid, mode("add"), notes.get(cid, []),
                         source=c.get("source_name"), record=c.get("record"), candidate_id=cid,
                         source_client_group_id=c.get("source_client_group_id"), source_ad_id=c.get("ad_id"),
                         creative_key=c.get("creative_key"), creative=c.get("creative"),
                         treatment_ids=c.get("treatment_ids") or [], currency=c.get("currency"),
                         spend=c.get("spend"), leads=c.get("leads"), cpl=c.get("cpl")))
    for kind in ("remove", "revive"):
        by_id = {c.get("ad_id"): c for c in cands.get(kind) or []}
        for ad_id in draft.get(kind) or []:
            c = by_id.get(ad_id) or {}
            out.append(_line(len(out) + 1, kind, ad_id, c.get("ad_name") or names.get(ad_id), mode(kind),
                             notes.get(ad_id, []), source=None, record=None, currency=facts.get("currency"),
                             spend=c.get("spend"), leads=c.get("leads"), cpl=c.get("cpl")))
    return out


def build_card(facts: dict, decision: str, draft: dict | None, settings: dict, run_id: str, *,
               candidates: dict | None = None, flag_only: bool = False) -> dict:
    """The card for one client, shaped as in spec section 7. `draft` is write_card's output, or
    None for a card that needs no AI. Only a "propose" decision with a draft that didn't fail
    makes lines, so a failed draft can never reach Auto. `candidates` names the chosen ads.
    `flag_only` is for a client this agent watches without acting on: every line is a Flag."""
    draft = draft or {}
    failed = bool(draft.get("failed"))
    usable = decision == CHANGE_DECISION and not failed
    lines = _lines(facts, draft, settings, candidates, flag_only) if usable else []
    additional = draft.get("additional_changes") or "" if usable else ""

    if lines or additional:
        tag = "changes"
    elif facts.get("zone") == "green" and decision == "none":
        tag = "green"
    else:
        tag = "no_changes"

    written = draft.get("thought_process") or ""
    if failed or tag == "changes":
        thought = written
    else:
        thought = " ".join(t for t in (no_change_reason(facts, decision), written) if t)
    next_steps = draft.get("next_steps") or ""
    if decision == "hold_downtrend":
        next_steps = _next_if_it_stalls(candidates)

    all_flagged = flag_only or (bool(lines) and all(ln["mode"] == "flag" for ln in lines))
    card = {"run_id": run_id, "client_group_id": facts["client_group_id"], "client_name": facts.get("name"),
            "tag": tag, "decision": decision, "zone": facts.get("zone"), "window": facts.get("window"),
            "previous_cpl": facts.get("prev_cpl"), "current_cpl": facts.get("cpl"), "currency": facts.get("currency"),
            "lines": lines, "additional_changes": additional, "thought_process": thought, "next_steps": next_steps,
            "status": STATUS_FLAGGED if all_flagged else STATUS_OPEN, "ai_failed": failed,
            "slack": {"channel_id": None, "ts": None}, "decided_by": None, "decided_at": None}
    card["original"] = copy.deepcopy({k: card[k] for k in _EDITABLE})
    return card


async def supersede_open(db, agent_id: str, gid: str, run_id: str) -> int:
    """A newer card for a client closes its open cards, so an old card can't be applied. This
    run's own card, and previews, are left alone."""
    res = await db[CARDS].update_many(
        {"agent_id": agent_id, "client_group_id": gid, "status": STATUS_OPEN, "run_id": {"$ne": run_id},
         "preview": {"$ne": True}},
        {"$set": {"status": STATUS_SUPERSEDED, "superseded_by_run": run_id, "updated_at": datetime.utcnow()}})
    return res.modified_count
