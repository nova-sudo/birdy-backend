"""
ai/agents/stock/scoreboard.py
-----------------------------
The stock agent's scoreboard store (spec section 9): one `agent_ad_outcomes` row per applied or
flagged Add, scored later. `record_for` is the read side the candidate ranking uses.
"""

OUTCOMES = "agent_ad_outcomes"
WON, LOST, UNDONE = "won", "lost", "undone"


async def record_for(db, agent_id, creative_key: str, treatment_ids: list[str] | None = None) -> dict:
    """How a creative has fared for this agent: {won, lost, moves}. `moves` counts every row.
    With `treatment_ids` only moves made in one of those treatments count. Rows still waiting to
    be scored, and Never went live rows, count as moves but as neither won nor lost."""
    q: dict = {"agent_id": agent_id, "creative_key": creative_key}
    if treatment_ids is not None:
        q["treatment_ids"] = {"$in": list(treatment_ids)}
    out = {"won": 0, "lost": 0, "moves": 0}
    async for row in db[OUTCOMES].find(q):
        out["moves"] += 1
        result = str(row.get("result") or "").lower()
        if result in (WON, LOST):
            out[result] += 1
    return out


async def record_add(db, card: dict, line: dict, ad_id: str, live_since) -> None:
    """The row for an Add that went live, waiting to be scored. One per line however often it's called.
    The baseline and currency are the destination's: the line's own spend and CPL are the source client's."""
    await db[OUTCOMES].update_one({"_id": f"{card['_id']}:{line['id']}"}, {"$setOnInsert": {
        "agent_id": card.get("agent_id"), "card_id": card["_id"], "line_id": line["id"],
        "client_group_id": card.get("client_group_id"), "treatment_ids": line.get("treatment_ids") or [],
        "creative_key": line.get("creative_key"), "ad_id": ad_id, "ad_name": line.get("ad_name"),
        "baseline_cpl": card.get("current_cpl"), "currency": card.get("currency"), "live_since": live_since,
        "spend": None, "leads": None, "cpl": None, "result": None, "scored_at": None, "created_at": live_since}},
        upsert=True)


async def mark_add_undone(db, card: dict, line: dict, now) -> None:
    """An Add someone reversed says nothing about the ad. It stays listed, as neither won nor lost, and
    having a result already means it is never scored."""
    await db[OUTCOMES].update_one({"_id": f"{card['_id']}:{line['id']}"}, {"$set": {"result": UNDONE, "scored_at": now}})
