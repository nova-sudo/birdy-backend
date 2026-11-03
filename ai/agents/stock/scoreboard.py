"""
ai/agents/stock/scoreboard.py
-----------------------------
The stock agent's scoreboard store (spec section 9): one `agent_ad_outcomes` row per applied or
flagged Add, scored later. `record_for` is the read side the candidate ranking uses.
"""

OUTCOMES = "agent_ad_outcomes"
WON, LOST = "won", "lost"


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
