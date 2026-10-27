"""Shared test helpers: seed the `agent_ad_history` doc the agent needs before it reads a
client, and the daily ad rows (`agent_ad_days`) the agent reads."""

from datetime import datetime

from ai.agents.history import HISTORY
from ai.agents.store import AD_DAYS


async def seed_history(db, gid, status="ready", **fields):
    """One history doc for client group `gid`. Defaults to a finished sync."""
    doc = {"_id": gid, "status": status, "error": None, "oldest_done": None, "newest_done": None,
           "last_topup_at": datetime(2026, 10, 25) if status == "ready" else None}
    doc.update(fields)
    await db[HISTORY].replace_one({"_id": gid}, doc, upsert=True)


async def seed_ad_days(db, rows):
    """Insert daily ad rows where the agent reads them."""
    rows = list(rows)
    if rows:
        await db[AD_DAYS].insert_many(rows)


def ad_days(db):
    """The agent's daily ad row collection, for direct reads/updates in tests."""
    return db[AD_DAYS]
