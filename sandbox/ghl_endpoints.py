"""
sandbox/ghl_endpoints.py
------------------------
The LeadConnector endpoints Birdy calls, answered from the GHL view of the world (sandbox/ghl_data.py).
Registered on ``sandbox.ghl_app.router`` (imported at the bottom of ghl_app, before the app is built).

Credentials, as GHL enforces them:
- agency (Company) token ``ghl``: /locations/search, /oauth/installedLocations, /locations,
  /locations/{id}, /users/. On a location-scoped endpoint it is a 401.
- location token ``ghl_location:<id>``: everything inside that one location. A request for another
  location (by ``locationId`` / ``location_id``, or by a contact, opportunity, conversation or message
  that lives elsewhere) is a 403.
- every endpoint except the OAuth ones wants a ``Version`` header (400 without one).

Writes (create contact, update contact / opportunity, tags, notes, tasks, send message) succeed in
GHL's shape and are remembered in sandbox.state, so later reads reflect them. Nothing is sent.
Calls never have a recording: the recording and transcription endpoints answer 404.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from sandbox import state
from sandbox.fakes import ProviderError, request_fields, require_credential
from sandbox.ghl_app import router
from sandbox.ghl_data import GhlView, iso, new_id, parse_when
from sandbox.ghl_search import cursor_page, query_match, search_contacts, unprocessable

UTC = timezone.utc
AUTH_CLASS_MESSAGE = "This authClass type is not allowed to access this scope. Please reach out to the Platform team to get it added."
NO_ACCESS_MESSAGE = "The token does not have access to this location."
OPP_STATUSES = ("open", "won", "lost", "abandoned")


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def _require_version(request: Request) -> None:
    if not request.headers.get("version"):
        raise ProviderError(400, "Version header was not found")


async def _agency(request: Request):
    """(cred, view) for an agency-token endpoint."""
    _require_version(request)
    cred = await require_credential(request, "ghl", "ghl_location")
    if cred.base_kind != "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    return cred, await GhlView.load(cred)


async def _company(request: Request, fields: dict):
    cred, view = await _agency(request)
    company = fields.get("companyId")
    if company and company != view.agency.ghl_company_id:
        raise ProviderError(400, "companyId does not match this token")
    return cred, view


async def _location(request: Request, location_id: Optional[str] = None, *, agency_ok: bool = False):
    """(cred, view, client) for a location-scoped endpoint. ``location_id`` None: the token's own."""
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl" and not agency_ok:
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    if cred.base_kind == "ghl_location":
        if location_id is None:
            location_id = cred.arg
        elif location_id != cred.arg:
            raise ProviderError(403, NO_ACCESS_MESSAGE)
    if not location_id:
        raise unprocessable("locationId should not be empty")
    view = await GhlView.load(cred)
    client = view.client_for(location_id)
    if client is None:
        raise ProviderError(400, f"Location with id {location_id} not found")
    return cred, view, client


def _own(cred, location_id: Optional[str]) -> None:
    """A thing found by its own id must live in the token's location."""
    if cred.base_kind == "ghl_location" and location_id != cred.arg:
        raise ProviderError(403, NO_ACCESS_MESSAGE)


async def _contact(request: Request, contact_id: str):
    """(cred, view, contact json) for a contact the location token may see."""
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    view = await GhlView.load(cred)
    contact, _client = view.contact(contact_id)
    if contact is None:
        raise ProviderError(400, f"Contact with id {contact_id} not found")
    _own(cred, contact["locationId"])
    return cred, view, contact


def _int(fields: dict, name: str, default: int, *, lo: int = 0, hi: Optional[int] = None) -> int:
    raw = fields.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise unprocessable(f"{name} must be a number")
    if value < lo or (hi is not None and value > hi):
        raise unprocessable(f"{name} must be between {lo} and {hi}" if hi is not None else f"{name} must be >= {lo}")
    return value


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Locations (agency token)
# ---------------------------------------------------------------------------
def _skip_page(rows: list, fields: dict, default_limit: int, max_limit: int) -> list:
    skip = _int(fields, "skip", 0)
    limit = _int(fields, "limit", default_limit, lo=1, hi=max_limit)
    return rows[skip: skip + limit]


@router.get("/locations/search")
async def locations_search(request: Request):
    fields = await request_fields(request)
    _cred, view = await _company(request, fields)
    rows = [view.location_json(c) for c in view.w.clients]
    if fields.get("email"):
        rows = [r for r in rows if r["email"].lower() == str(fields["email"]).lower()]
    rows.sort(key=lambda r: r["dateAdded"], reverse=str(fields.get("order") or "asc").lower() == "desc")
    return {"locations": _skip_page(rows, fields, 10, 1000)}


@router.get("/oauth/installedLocations")
async def installed_locations(request: Request):
    fields = await request_fields(request)
    _cred, view = await _company(request, fields)
    installed = str(fields.get("isInstalled", "true")).lower() != "false"
    rows = [view.installed_location_json(c) for c in view.w.clients] if installed else []
    q = str(fields.get("query") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["name"].lower()]
    page = _skip_page(rows, fields, 20, 1000)
    return {"locations": page, "count": len(rows), "installToFutureLocations": True}


@router.get("/locations")
@router.get("/locations/")
async def locations_legacy(request: Request):
    """The pre-search listing (fallback only): capped at 100."""
    fields = await request_fields(request)
    _cred, view = await _company(request, fields)
    rows = [view.location_json(c) for c in view.w.clients]
    return {"locations": _skip_page(rows, fields, 100, 100)}


@router.get("/locations/{location_id}")
async def location_get(request: Request, location_id: str):
    _cred, view, client = await _location(request, location_id, agency_ok=True)
    return {"location": view.location_json(client)}


@router.get("/locations/{location_id}/customFields")
async def location_custom_fields(request: Request, location_id: str):
    _cred, view, client = await _location(request, location_id)
    return {"customFields": view.custom_fields(client)}


@router.get("/locations/{location_id}/tags")
async def location_tags(request: Request, location_id: str):
    _cred, view, client = await _location(request, location_id)
    return {"tags": view.location_tags(client)}


@router.get("/users")
@router.get("/users/")
async def users_list(request: Request):
    fields = await request_fields(request)
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    location_id = fields.get("locationId")
    if cred.base_kind == "ghl_location":
        _own(cred, location_id or cred.arg)
    view = await GhlView.load(cred)
    if location_id and view.client_for(location_id) is None:
        raise ProviderError(400, f"Location with id {location_id} not found")
    return {"users": [view.user_json()]}


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------
@router.get("/contacts")
@router.get("/contacts/")
async def contacts_list(request: Request):
    """The legacy list: ``limit`` (max 100), newest first, paged by ``startAfter`` (ms) and
    ``startAfterId``; ``meta.total`` is the location's contact count."""
    fields = await request_fields(request)
    _cred, view, client = await _location(request, fields.get("locationId"))
    limit = _int(fields, "limit", 20, lo=1, hi=100)
    rows = view.contacts(client)
    q = str(fields.get("query") or "").strip()
    if q:
        rows = [r for r in rows if query_match(r, q)]
    start_after = fields.get("startAfter")
    chunk, next_after, next_id = cursor_page(
        rows, limit=limit, start_after=int(start_after) if str(start_after or "").isdigit() else None,
        start_after_id=fields.get("startAfterId") or None)
    base = "https://services.leadconnectorhq.com/contacts/"
    return {
        "contacts": chunk,
        "meta": {
            "total": len(rows),
            "startAfter": next_after,
            "startAfterId": next_id,
            "nextPageUrl": (f"{base}?locationId={client.ghl_location_id}&limit={limit}"
                            f"&startAfter={next_after}&startAfterId={next_id}") if next_id else None,
            "currentPage": None, "nextPage": None, "prevPage": None,
        },
    }


@router.post("/contacts/search")
async def contacts_search(request: Request):
    body = await request_fields(request)
    _cred, view, client = await _location(request, body.get("locationId") or None)
    if not body.get("locationId"):
        raise unprocessable("locationId should not be empty")
    result = search_contacts(view.contacts(client), body)
    return {**result, "traceId": f"sbx-{new_id('t', 24)}"}


@router.post("/contacts")
@router.post("/contacts/")
async def contacts_create(request: Request):
    body = await request_fields(request)
    cred, view, client = await _location(request, body.get("locationId") or None)
    if not body.get("locationId"):
        raise unprocessable("locationId should not be empty")
    email = str(body.get("email") or "").strip().lower()
    phone = "".join(ch for ch in str(body.get("phone") or "") if ch.isdigit() or ch == "+")
    for existing in view.contacts(client):
        field = ("email" if email and (existing.get("email") or "").lower() == email else
                 "phone" if phone and existing.get("phone") == phone else None)
        if field:
            raise ProviderError(400, "This location does not allow duplicated contacts.",
                                meta={"contactName": f"{existing.get('firstName') or ''} {existing.get('lastName') or ''}".strip(),
                                      "contactId": existing["id"], "matchingField": field})
    allowed = ("firstName", "lastName", "name", "companyName", "address1", "city", "state", "postalCode",
               "country", "website", "timezone", "dateOfBirth", "source", "tags", "assignedTo", "dnd",
               "customFields")
    fields = {k: body[k] for k in allowed if k in body}
    if "name" in fields and not (fields.get("firstName") or fields.get("lastName")):
        first, _, last = str(fields.pop("name")).partition(" ")
        fields["firstName"], fields["lastName"] = first, last
    fields.pop("name", None)
    now = _now()
    contact_id = new_id("n")
    fields.update(locationId=client.ghl_location_id, email=email or None, phone=phone or None, dateAdded=iso(now))
    await state.set(cred.user_id, "ghl_contact_new", contact_id, fields, at=now)
    view.new_contacts[contact_id] = dict(fields, id=contact_id)
    view.invalidate()
    contact, _c = view.contact(contact_id)
    return JSONResponse({"contact": contact}, status_code=201)


@router.get("/contacts/{contact_id}")
async def contact_get(request: Request, contact_id: str):
    _cred, _view, contact = await _contact(request, contact_id)
    return {"contact": contact}


async def _patch_contact(cred, view, contact: dict, patch: dict) -> dict:
    now = _now()
    current, _at = view.contact_patch.get(contact["id"], ({}, None))
    merged = {**current, **patch}
    await state.set(cred.user_id, "ghl_contact", contact["id"], merged, at=now)
    view.contact_patch[contact["id"]] = (merged, now)
    view.invalidate()
    fresh, _c = view.contact(contact["id"])
    return fresh


@router.put("/contacts/{contact_id}")
async def contact_update(request: Request, contact_id: str):
    cred, view, contact = await _contact(request, contact_id)
    body = await request_fields(request)
    editable = ("firstName", "lastName", "email", "phone", "companyName", "address1", "city", "state",
                "postalCode", "country", "website", "timezone", "dateOfBirth", "source", "tags", "assignedTo",
                "dnd", "customFields")
    patch = {k: body[k] for k in editable if k in body}
    if "email" in patch:
        patch["email"] = str(patch["email"] or "").strip().lower() or None
    fresh = await _patch_contact(cred, view, contact, patch)
    return {"succeded": True, "contact": fresh}


def _tag_list(body: dict) -> list:
    tags = body.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise unprocessable("tags must be an array of strings")
    return [t.strip().lower() for t in tags if t.strip()]


@router.post("/contacts/{contact_id}/tags")
async def contact_add_tags(request: Request, contact_id: str):
    cred, view, contact = await _contact(request, contact_id)
    add = _tag_list(await request_fields(request))
    tags = list(contact.get("tags") or [])
    tags += [t for t in add if t not in tags]
    fresh = await _patch_contact(cred, view, contact, {"tags": tags})
    return JSONResponse({"tags": fresh["tags"]}, status_code=201)


@router.delete("/contacts/{contact_id}/tags")
async def contact_remove_tags(request: Request, contact_id: str):
    cred, view, contact = await _contact(request, contact_id)
    remove = set(_tag_list(await request_fields(request)))
    tags = [t for t in contact.get("tags") or [] if t not in remove]
    fresh = await _patch_contact(cred, view, contact, {"tags": tags})
    return {"tags": fresh["tags"]}


@router.get("/contacts/{contact_id}/notes")
async def contact_notes(request: Request, contact_id: str):
    _cred, view, contact = await _contact(request, contact_id)
    return {"notes": view.contact_notes(contact)}


@router.post("/contacts/{contact_id}/notes")
async def contact_add_note(request: Request, contact_id: str):
    cred, view, contact = await _contact(request, contact_id)
    body = await request_fields(request)
    text = str(body.get("body") or "").strip()
    if not text:
        raise unprocessable("body should not be empty")
    now = _now()
    note_id = new_id("N")
    note = {"contactId": contact["id"], "body": text, "userId": body.get("userId") or view.agency.ghl_user_id,
            "dateAdded": iso(now)}
    await state.set(cred.user_id, "ghl_note", note_id, note, at=now)
    return JSONResponse({"note": {"id": note_id, **note}}, status_code=201)


@router.get("/contacts/{contact_id}/tasks")
async def contact_tasks(request: Request, contact_id: str):
    _cred, view, contact = await _contact(request, contact_id)
    return {"tasks": view.contact_tasks(contact)}


@router.post("/contacts/{contact_id}/tasks")
async def contact_add_task(request: Request, contact_id: str):
    cred, view, contact = await _contact(request, contact_id)
    body = await request_fields(request)
    title = str(body.get("title") or "").strip()
    if not title:
        raise unprocessable("title should not be empty")
    due = parse_when(body.get("dueDate"))
    if body.get("dueDate") and due is None:
        raise unprocessable("dueDate must be a valid ISO 8601 date string")
    now = _now()
    task_id = new_id("K")
    task = {"contactId": contact["id"], "title": title, "body": str(body.get("body") or ""),
            "assignedTo": body.get("assignedTo") or view.agency.ghl_user_id, "dueDate": iso(due) if due else None,
            "completed": bool(body.get("completed", False)), "dateAdded": iso(now)}
    await state.set(cred.user_id, "ghl_task", task_id, task, at=now)
    return JSONResponse({"task": {"id": task_id, **task}}, status_code=201)


@router.get("/contacts/{contact_id}/appointments")
async def contact_appointments(request: Request, contact_id: str):
    _cred, view, contact = await _contact(request, contact_id)
    return {"events": view.appointments(contact)}


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------
def _mdy(value) -> Optional[str]:
    """GHL's ``date`` / ``endDate`` filters are mm-dd-yyyy; returns yyyy-mm-dd."""
    if not value:
        return None
    s = str(value)
    try:
        if len(s) == 10 and s[2] == "-" and s[5] == "-":
            return f"{s[6:10]}-{s[0:2]}-{s[3:5]}"
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        raise unprocessable("date must be in mm-dd-yyyy format")


@router.get("/opportunities/search")
async def opportunities_search(request: Request):
    f = await request_fields(request)
    location_id = f.get("location_id") or f.get("locationId")
    if not location_id:
        raise unprocessable("location_id should not be empty")
    _cred, view, client = await _location(request, location_id)
    status = str(f.get("status") or "all").lower()
    if status != "all" and status not in OPP_STATUSES:
        raise unprocessable(f"status must be one of the following values: {', '.join(OPP_STATUSES + ('all',))}")
    limit = _int(f, "limit", 20, lo=1, hi=100)
    page = _int(f, "page", 1, lo=1)
    rows = view.opportunities(client)
    if status != "all":
        rows = [o for o in rows if o["status"] == status]
    for param, key in (("contact_id", "contactId"), ("pipeline_id", "pipelineId"),
                       ("pipeline_stage_id", "pipelineStageId"), ("assigned_to", "assignedTo"), ("id", "id")):
        value = f.get(param) or f.get(key)
        if value:
            rows = [o for o in rows if o.get(key) == value]
    q = str(f.get("q") or "").strip().lower()
    if q:
        rows = [o for o in rows if q in (o["name"] or "").lower() or q in (o["contact"].get("email") or "").lower()]
    since, until = _mdy(f.get("date")), _mdy(f.get("endDate"))
    if since:
        rows = [o for o in rows if o["createdAt"][:10] >= since]
    if until:
        rows = [o for o in rows if o["createdAt"][:10] <= until]
    order = str(f.get("order") or "added_desc").lower()
    if order == "added_asc":
        rows = list(reversed(rows))
    total = len(rows)
    start_after_id = f.get("startAfterId")
    if start_after_id:
        chunk, next_after, next_id = cursor_page(
            rows, limit=limit, start_after_id=start_after_id, time_field="createdAt",
            start_after=int(f["startAfter"]) if str(f.get("startAfter") or "").isdigit() else None)
        current = None
        next_page = None
    else:
        chunk = rows[(page - 1) * limit: page * limit]
        more = page * limit < total
        last = chunk[-1] if chunk and more else None
        next_after = int(parse_when(last["createdAt"]).timestamp() * 1000) if last else None
        next_id = last["id"] if last else None
        current, next_page = page, (page + 1 if more else None)
    base = "https://services.leadconnectorhq.com/opportunities/search"
    return {
        "opportunities": chunk,
        "meta": {
            "total": total,
            "currentPage": current,
            "nextPage": next_page,
            "prevPage": (current - 1) if current and current > 1 else None,
            "startAfter": next_after,
            "startAfterId": next_id,
            "nextPageUrl": (f"{base}?location_id={client.ghl_location_id}&limit={limit}"
                            f"&startAfter={next_after}&startAfterId={next_id}") if next_id else None,
        },
        "aggregations": {"pipelines": {}},
    }


@router.get("/opportunities/pipelines")
async def opportunities_pipelines(request: Request):
    f = await request_fields(request)
    _cred, view, client = await _location(request, f.get("locationId") or f.get("location_id") or None)
    return {"pipelines": view.pipelines(client)}


async def _opportunity(request: Request, opportunity_id: str):
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    view = await GhlView.load(cred)
    opp, client = view.opportunity(opportunity_id)
    if opp is None:
        raise ProviderError(400, f"Opportunity with id {opportunity_id} not found")
    _own(cred, opp["locationId"])
    return cred, view, opp, client


async def _patch_opportunity(cred, view, opp: dict, client, patch: dict) -> dict:
    now = _now()
    current, _at = view.opp_patch.get(opp["id"], ({}, None))
    merged = {**current, **patch}
    await state.set(cred.user_id, "ghl_opportunity", opp["id"], merged, at=now)
    view.opp_patch[opp["id"]] = (merged, now)
    view.invalidate()
    fresh, _c = view.opportunity(opp["id"])
    return fresh


@router.get("/opportunities/{opportunity_id}")
async def opportunity_get(request: Request, opportunity_id: str):
    _cred, _view, opp, _client = await _opportunity(request, opportunity_id)
    return {"opportunity": opp}


def _opportunity_patch(body: dict, client) -> dict:
    patch = {}
    if "status" in body:
        status = str(body["status"] or "").lower()
        if status not in OPP_STATUSES:
            raise unprocessable(f"status must be one of the following values: {', '.join(OPP_STATUSES)}")
        patch["status"] = status
    if "pipelineId" in body and body["pipelineId"] != client.pipeline.id:
        raise ProviderError(400, f"Pipeline with id {body['pipelineId']} not found")
    if "pipelineStageId" in body:
        if body["pipelineStageId"] not in {s.id for s in client.pipeline.stages}:
            raise ProviderError(400, f"Stage with id {body['pipelineStageId']} not found in the pipeline")
        patch["pipelineStageId"] = body["pipelineStageId"]
    if "monetaryValue" in body:
        try:
            patch["monetaryValue"] = round(float(body["monetaryValue"] or 0), 2)
        except (TypeError, ValueError):
            raise unprocessable("monetaryValue must be a number")
    for k in ("name", "assignedTo", "source"):
        if k in body:
            patch[k] = body[k]
    return patch


@router.put("/opportunities/{opportunity_id}")
async def opportunity_update(request: Request, opportunity_id: str):
    cred, view, opp, client = await _opportunity(request, opportunity_id)
    patch = _opportunity_patch(await request_fields(request), client)
    return {"opportunity": await _patch_opportunity(cred, view, opp, client, patch)}


@router.put("/opportunities/{opportunity_id}/status")
async def opportunity_status(request: Request, opportunity_id: str):
    cred, view, opp, client = await _opportunity(request, opportunity_id)
    body = await request_fields(request)
    if "status" not in body:
        raise unprocessable("status should not be empty")
    await _patch_opportunity(cred, view, opp, client, _opportunity_patch({"status": body["status"]}, client))
    return {"succeded": True}


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
_CHANNEL_TYPES = {"sms": {"TYPE_SMS"}, "email": {"TYPE_EMAIL"}, "call": {"TYPE_CALL"}}


@router.get("/conversations/search")
async def conversations_search(request: Request):
    f = await request_fields(request)
    _cred, view, client = await _location(request, f.get("locationId") or None)
    if f.get("contactId"):
        contact, _c = view.contact(f["contactId"])
        contacts = [contact] if contact and contact["locationId"] == client.ghl_location_id else []
    else:
        contacts = view.contacts(client)
    limit = _int(f, "limit", 20, lo=1, hi=100)
    rows = [view.conversation_json(x) for x in contacts]
    rows = [r for r in rows if r["lastMessageDate"]]
    rows.sort(key=lambda r: (r["lastMessageDate"], r["id"]), reverse=True)
    start_after = f.get("startAfterDate")
    if str(start_after or "").isdigit():
        rows_after = [r for r in rows if r["lastMessageDate"] < int(start_after)]
    else:
        rows_after = rows
    return {"conversations": rows_after[:limit], "total": len(rows)}


@router.get("/conversations/messages/export")
async def messages_export(request: Request):
    """A contact's messages: ``channel`` absent = every non-email channel, ``Email`` = email only.
    Cursor paging: ``nextCursor`` until the last page."""
    f = await request_fields(request)
    location_id = f.get("locationId")
    if not location_id:
        raise unprocessable("locationId should not be empty")
    _cred, view, client = await _location(request, location_id)
    contact_id = f.get("contactId")
    contact = None
    if contact_id:
        contact, _c = view.contact(contact_id)
        if contact is None:
            raise ProviderError(400, f"Contact with id {contact_id} not found")
        if contact["locationId"] != client.ghl_location_id:
            raise ProviderError(403, NO_ACCESS_MESSAGE)
        rows = view.messages(contact)
    else:
        rows = [m for x in view.contacts(client) for m in view.messages(x)]
    channel = str(f.get("channel") or "").strip().lower()
    if channel:
        if channel not in _CHANNEL_TYPES:
            raise unprocessable("channel must be one of the following values: SMS, Email, Call")
        rows = [m for m in rows if m["messageType"] in _CHANNEL_TYPES[channel]]
    else:
        rows = [m for m in rows if m["messageType"] != "TYPE_EMAIL"]
    since, until = parse_when(f.get("startDate")), parse_when(f.get("endDate"))
    if since:
        rows = [m for m in rows if m["dateAdded"] >= iso(since)]
    if until:
        rows = [m for m in rows if m["dateAdded"] <= iso(until)]
    desc = str(f.get("sortOrder") or "desc").lower() == "desc"
    rows.sort(key=lambda m: (m["dateAdded"], m["id"]), reverse=desc)
    limit = _int(f, "limit", 100, lo=1, hi=500)
    cursor = str(f.get("cursor") or "")
    if cursor and not cursor.isdigit():
        raise ProviderError(400, "Invalid cursor")
    start = int(cursor or 0)
    chunk = rows[start: start + limit]
    more = start + limit < len(rows)
    return {"messages": chunk, "nextCursor": str(start + limit) if more else None, "total": len(rows)}


@router.get("/conversations/messages/{message_id}/locations/{location_id}/recording")
async def message_recording(request: Request, message_id: str, location_id: str):
    """Sandbox calls never have a recording (no fake audio ever reaches a transcription service)."""
    cred, view, _client = await _location(request, location_id)
    message, _contact = view.message(message_id)
    if message is None:
        raise ProviderError(404, "Message not found")
    _own(cred, message["locationId"])
    raise ProviderError(404, "Recording not found for this message")


@router.get("/conversations/locations/{location_id}/messages/{message_id}/transcription")
async def message_transcription(request: Request, message_id: str, location_id: str):
    cred, view, _client = await _location(request, location_id)
    message, _contact = view.message(message_id)
    if message is None:
        raise ProviderError(404, "Message not found")
    _own(cred, message["locationId"])
    raise ProviderError(404, "Transcription not found for this message")


@router.get("/conversations/messages/{message_id}")
async def message_get(request: Request, message_id: str):
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    view = await GhlView.load(cred)
    message, _contact = view.message(message_id)
    if message is None:
        raise ProviderError(404, "Message not found")
    _own(cred, message["locationId"])
    return message


@router.post("/conversations/messages")
async def message_send(request: Request):
    """Send an SMS or an email to a contact: remembered, shown in the conversation, never sent."""
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    body = await request_fields(request)
    kind = str(body.get("type") or "")
    if kind not in ("SMS", "Email"):
        raise unprocessable("type must be one of the following values: SMS, Email")
    contact_id = body.get("contactId")
    if not contact_id:
        raise unprocessable("contactId should not be empty")
    view = await GhlView.load(cred)
    contact, _client = view.contact(contact_id)
    if contact is None:
        raise ProviderError(400, f"Contact with id {contact_id} not found")
    _own(cred, contact["locationId"])
    text = str(body.get("message") or "")
    html = str(body.get("html") or "")
    if not (text.strip() or html.strip()):
        raise unprocessable("message should not be empty")
    if kind == "SMS" and not contact.get("phone"):
        raise ProviderError(400, "Contact does not have a phone number")
    if kind == "Email" and not contact.get("email"):
        raise ProviderError(400, "Contact does not have an email address")
    if contact.get("dnd"):
        raise ProviderError(400, "Contact has opted out of messages (DND is enabled)")
    now = _now()
    message_id = new_id("s", 22)
    mtype = "TYPE_SMS" if kind == "SMS" else "TYPE_EMAIL"
    message = {
        "type": 2 if kind == "SMS" else 3, "messageType": mtype,
        "locationId": contact["locationId"], "contactId": contact["id"],
        "conversationId": view.conversation_id(contact["id"]),
        "dateAdded": iso(now), "dateUpdated": iso(now),
        "body": html if kind == "Email" and html else text,
        "direction": "outbound", "status": "delivered",
        "contentType": "text/html" if kind == "Email" else "text/plain",
        "attachments": [a for a in body.get("attachments") or [] if isinstance(a, str)],
        "source": "api", "userId": view.agency.ghl_user_id,
        "meta": ({"email": {"subject": str(body.get("subject") or ""), "messageIds": [f"sbx-{message_id}"]}}
                 if kind == "Email" else {}),
    }
    await state.set(cred.user_id, "ghl_message", message_id, message, at=now)
    out = {"conversationId": message["conversationId"], "messageId": message_id,
           "msg": "Message queued successfully."}
    if kind == "Email":
        out["emailMessageId"] = f"sbx-{message_id}"
    return JSONResponse(out, status_code=201)


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(request: Request, conversation_id: str):
    """One conversation's messages, newest first, paged by ``lastMessageId``."""
    cred, view, contact = await _conversation(request, conversation_id)
    f = await request_fields(request)
    limit = _int(f, "limit", 20, lo=1, hi=100)
    rows = list(reversed(view.messages(contact)))
    last_id = f.get("lastMessageId")
    if last_id:
        idx = next((i for i, m in enumerate(rows) if m["id"] == last_id), None)
        rows_after = rows[idx + 1:] if idx is not None else []
    else:
        rows_after = rows
    chunk = rows_after[:limit]
    more = len(rows_after) > limit
    return {"messages": {"lastMessageId": chunk[-1]["id"] if chunk else None, "nextPage": more,
                         "messages": chunk}}


async def _conversation(request: Request, conversation_id: str):
    _require_version(request)
    cred = await require_credential(request, "ghl_location", "ghl")
    if cred.base_kind == "ghl":
        raise ProviderError(401, AUTH_CLASS_MESSAGE)
    view = await GhlView.load(cred)
    contact_id = view.contact_of_conversation(conversation_id)
    contact, _c = view.contact(contact_id) if contact_id else (None, None)
    if contact is None:
        raise ProviderError(400, f"Conversation with id {conversation_id} not found")
    _own(cred, contact["locationId"])
    return cred, view, contact


@router.get("/conversations/{conversation_id}")
async def conversation_get(request: Request, conversation_id: str):
    _cred, view, contact = await _conversation(request, conversation_id)
    return view.conversation_json(contact)
