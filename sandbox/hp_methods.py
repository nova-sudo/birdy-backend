"""
sandbox/hp_methods.py
---------------------
The Hot Prospector operations (the body's ``Method``), answered from the world. Each handler gets
``(cred, body, world)`` and returns HP's envelope; a bad request answers HP's error shape
``{"response": "false", "message": ...}`` with HTTP 200, like HP.

Called by the product (integrations/hotprospector.py):
    getMemberUsers            team members (all call centres)
    SearchByUserInput         leads of one GHL location: locationId, searchField/searchText, limit (<= 100), offset
    FetchUserCallLog          call log: locationId?, from_date/to_date (<= 30 days; none = last 7 days),
                              type, limit (<= 500), offset, sort_order
    FetchLeadCallLogs         one lead's calls (list-wrapped envelope, as HP answers it)
    getMemberDashboardData    per-agent metrics of one day (``date``, default today)
Not called by the product today, there for completeness (names follow HP's style; HP's own
names for these are not confirmed):
    getGroups / getLists      the call-centre groups (a "list" per group)
    getLeadDetails            one lead with its calls
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from sandbox import hp_rows
from sandbox.hp_app import method

SEARCH_MAX_LIMIT = 100
SEARCH_DEFAULT_LIMIT = 50
CALL_LOG_MAX_LIMIT = 500
CALL_LOG_DEFAULT_LIMIT = 100
CALL_LOG_MAX_RANGE_DAYS = 30
CALL_LOG_DEFAULT_DAYS = 7


def fail(message: str) -> dict:
    return {"response": "false", "message": message}


def _int(body: dict, name: str, default: int, lo: int, hi: int):
    raw = body.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return min(max(value, lo), hi)


def _date(raw):
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


@method("getMemberUsers")
async def get_member_users(cred, body: dict, world) -> dict:
    return {"response": "true", "data": [hp_rows.member_row(world, m) for m in world.members()]}


@method("SearchByUserInput")
async def search_by_user_input(cred, body: dict, world) -> dict:
    limit = _int(body, "limit", SEARCH_DEFAULT_LIMIT, 1, SEARCH_MAX_LIMIT)
    offset = _int(body, "offset", 0, 0, 10 ** 9)
    if limit is None or offset is None:
        return fail("limit and offset must be numbers")
    rows = [hp_rows.lead_row(world, lead)
            for c in hp_rows.call_centres(world, body.get("locationId"))
            for lead in hp_rows.leads(world, c)]
    rows = [r for r in rows if hp_rows.search_matches(r, body.get("searchField"), body.get("searchText"))]
    total = len(rows)
    return {"response": "true", "Results": rows[offset:offset + limit], "total_count": total,
            "total_pages": max(1, math.ceil(total / limit)), "limit": limit, "offset": offset,
            "max_limit": SEARCH_MAX_LIMIT}


@method("FetchUserCallLog")
async def fetch_user_call_log(cred, body: dict, world) -> dict:
    limit = _int(body, "limit", CALL_LOG_DEFAULT_LIMIT, 1, CALL_LOG_MAX_LIMIT)
    offset = _int(body, "offset", 0, 0, 10 ** 9)
    if limit is None or offset is None:
        return fail("limit and offset must be numbers")
    since, until = body.get("from_date"), body.get("to_date")
    until = _date(until) if until else world.today
    since = _date(since) if since else (until - timedelta(days=CALL_LOG_DEFAULT_DAYS - 1) if until else None)
    if since is None or until is None:
        return fail("from_date and to_date must be dates (Y-m-d)")
    if since > until:
        return fail("from_date must be on or before to_date")
    if (until - since).days >= CALL_LOG_MAX_RANGE_DAYS + 1:
        return fail(f"The date range cannot exceed {CALL_LOG_MAX_RANGE_DAYS} days")

    in_range = hp_rows.calls(world, hp_rows.call_centres(world, body.get("locationId")), since, until)
    kind = str(body.get("type") or "").strip().lower()
    selected = [x for x in in_range if not kind or x.direction == kind]
    if str(body.get("sort_order") or "DESC").upper() != "ASC":
        selected.reverse()
    page = selected[offset:offset + limit]
    has_more = offset + len(page) < len(selected)
    return {
        "response": "true",
        "Results": [hp_rows.call_row(world, x) for x in page],
        "total_records": len(selected),
        "inbound_count": sum(1 for x in in_range if x.direction == "inbound"),
        "outbound_count": sum(1 for x in in_range if x.direction == "outbound"),
        "limit": limit, "offset": offset,
        "has_more": has_more, "next_offset": offset + len(page) if has_more else None,
        "from_date": since.isoformat(), "to_date": until.isoformat(),
    }


def _lead_of(world, body: dict):
    lead = world.hp_lead(str(body.get("LeadId") or body.get("leadId") or "").strip())
    if lead is None or not world.clients[lead.client_index].call_centre:
        return None
    return lead


@method("FetchLeadCallLogs")
async def fetch_lead_call_logs(cred, body: dict, world):
    lead = _lead_of(world, body)
    if lead is None:
        return [fail("Lead not found")]
    return [{"response": "true",
             "Results": [hp_rows.call_row(world, x, for_lead=True) for x in world.calls_for(lead)]}]


@method("getMemberDashboardData")
async def get_member_dashboard_data(cred, body: dict, world) -> dict:
    day = _date(body["date"]) if body.get("date") else world.today
    if day is None:
        return fail("date must be a date (Y-m-d)")
    # A future day, or one before the account's history, has no calls: every agent at zero.
    return {"response": "true", "date": day.isoformat(), "Results": hp_rows.member_day(world, day)}


def _group(world, c) -> dict:
    return {"GroupId": c.hp_group_id, "GroupName": hp_rows.group_name(c), "locationId": c.ghl_location_id,
            "members": len(c.members), "leads": len(hp_rows.leads(world, c))}


@method("getGroups")
@method("GetGroups")
async def get_groups(cred, body: dict, world) -> dict:
    return {"response": "true", "Results": [_group(world, c) for c in hp_rows.call_centres(world)]}


@method("getLists")
@method("GetLists")
async def get_lists(cred, body: dict, world) -> dict:
    return {"response": "true", "Results": [
        {"ListId": c.hp_group_id, "ListName": f"{c.name} - Facebook Leads", "GroupId": c.hp_group_id,
         "locationId": c.ghl_location_id, "leads": len(hp_rows.leads(world, c))}
        for c in hp_rows.call_centres(world)]}


@method("getLeadDetails")
@method("GetLeadDetails")
async def get_lead_details(cred, body: dict, world) -> dict:
    lead = _lead_of(world, body)
    if lead is None:
        return fail("Lead not found")
    return {"response": "true", "Results": [{
        **hp_rows.lead_row(world, lead),
        "call_logs": [hp_rows.call_row(world, x, for_lead=True) for x in world.calls_for(lead)]}]}
