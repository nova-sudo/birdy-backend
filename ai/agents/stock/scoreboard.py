"""
ai/agents/stock/scoreboard.py
-----------------------------
The stock agent's scoreboard store (spec section 9): one `agent_ad_outcomes` row per applied or
flagged Add, scored later. `record_for` is the read side the candidate ranking uses.
"""

import time
from datetime import datetime, time as dtime, timedelta, timezone

OUTCOMES = "agent_ad_outcomes"
WON, LOST, UNDONE = "won", "lost", "undone"
NO_CLEAR_CHANGE, NEVER_WENT_LIVE = "no_clear_change", "never_went_live"
FLAGGED_LOOKBACK = timedelta(days=30)   # how long after a flagged Add a person may still create the ad


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


async def record_add(db, card: dict, line: dict, ad_id: str, live_since, *, flagged: bool = False) -> None:
    """The row for an Add that went live, waiting to be scored. One per line however often it's called.
    The baseline and currency are the destination's: the line's own spend and CPL are the source client's.
    `flagged` marks an Add a person made by hand after a Flag card."""
    await db[OUTCOMES].update_one({"_id": f"{card['_id']}:{line['id']}"}, {"$setOnInsert": {
        "agent_id": card.get("agent_id"), "card_id": card["_id"], "line_id": line["id"],
        "client_group_id": card.get("client_group_id"), "treatment_ids": line.get("treatment_ids") or [],
        "creative_key": line.get("creative_key"), "ad_id": ad_id, "ad_name": line.get("ad_name"),
        "baseline_cpl": card.get("current_cpl"), "currency": card.get("currency"), "live_since": live_since,
        "spend": None, "leads": None, "cpl": None, "result": None, "scored_at": None, "created_at": live_since,
        **({"flagged": True} if flagged else {})}},
        upsert=True)


async def mark_add_undone(db, card: dict, line: dict, now) -> None:
    """An Add someone reversed says nothing about the ad. It stays listed, as neither won nor lost, and
    having a result already means it is never scored."""
    await db[OUTCOMES].update_one({"_id": f"{card['_id']}:{line['id']}"}, {"$set": {"result": UNDONE, "scored_at": now}})


def _naive(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(timezone.utc).replace(tzinfo=None)


async def record_flagged_adds(db, agent: dict, now_utc: datetime, *, deadline: float | None = None) -> int:
    """Nobody records a flagged Add when it is flagged: people create the ad. So each catch-up looks, for the
    flagged Add lines of the last 30 days (a card that a newer one has since superseded included, a dismissed
    one not), for an ad in the card's client with the line's normalised name that was created after the card
    and has spent. That ad gets a row, live from its first day of spend, so it is scored like any Add. An ad
    already on the scoreboard isn't used twice. Returns how many rows it recorded."""
    # Imported here: ai.agents.store imports this module for OUTCOMES, and candidates imports it for record_for.
    from ai.agents.guardrails import agent_tz, local_today
    from ai.agents.snapshot import load_ad_days, load_state
    from ai.agents.stock.candidates import normalise_name
    from ai.agents.store import CARDS

    now = _naive(now_utc)
    cards = await db[CARDS].find({
        "agent_id": agent["_id"], "preview": {"$ne": True}, "created_at": {"$gte": now - FLAGGED_LOOKBACK},
        "status": {"$in": ["flagged", "open", "partly_applied", "applied", "superseded"]},
        "lines": {"$elemMatch": {"kind": "add", "state": "flagged"}}}).sort("created_at", 1).to_list(length=None)
    pending = []
    for card in cards:
        for line in card.get("lines") or []:
            if line.get("kind") == "add" and line.get("state") == "flagged" and normalise_name(line.get("ad_name")):
                pending.append((card, line))
    if not pending:
        return 0
    have = {r["_id"] async for r in db[OUTCOMES].find({"_id": {"$in": [f"{c['_id']}:{ln['id']}" for c, ln in pending]}}, {"_id": 1})}
    used = {r.get("ad_id") async for r in db[OUTCOMES].find({"agent_id": agent["_id"]}, {"ad_id": 1})}
    today = local_today(now_utc, agent_tz(agent))
    recorded, loaded = 0, {}
    for card, line in pending:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if f"{card['_id']}:{line['id']}" in have:
            continue
        gid = card.get("client_group_id")
        if gid not in loaded:
            states, _ = await load_state(db, gid)
            loaded[gid] = (states, await load_ad_days(db, agent.get("user_id"), gid, today - FLAGGED_LOOKBACK - timedelta(days=1), today))
        states, days = loaded[gid]
        want, made = normalise_name(line.get("ad_name")), _naive(card["created_at"])
        first = {}
        for ad in states.values():
            if ad.ad_id in used or normalise_name(ad.name) != want:
                continue
            if ad.created_time is not None and _naive(ad.created_time) < made - timedelta(days=1):
                continue
            spent = [d.day for d in days.get(ad.ad_id, []) if d.spend > 0 and d.day >= made.date()]
            if spent:
                first[ad.ad_id] = min(spent)
        if not first:
            continue
        ad_id = min(first, key=lambda a: (first[a], a))
        await record_add(db, card, line, ad_id, datetime.combine(first[ad_id], dtime.min), flagged=True)
        used.add(ad_id)
        recorded += 1
    return recorded
