"""Shared test helper: seed the `agent_ad_history` doc the agent needs before it reads a client."""

from datetime import datetime

from ai.agents.history import HISTORY


async def seed_history(db, gid, status="ready", **fields):
    """One history doc for client group `gid`. Defaults to a finished sync."""
    doc = {"_id": gid, "status": status, "error": None, "oldest_done": None, "newest_done": None,
           "last_topup_at": datetime(2026, 10, 25) if status == "ready" else None}
    doc.update(fields)
    await db[HISTORY].replace_one({"_id": gid}, doc, upsert=True)
