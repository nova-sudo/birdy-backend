"""
sandbox/hp_rows.py
------------------
The world's people and calls in Hot Prospector's own shapes (pure, no I/O). sandbox/hp_methods.py
answers the operations with these.

What a Hot Prospector account looks like for a sandbox account:

- One HP group per call-centre client (``client.hp_group_id``; 5 of the 8 clients). The other
  clients have no call centre: their location has no HP leads and no calls.
- An HP lead per Meta lead of a call-centre client (the same person as the GHL contact: same name,
  email, phone), id ``world.hp_lead_id(lead.id)``. Listed oldest first, so paging by offset stays
  stable while new leads arrive.
- A call log row per ``world.calls`` call. A call nobody picked up ("No Answer") reports 0 seconds,
  so the product's "answered" (duration > 0) means "connected"; a conversation is 45 s or more.
- No recordings: every row has an empty ``recording`` / ``recordingId``, so nothing ever downloads
  fake audio or sends it to a transcription service.
- ``member_day`` is the per-agent dashboard of one day, counted from that day's call log rows, so
  the Members tab and the call log always agree.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

from sandbox.world import TZ, Call, Client, Lead, Member, World

NO_ANSWER = "No Answer"
CONVERSATION_SECONDS = 45


# ---------------------------------------------------------------------------
# Leads (SearchByUserInput)
# ---------------------------------------------------------------------------
def call_centres(world: World, location_id: Optional[str] = None) -> list[Client]:
    """The call-centre clients in scope: one location's (empty when it has no call centre), or all."""
    if location_id:
        c = world.client(str(location_id))
        return [c] if c is not None and c.call_centre and c.ghl_location_id == str(location_id) else []
    return [c for c in world.clients if c.call_centre]


def leads(world: World, client: Client) -> list[Lead]:
    """Every HP lead of a call-centre client, oldest first."""
    return world.leads(client, world.history_start, world.today) if client.call_centre else []


def lead_row(world: World, lead: Lead) -> dict:
    """A SearchByUserInput ``Results`` row."""
    c = world.clients[lead.client_index]
    created = lead.created_time.astimezone(TZ)
    return {
        "LeadId": world.hp_lead_id(lead.id),
        "Firstname": lead.first_name,
        "Lastname": lead.last_name,
        "E-Mail": lead.email,
        "Phone": lead.phone,
        "Mobile": "",
        "CountryCode": "GB",
        "Zipcode": "",
        "City": c.city,
        "State": "",
        "Address": "",
        "Company": "",
        "Website": "",
        "GroupId": c.hp_group_id,
        "GroupName": group_name(c),
        "locationId": c.ghl_location_id,
        "Tags": f"facebook lead, {c.vertical}",
        "Lead_Custom_Fields": {**lead.answers, "lead_source": "Facebook Lead Form",
                               "facebook_lead_id": lead.id, "ghl_contact_id": world.contact_id(lead.id)},
        "DateAdded": created.strftime("%Y-%m-%d %H:%M:%S"),
    }


def search_matches(row: dict, field: str, text: str) -> bool:
    """SearchByUserInput's ``searchField`` / ``searchText`` filter (substring, case-insensitive)."""
    text = (text or "").strip().lower()
    if not text:
        return True
    keys = {"email": ("E-Mail",), "e-mail": ("E-Mail",), "phone": ("Phone", "Mobile"),
            "firstname": ("Firstname",), "lastname": ("Lastname",), "name": ("Firstname", "Lastname"),
            "leadid": ("LeadId",)}.get((field or "").strip().lower(),
                                       ("Firstname", "Lastname", "E-Mail", "Phone", "LeadId"))
    return any(text in str(row.get(k) or "").lower() for k in keys)


def group_name(client: Client) -> str:
    return f"{client.name} Call Centre"


# ---------------------------------------------------------------------------
# Calls (FetchUserCallLog, FetchLeadCallLogs)
# ---------------------------------------------------------------------------
def talk_seconds(call: Call) -> int:
    """What HP reports as the call's duration: 0 when nobody picked up."""
    return 0 if call.disposition == NO_ANSWER else int(call.duration)


def display_time(at: datetime) -> str:
    """HP's call_time: the account's local time (London), e.g. "Apr 15, 2026 6:34 pm"."""
    t = at.astimezone(TZ)
    hour = t.hour % 12 or 12
    return f"{t:%b} {t.day}, {t.year} {hour}:{t:%M} {'am' if t.hour < 12 else 'pm'}"


def transferred(call: Call) -> bool:
    """A booked call is handed to a closer about a third of the time (deterministic)."""
    return call.disposition == "Booked Appointment" and int(call.id) % 3 == 0


def call_row(world: World, call: Call, *, for_lead: bool = False) -> dict:
    """A FetchUserCallLog ``Results`` row (``for_lead``: a FetchLeadCallLogs row, whose
    ``call_status`` is the direction, as the product reads it)."""
    lead = world.lead(call.lead_id)
    c = world.clients[call.client_index]
    seconds = talk_seconds(call)
    first = world.calls_for(lead)[0] if lead is not None else None
    speed = int((call.started_at - lead.created_time).total_seconds()) \
        if lead is not None and first is not None and first.id == call.id and call.direction == "outbound" else 0
    return {
        "call_id": call.id,
        "leadId": call.hp_lead_id,
        "lead_Name": lead.full_name if lead is not None else "",
        "location_name": c.name,
        "locationId": c.ghl_location_id,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "recording": "",            # sandbox calls have no recording: nothing to download or transcribe
        "recordingId": "",
        "group": group_name(c),
        "groupId": call.group_id,
        "caller_name": call.member_name,
        "member_id": call.member_id,
        "call_time": display_time(call.started_at),
        "duration": str(seconds),
        "city": c.city,
        "state": "",
        "transfer": "Yes" if transferred(call) else "No",
        "call_type": call.direction,
        "call_status": call.direction if for_lead else ("completed" if seconds > 0 else "no-answer"),
        "disposition": call.disposition,
        "speed_to_lead": str(max(speed, 0)),
    }


def calls(world: World, clients: Iterable[Client], since: date, until: date) -> list[Call]:
    """Calls of the clients that started on a London day in [since, until], oldest first."""
    out = [x for c in clients for x in world.calls(c, since, until)]
    return sorted(out, key=lambda x: (x.started_at, x.id))


# ---------------------------------------------------------------------------
# Member dashboard (getMemberDashboardData)
# ---------------------------------------------------------------------------
def _pct(part: float, whole: float) -> str:
    return f"{round(part / whole * 100) if whole else 0} %"


def member_day(world: World, day: date) -> list[dict]:
    """Every member's dashboard for one London day, from that day's calls (members without calls
    get zeros). HP's numbers come back as strings."""
    by_member: dict[str, list[Call]] = {}
    for x in calls(world, call_centres(world), day, day):
        by_member.setdefault(x.member_id, []).append(x)
    return [_member_metrics(m, by_member.get(m.id, [])) for m in world.members()]


def _member_metrics(m: Member, day_calls: list[Call]) -> dict:
    outbound = sum(1 for x in day_calls if x.direction == "outbound")
    inbound = len(day_calls) - outbound
    answered = sum(1 for x in day_calls if talk_seconds(x) > 0)
    convos = sum(1 for x in day_calls if talk_seconds(x) >= CONVERSATION_SECONDS)
    seconds = sum(talk_seconds(x) for x in day_calls)
    return {
        "agentId": m.id,
        "agentName": m.name,
        "agentEmail": m.email,
        "groupId": m.group_id,
        "outboundCall": str(outbound),
        "inboundCall": str(inbound),
        "answered_calls": str(answered),
        "hangups": str(answered - convos),
        "convos": str(convos),
        "Prospects": str(len({x.lead_id for x in day_calls})),
        "Appts": str(sum(1 for x in day_calls if x.disposition == "Booked Appointment")),
        "SMS": "0",
        "talkMin": f"{seconds / 60:.6f}",
        "answer_rate": _pct(answered, outbound + inbound),
        "cr": _pct(convos, answered),
    }


# ---------------------------------------------------------------------------
# Members (getMemberUsers)
# ---------------------------------------------------------------------------
def member_row(world: World, m: Member) -> dict:
    """A getMemberUsers row: the fields Sales-Hub's Members tab reads (first_name, last_name, email,
    mobile, member_status, memberId) plus the camelCase ones phase 1 returned."""
    c = world.clients[m.client_index]
    return {
        "memberId": m.id, "name": m.name,
        "first_name": m.first_name, "last_name": m.last_name,
        "firstName": m.first_name, "lastName": m.last_name,
        "email": m.email, "phone": m.phone, "mobile": m.phone, "direct_number": m.phone,
        "role": m.role, "status": "active", "member_status": "Active",
        "groupId": m.group_id, "group_name": group_name(c),
    }
