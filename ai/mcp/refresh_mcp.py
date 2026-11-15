"""
ai/mcp/refresh_mcp.py
---------------------
Refresh one client's Meta / GoHighLevel data and check on it. user_id always comes from the token. There is no
refresh-everything tool: the global cycle is not per-user. The work lives in services/client_refresh.py.
"""

from typing import Literal

from fastmcp.exceptions import ToolError

from ai.mcp.access import READ, WRITE
from ai.mcp.client_mcp import resolve_client
from ai.mcp.server import mcp, current_user_id as _current_user_id
from core.database import get_db
from core.mongo_client import get_shared_mongo_client
from services import client_refresh

_NAMES = {"meta": "Meta", "ghl": "GoHighLevel"}


def _client(group: dict) -> dict:
    return {"id": group["id"], "name": group.get("name")}


@mcp.tool(**WRITE)
async def refresh_client_data(client: str, integration: Literal["meta", "ghl", "all"] = "all") -> dict:
    """Start a fresh pull of one client's ad data from Meta, its GoHighLevel leads and opportunities, or both.

    Use it when a client's numbers look stale (get_client shows when each last refreshed) or the user asks to
    refresh. It returns at once with `started` (the integrations it began) and `already_running` (ones it did
    not start a second time); the data itself arrives later. A refresh takes a few minutes: poll
    refresh_status until the status is "complete" before reading the new numbers.
    Asking for an integration the client has not connected is an error; with "all", unconnected ones are listed
    in `not_connected`. A name that matches several clients returns the candidates and nothing is started.

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
        integration: "meta", "ghl" or "all" (default): which data to refresh.
    """
    mongo_client = get_shared_mongo_client()
    db, user_id = get_db(mongo_client), _current_user_id()
    group = await resolve_client(db, user_id, client)

    wanted = list(client_refresh.INTEGRATIONS) if integration == "all" else [integration]
    connected = [i for i in wanted if client_refresh.is_connected(group, i)]
    if integration != "all" and not connected:
        raise ToolError(f"{_NAMES[integration]} is not connected for {group.get('name')}, so there is nothing to refresh.")
    if not connected:
        raise ToolError(f"{group.get('name')} has no connected integrations, so there is nothing to refresh.")

    started, already_running = [], []
    for name in connected:
        if await client_refresh.is_running(db, group, name):
            already_running.append(name)
        else:
            await client_refresh.queue_refresh(mongo_client, user_id, group, name)
            started.append(name)

    out = {"client": _client(group), "started": started, "already_running": already_running,
           "check_with": "refresh_status"}
    if integration == "all" and len(connected) < len(wanted):
        out["not_connected"] = [i for i in wanted if i not in connected]
    return out


@mcp.tool(**READ)
async def refresh_status(client: str) -> dict:
    """Where a client's data refresh stands: for Meta and for GoHighLevel, its status ("idle", "running", "complete" or "error"), the last error if any, and for Meta the job's progress (status, current step, presets done of total).

    Use it after refresh_client_data, polling every minute or so until the status is "complete" (a refresh takes a few minutes), or to see why a refresh failed. meta_token_error true means the Facebook connection needs renewing in Birdy.
    A name that matches several clients returns the candidates and nothing else.

    Args:
        client: The client's id (from get_client_groups) or its name (a unique part of it is enough).
    """
    mongo_client = get_shared_mongo_client()
    db, user_id = get_db(mongo_client), _current_user_id()
    group = await resolve_client(db, user_id, client)
    status = await client_refresh.get_refresh_status(mongo_client, group["id"], user_id)
    return {"client": _client(group), **(status or {})}
