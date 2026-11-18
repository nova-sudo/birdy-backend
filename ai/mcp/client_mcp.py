"""
ai/mcp/client_mcp.py
--------------------
One client's context: profile, targets, notes. A client is identified by its client group id, never its
name (names are denormalized copies across collections); a name is only a way to find the group.
user_id always comes from the token. Other tool modules import `resolve_client` from here.
"""

import math

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ, WRITE
from ai.mcp.server import mcp, current_user_id as _current_user_id
from ai.tools.agent_tools import _match_client
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import client_notes, client_targets

LATEST_NOTES = 10


async def resolve_client(db, user_id: str, client: str) -> dict:
    """The user's client group for `client` (its id, or a name: exact match wins, else a unique part of it).

    Searches only this user's own groups. Several matches raise a ToolError naming them, so nothing is
    ever done to a client picked silently."""
    groups = await db["client_groups"].find({"user_id": user_id}).to_list(length=None)
    needle = str(client or "").strip()
    for g in groups:
        if needle and g.get("id") == needle:
            return g
    group, err = _match_client(groups, client)
    if err:
        names = err.get("candidates") or []
        if err.get("ambiguous"):
            raise ToolError(f"Several clients match '{client}': {', '.join(names)}. Which one?")
        msg = err["error"] if not needle else f"No client matches '{client}'."
        if names:
            msg += f" Your clients: {', '.join(names)}."
        raise ToolError(msg)
    return group


def _db():
    return get_db(get_shared_mongo_client())


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _notes(db, user_id: str, group: dict, limit: int | None = None) -> list[dict]:
    rows = await client_notes.list_notes(db, user_id, group["id"], limit=limit or client_notes.MAX_NOTES_RETURNED)
    return [{k: v for k, v in r.items() if k not in ("user_id", "group_id")} for r in rows]


def hotprospector_connected(group: dict) -> bool:
    """HotProspector is linked through the client's GHL location and its call_log_provider;
    hotprospector_group_id is the old link and is empty on every client set up since."""
    if group.get("hotprospector_group_id"):
        return True
    return ((group.get("call_log_provider") or "").strip().lower() == "hotprospector"
            and bool(group.get("ghl_location_id")))


@mcp.tool(**READ)
async def get_client(client: str) -> dict:
    """One client's profile: id, name, targets (cost per lead and others), ad account currency, which integrations are connected (Meta, GoHighLevel, call center), when its Meta and GoHighLevel data last refreshed, and its latest 10 notes.

    Use it for "tell me about Acme", before changing a client's target, or to check whether a client's data is fresh.
    A name that matches several clients returns the candidates and nothing else.

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
    """
    db, user_id = _db(), _current_user_id()
    group = await resolve_client(db, user_id, client)
    return {
        "id": group["id"],
        "name": group.get("name"),
        "targets": {k: v for k, v in (group.get("targets") or {}).items() if k != "updated_at"},
        "ad_account_currency": group.get("ad_account_currency"),
        "integrations": {
            "meta": bool(group.get("meta_ad_account_id")),
            "ghl": bool(group.get("ghl_location_id")),
            "hotprospector": hotprospector_connected(group),
        },
        "last_refreshed": {"meta": _iso(group.get("last_meta_refresh")), "ghl": _iso(group.get("last_ghl_refresh"))},
        "notes": await _notes(db, user_id, group, LATEST_NOTES),
    }


@mcp.tool(**READ)
async def get_client_notes(client: str) -> dict:
    """All notes on one client, newest first. Each has an id, the author, the text (body) and when it was written.

    Use it for "what did we note about Acme" or to find the id of a note before deleting it.

    Args:
        client: The client's id or its name (a unique part of it is enough).
    """
    db, user_id = _db(), _current_user_id()
    group = await resolve_client(db, user_id, client)
    return {"client": group.get("name"), "notes": await _notes(db, user_id, group)}


@mcp.tool(**WRITE)
async def add_client_note(client: str, text: str) -> dict:
    """Add a note to a client, for something the data cannot show: a call, a change of plan, why a month looked odd. Returns the saved note.

    Use it when the owner asks you to remember or record something about a client. A name that matches several
    clients returns the candidates and saves nothing.

    Args:
        client: The client's id or its name (a unique part of it is enough).
        text: The note, up to 2000 characters.
    """
    db, user_id = _db(), _current_user_id()
    group = await resolve_client(db, user_id, client)
    try:
        return await client_notes.create_note(db, user_id, group["id"], text, source="mcp")
    except client_notes.NoteError as e:
        raise ToolError(e.detail)


@mcp.tool(**WRITE)
async def delete_client_note(client: str, note_id: str) -> dict:
    """Delete one note from a client. Get the note id from get_client_notes. Only a note on that client can be deleted.

    Args:
        client: The client's id or its name (a unique part of it is enough).
        note_id: The note's id.
    """
    db, user_id = _db(), _current_user_id()
    group = await resolve_client(db, user_id, client)
    try:
        await client_notes.delete_note(db, user_id, group["id"], note_id)
    except client_notes.NoteError as e:
        raise ToolError(e.detail)
    return {"success": True, "id": note_id, "client": group.get("name")}


def _amount(value, label: str):
    """Same rule the targets route applies (any stored number, zero included), minus what can never be a goal."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ToolError(f"{label} must be a number, zero or more")
    return value


@mcp.tool(**WRITE)
async def set_client_target(client: str, cpl: float | None = None, cpa: float | None = None,
                            monthly_wins: float | None = None, monthly_revenue: float | None = None,
                            conversion_rate: float | None = None, monthly_spend: float | None = None,
                            aov: float | None = None) -> dict:
    """Set one or more of a client's targets, the goals the dashboard and agents measure the client against. Each must be zero or more. A target you leave out is left as it is.

    Money values are in the client's own ad account currency. Use it when the owner says what a client's goal should be.
    A name that matches several clients returns the candidates and changes nothing.

    Args:
        client: The client's id or its name (a unique part of it is enough).
        cpl: Cost per lead: spend divided by leads.
        cpa: Cost per acquisition: spend divided by clients actually won. Not the same number as cpl.
        monthly_wins: Clients won (closes) per month. Drives the client's health band.
        monthly_revenue: Revenue per month.
        conversion_rate: Lead to win conversion rate.
        monthly_spend: Ad spend per month.
        aov: Average order value.
    """
    given = dict(cpl=cpl, cpa=cpa, monthly_wins=monthly_wins, monthly_revenue=monthly_revenue,
                 conversion_rate=conversion_rate, monthly_spend=monthly_spend, aov=aov)
    sent = {k: _amount(v, k) for k, v in given.items() if v is not None}
    if not sent:
        raise ToolError("Give at least one target: " + ", ".join(client_targets.TARGET_FIELDS) + ".")
    db, user_id = _db(), _current_user_id()
    group = await resolve_client(db, user_id, client)
    before = {k: (group.get("targets") or {}).get(k) for k in sent}
    targets = await client_targets.apply_targets(db, user_id, group["id"], sent)
    if targets is None:
        raise ToolError("Client not found")
    targets = client_targets.clean(targets)
    return {"id": group["id"], "client": group.get("name"), "before": before, "targets": targets,
            "summary": f"Updated {group.get('name')}'s targets: " + ", ".join(f"{k} {v}" for k, v in sent.items()) + "."}
