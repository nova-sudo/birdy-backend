"""
ai/team/sub_agents.py
---------------------
The one place the chat reads sub-agent records from.

Sub-agent storage, CRUD and approval live in another branch (feat/sub-agents).
Until it merges, `_source` below is a stub that returns no records, so no
`ask_sub_agent` tool is ever offered. Merging is a one-line change: point
`_source` at the real lister, e.g.

    from ai.sub_agents.store import list_sub_agents as _source

Contract the real lister must meet
    async def list_sub_agents(db, parent: str, user_id: str) -> list[dict | object]
      parent   "media_buyer" | "sales_coach" (the owning main agent's key)
      user_id  the owner; only that user's records
    Each record (a dict or an object with these attributes):
      id            str, stable
      name          str, shown in the route ("Lead quality checker")
      instructions  str, its own written instructions
      tools         list[str], tool names its main agent picked for it
    Optional:
      client_group_ids  list[str], clients it covers; empty or missing = all
      status            "active" (default) | "paused" | anything else; only
                        active records are callable

Whatever the record says, a sub-agent never gets a tool its main agent does
not have, and never a write tool (delegation.py intersects the lists).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SubAgent:
    id: str
    name: str
    instructions: str
    tools: tuple = ()
    client_group_ids: tuple = ()
    status: str = "active"
    parent: str = ""

    @property
    def key(self) -> str:
        """Its agent key in a route step: "sub:<id>"."""
        return f"sub:{self.id}"


async def _source(db, parent: str, user_id: str) -> list:
    """Stub until feat/sub-agents merges: no records."""
    return []


def _get(rec, name, default=None):
    if isinstance(rec, dict):
        return rec.get(name, default)
    return getattr(rec, name, default)


def coerce(rec, parent: str) -> SubAgent | None:
    """A record from the source as a SubAgent, or None when it is unusable."""
    rid = _get(rec, "id") or _get(rec, "_id")
    name = _get(rec, "name")
    if not rid or not name:
        return None
    return SubAgent(
        id=str(rid), name=str(name),
        instructions=str(_get(rec, "instructions") or ""),
        tools=tuple(str(t) for t in (_get(rec, "tools") or ()) if t),
        client_group_ids=tuple(str(g) for g in (_get(rec, "client_group_ids") or ()) if g),
        status=str(_get(rec, "status") or "active"),
        parent=parent,
    )


async def list_sub_agents(db, parent: str, user_id: str) -> list[SubAgent]:
    """The owner's active sub-agents under one main agent. Never raises."""
    try:
        rows = await _source(db, parent, user_id) or []
    except Exception:
        import logging
        logging.getLogger(__name__).warning("sub-agent listing failed", exc_info=True)
        return []
    out = []
    for r in rows:
        sub = coerce(r, parent)
        if sub and sub.status == "active":
            out.append(sub)
    return out
