"""
sandbox/ghl_data.py
-------------------
The world as GoHighLevel sees it: locations, contacts, opportunities, pipelines, conversations,
appointments, notes, custom fields and users, in LeadConnector's JSON shapes, with the GHL writes a
sandbox account made (sandbox/state.py, kinds ``ghl_*``) applied on top.

Everything is derived from sandbox.world: a contact is a Meta lead (same name, email, phone, ad ids),
its opportunity is World.opportunity_for(lead), its stage history drives the conversation, the
appointment and the notes. What the world does not have (messages, appointments, notes, custom
fields) is generated here from the world's deterministic primitives, so asking twice gives the same
answer and nothing is ever dated after ``now``.

    view = await GhlView.load(cred)                  # one per request
    client = view.client_for(location_id)
    view.contacts(client)                            # newest first, GHL contact JSON
    view.opportunities(client)                       # GHL opportunity JSON
    view.messages(contact_json)                      # oldest first

Remembered writes (``state`` kinds):
    ghl_contact_new   key contact id     the contact JSON fields given on POST /contacts/
    ghl_contact       key contact id     field patch (PUT /contacts/{id}, tag add / remove)
    ghl_opportunity   key opportunity id field patch (PUT /opportunities/{id}, /status)
    ghl_message       key message id     a message sent through POST /conversations/messages
    ghl_note          key note id        POST /contacts/{id}/notes
    ghl_task          key task id        POST /contacts/{id}/tasks
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sandbox.world import TZ, Client, Lead, _between, _ghl_id, _h, _pick, _u, world_for

UTC = timezone.utc
GHL_KINDS = ("ghl_contact_new", "ghl_contact", "ghl_opportunity", "ghl_message", "ghl_note", "ghl_task")
MESSAGE_TYPES = {"TYPE_CALL": 1, "TYPE_SMS": 2, "TYPE_EMAIL": 3}
_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# A location's contact and opportunity lists cost ~0.1 s to derive; a sync pages through them
# (opportunities 100 at a time), so they are kept for LIST_TTL seconds per (user, location, the
# account's remembered writes). Any write changes the key; at worst a list is LIST_TTL seconds
# behind the clock (a lead that arrived in the meantime shows on the next read).
LIST_TTL = 30.0
_LIST_CACHE_SIZE = 64
_lists: "OrderedDict[tuple, tuple]" = OrderedDict()
_lists_lock = threading.Lock()


def clear_cache() -> None:
    with _lists_lock:
        _lists.clear()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def iso(dt: Optional[datetime]) -> Optional[str]:
    """GHL's timestamp format: ``2026-11-20T10:15:30.123Z`` (UTC, milliseconds)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_when(value) -> Optional[datetime]:
    """An aware datetime from an ISO string, an epoch in ms (number or digit string), or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()):
        return datetime.fromtimestamp(int(value) / 1000.0, tz=UTC)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def ms(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.timestamp() * 1000) if dt else None


def new_id(prefix: str, length: int = 20) -> str:
    """A fresh GHL-style id for something created by a write (never collides with world ids,
    whose first letters are c / o / v / a / t)."""
    return prefix + "".join(secrets.choice(_ALNUM) for _ in range(length - len(prefix)))


def _street(address: str) -> str:
    return (address or "").split(", ")[0]


def _offer(world, lead: Lead) -> str:
    camp = world.find(lead.campaign_id)
    return camp.name.split(" | ")[0] if camp else "your enquiry"


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------
class GhlView:
    """The account's GoHighLevel data as of the world's ``now``. Build one per request with
    ``await GhlView.load(cred)`` (or ``GhlView(world, writes)`` in tests)."""

    def __init__(self, world, writes: list, fingerprint=None):
        self.w = world
        self.fingerprint = fingerprint
        self.now = world.now
        self.user_id = world.user_id
        self.agency = world.agency
        self.new_contacts: dict = {}      # id -> fields
        self.contact_patch: dict = {}     # id -> (patch, at)
        self.opp_patch: dict = {}         # id -> (patch, at)
        self.sent: list = []              # message dicts
        self.notes: list = []
        self.tasks: list = []
        for d in writes:
            kind, key, value, at = d.get("kind"), d.get("key"), d.get("value"), d.get("at")
            if at is not None and at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if at is not None and at > self.now:
                continue
            if kind == "ghl_contact_new":
                self.new_contacts[key] = dict(value, id=key)
            elif kind == "ghl_contact":
                self.contact_patch[key] = (value or {}, at)
            elif kind == "ghl_opportunity":
                self.opp_patch[key] = (value or {}, at)
            elif kind == "ghl_message":
                self.sent.append(dict(value, id=key))
            elif kind == "ghl_note":
                self.notes.append(dict(value, id=key))
            elif kind == "ghl_task":
                self.tasks.append(dict(value, id=key))
        self._contacts: dict = {}         # location id -> [contact json]
        self._opps: dict = {}             # location id -> [opp json]

    def invalidate(self) -> None:
        """After a write through this view: rebuild the lists (and skip the shared cache)."""
        self.fingerprint = None
        self._contacts.clear()
        self._opps.clear()

    @classmethod
    async def load(cls, cred) -> "GhlView":
        from sandbox import state

        docs = await state.list(cred.user_id)
        overrides = await state.overrides(cred.user_id) if any(
            d.get("kind") in (state.STATUS, state.BUDGET, state.NAME) for d in docs) else None
        world = world_for(cred.user_id, overrides=overrides)
        fingerprint = (len(docs), max((d["at"] for d in docs), default=None))
        return cls(world, [d for d in docs if d.get("kind") in GHL_KINDS], fingerprint)

    # ----- locations ----------------------------------------------------
    def client_for(self, location_id) -> Optional[Client]:
        c = self.w.client(str(location_id or ""))
        return c if c is not None and c.ghl_location_id == location_id else None

    def location_json(self, c: Client) -> dict:
        """GET /locations/{id} ``location`` (also the rows of /locations/search)."""
        return {
            "id": c.ghl_location_id,
            "companyId": self.agency.ghl_company_id,
            "name": c.name,
            "domain": "",
            "address": _street(c.address),
            "city": c.city,
            "state": "",
            "country": "GB",
            "postalCode": c.postcode,
            "website": c.website,
            "timezone": c.timezone,
            "email": c.email,
            "phone": c.phone,
            "firstName": "Sandbox",
            "lastName": "Owner",
            "logoUrl": "",
            "business": {
                "name": c.name, "address": _street(c.address), "city": c.city, "state": "", "country": "GB",
                "postalCode": c.postcode, "website": c.website, "timezone": c.timezone, "email": c.email,
                "logoUrl": "",
            },
            "social": {},
            "settings": {"allowDuplicateContact": False, "allowDuplicateOpportunity": False,
                         "allowFacebookNameMerge": False, "disableContactTimezone": False},
            "reseller": {},
            "dateAdded": iso(datetime(2025, 5, 20, 9, 0, tzinfo=UTC) + timedelta(days=c.index)),
            "isInstalled": True,
        }

    def installed_location_json(self, c: Client) -> dict:
        return {"_id": c.ghl_location_id, "name": c.name, "address": _street(c.address), "isInstalled": True}

    # ----- users, custom fields, pipelines -----------------------------
    def user_json(self) -> dict:
        return {
            "id": self.agency.ghl_user_id, "name": "Sandbox Tester", "firstName": "Sandbox",
            "lastName": "Tester", "email": self.user_id, "phone": "", "extension": "",
            "permissions": {"contactsEnabled": True, "opportunitiesEnabled": True, "conversationsEnabled": True},
            "roles": {"type": "agency", "role": "admin",
                      "locationIds": [c.ghl_location_id for c in self.w.clients]},
            "deleted": False,
        }

    def custom_fields(self, c: Client) -> list:
        from sandbox.world import _QUESTIONS

        out = []
        for i, (question, options) in enumerate(_QUESTIONS[c.vertical]):
            name = question.replace("_", " ").rstrip("?").capitalize() + "?"
            out.append({
                "id": _ghl_id(self.w.seed, "custom-field", c.index, question),
                "name": name,
                "fieldKey": "contact." + question.rstrip("?"),
                "placeholder": "",
                "dataType": "SINGLE_OPTIONS",
                "picklistOptions": list(options),
                "position": i * 50,
                "model": "contact",
                "locationId": c.ghl_location_id,
                "documentType": "field",
                "parentId": None,
                "dateAdded": iso(datetime(2025, 5, 21, 9, 0, tzinfo=UTC)),
            })
        return out

    def pipelines(self, c: Client) -> list:
        p = c.pipeline
        return [{
            "id": p.id, "name": p.name, "locationId": c.ghl_location_id,
            "stages": [{"id": s.id, "name": s.name, "position": s.position, "showInFunnel": True,
                        "showInPieChart": True, "originId": s.id} for s in p.stages],
            "showInFunnel": True, "showInPieChart": True, "originId": p.id,
            "dateAdded": iso(datetime(2025, 5, 21, 9, 30, tzinfo=UTC)),
            "dateUpdated": iso(datetime(2025, 5, 21, 9, 30, tzinfo=UTC)),
        }]

    # ----- contacts -----------------------------------------------------
    def contacts(self, c: Client) -> list:
        """Every contact in the location, newest ``dateAdded`` first (GHL's default order)."""
        loc = c.ghl_location_id
        if loc in self._contacts:
            return self._contacts[loc]
        key = (self.user_id, loc, self.w.today, self.fingerprint) if self.fingerprint is not None else None
        if key is not None:
            with _lists_lock:
                hit = _lists.get(key)
            if hit is not None and time.monotonic() - hit[0] < LIST_TTL:
                self._contacts[loc], self._opps[loc] = hit[1], hit[2]
                return hit[1]
        out, opps = [], []
        w = self.w
        for lead in w.leads(c, w.history_start, w.today):
            contact = w.contact_for(lead)
            if contact.date_added > self.now:
                continue        # the lead exists in Meta, GHL has not created the contact yet
            opp = w.opportunity_for(lead)
            opp_json = self._opportunity_json(c, lead, contact, opp) if opp else None
            out.append(self._contact_json(c, lead, contact, opp_json))
            if opp_json:
                opps.append(opp_json)
        for fields in self.new_contacts.values():
            if fields.get("locationId") == loc:
                out.append(self._new_contact_json(fields))
        out.sort(key=lambda x: (x["dateAdded"], x["id"]), reverse=True)
        for opp in opps:        # the opportunity's contact block carries the contact's latest tags
            contact = next((x for x in out if x["id"] == opp["contactId"]), None)
            if contact:
                opp["contact"]["tags"] = list(contact["tags"])
        opps.sort(key=lambda x: (x["createdAt"], x["id"]), reverse=True)
        self._contacts[loc], self._opps[loc] = out, opps
        if key is not None:
            with _lists_lock:
                _lists[key] = (time.monotonic(), out, opps)
                _lists.move_to_end(key)
                while len(_lists) > _LIST_CACHE_SIZE:
                    _lists.popitem(last=False)
        return out

    def contact(self, contact_id: str) -> tuple:
        """(contact json, client) for any contact of the account, or (None, None)."""
        contact_id = str(contact_id or "")
        if contact_id in self.new_contacts:
            fields = self.new_contacts[contact_id]
            return self._new_contact_json(fields), self.client_for(fields.get("locationId"))
        base = self.w.contact(contact_id)
        if base is None or base.date_added > self.now:
            return None, None
        c = self.w.clients[base.client_index]
        found = next((x for x in self.contacts(c) if x["id"] == contact_id), None)
        return (found, c) if found else (None, None)

    def lead_of(self, contact_json: dict) -> Optional[Lead]:
        base = self.w.contact(contact_json.get("id", ""))
        return self.w.lead(base.lead_id) if base else None

    def _patched(self, contact_id: str, out: dict) -> dict:
        patch, at = self.contact_patch.get(contact_id, ({}, None))
        for k, v in patch.items():
            out[k] = list(v) if isinstance(v, (list, tuple)) else v
        if at is not None and iso(at) > out["dateUpdated"]:
            out["dateUpdated"] = iso(at)
        for name in ("firstName", "lastName", "email"):
            out[f"{name}LowerCase" if name != "email" else "emailLowerCase"] = (out.get(name) or "").lower() or None
        out["fullNameLowerCase"] = f"{out.get('firstName') or ''} {out.get('lastName') or ''}".strip().lower()
        return out

    def _contact_json(self, c: Client, lead: Lead, contact, opp_json: Optional[dict]) -> dict:
        w = self.w
        ad, adset, camp = w.find(lead.ad_id), w.find(lead.adset_id), w.find(lead.campaign_id)
        fields = {f["fieldKey"].split(".", 1)[1] + "?": f["id"] for f in self.custom_fields(c)}
        attribution = {
            "sessionSource": "Paid Social",
            "medium": "facebook",
            "mediumId": lead.form_id,
            "utmSource": "facebook",
            "utmMedium": adset.name if adset else None,
            "utmContent": ad.name if ad else None,
            "utmCampaign": camp.name if camp else None,
            "campaign": camp.name if camp else None,
            "campaignId": lead.campaign_id,
            "adSetId": lead.adset_id,
            "adId": lead.ad_id,
            "adSource": "instagram" if lead.platform == "ig" else "facebook",
            "formId": lead.form_id,
            "url": "https://www.instagram.com/" if lead.platform == "ig" else "https://www.facebook.com/",
        }
        out = {
            "id": contact.id,
            "locationId": c.ghl_location_id,
            "firstName": contact.first_name,
            "lastName": contact.last_name,
            "email": contact.email,
            "phone": contact.phone,
            "companyName": None,
            "address1": None,
            "city": None,
            "state": None,
            "postalCode": None,
            "country": "GB",
            "timezone": c.timezone,
            "website": None,
            "dateOfBirth": None,
            "type": "lead",
            "source": contact.source,
            "dnd": False,
            "dndSettings": {},
            "assignedTo": None,
            "businessId": None,
            "followers": [],
            "additionalEmails": [],
            "additionalPhones": [],
            "validEmail": True,
            "tags": list(contact.tags),
            "customFields": [{"id": fields[q], "value": a} for q, a in lead.answers.items() if q in fields],
            "attributionSource": attribution,
            "lastAttributionSource": dict(attribution),
            "dateAdded": iso(contact.date_added),
            "dateUpdated": iso(contact.date_updated),
            "opportunities": ([{
                "id": opp_json["id"], "pipelineId": opp_json["pipelineId"],
                "pipelineStageId": opp_json["pipelineStageId"], "monetaryValue": opp_json["monetaryValue"],
                "status": opp_json["status"],
            }] if opp_json else []),
        }
        if opp_json and opp_json["updatedAt"] > out["dateUpdated"]:
            out["dateUpdated"] = opp_json["updatedAt"]
        return self._patched(contact.id, out)

    def _new_contact_json(self, fields: dict) -> dict:
        c = self.client_for(fields.get("locationId"))
        out = {
            "id": fields["id"], "locationId": fields.get("locationId"),
            "firstName": fields.get("firstName"), "lastName": fields.get("lastName"),
            "email": (fields.get("email") or None), "phone": fields.get("phone") or None,
            "companyName": fields.get("companyName"), "address1": fields.get("address1"),
            "city": fields.get("city"), "state": fields.get("state"), "postalCode": fields.get("postalCode"),
            "country": fields.get("country") or "GB", "timezone": fields.get("timezone") or (c.timezone if c else None),
            "website": fields.get("website"), "dateOfBirth": fields.get("dateOfBirth"),
            "type": "lead", "source": fields.get("source"), "dnd": bool(fields.get("dnd", False)),
            "dndSettings": {}, "assignedTo": fields.get("assignedTo"), "businessId": None, "followers": [],
            "additionalEmails": [], "additionalPhones": [], "validEmail": bool(fields.get("email")),
            "tags": list(fields.get("tags") or []),
            "customFields": list(fields.get("customFields") or []),
            "attributionSource": {}, "lastAttributionSource": {},
            "dateAdded": fields["dateAdded"], "dateUpdated": fields["dateAdded"],
            "opportunities": [],
        }
        return self._patched(fields["id"], out)

    # ----- opportunities -----------------------------------------------
    def opportunities(self, c: Client) -> list:
        """Every opportunity in the location, newest ``createdAt`` first."""
        self.contacts(c)
        return self._opps[c.ghl_location_id]

    def opportunity(self, opportunity_id: str) -> tuple:
        base = self.w.opportunity(str(opportunity_id or ""))
        if base is None:
            return None, None
        c = self.w.clients[base.client_index]
        found = next((o for o in self.opportunities(c) if o["id"] == opportunity_id), None)
        return (found, c) if found else (None, None)

    def _opportunity_json(self, c: Client, lead: Lead, contact, opp) -> dict:
        created = max(opp.created_at, contact.date_added)
        out = {
            "id": opp.id,
            "name": opp.name,
            "monetaryValue": opp.monetary_value,
            "pipelineId": opp.pipeline_id,
            "pipelineStageId": opp.stage.id,
            "pipelineStageUId": opp.stage.id,
            "assignedTo": self.agency.ghl_user_id,
            "status": opp.status,
            "source": contact.source,
            "lastStatusChangeAt": iso(max(opp.last_status_change_at, created)),
            "lastStageChangeAt": iso(max(opp.last_stage_change_at, created)),
            "lastActionDate": iso(max(opp.updated_at, created)),
            "indexVersion": len(opp.history),
            "createdAt": iso(created),
            "updatedAt": iso(max(opp.updated_at, created)),
            "contactId": contact.id,
            "locationId": c.ghl_location_id,
            "contact": {"id": contact.id, "name": contact.full_name, "companyName": None,
                        "email": contact.email, "phone": contact.phone, "tags": list(contact.tags)},
            "notes": [], "tasks": [], "calendarEvents": [], "customFields": [], "followers": [],
        }
        patch, at = self.opp_patch.get(opp.id, ({}, None))
        if patch:
            stamp = iso(at) if at else out["updatedAt"]
            if "status" in patch and patch["status"] != out["status"]:
                out["status"] = patch["status"]
                out["lastStatusChangeAt"] = stamp
            if "pipelineStageId" in patch and patch["pipelineStageId"] != out["pipelineStageId"]:
                out["pipelineStageId"] = out["pipelineStageUId"] = patch["pipelineStageId"]
                out["lastStageChangeAt"] = stamp
            for k in ("monetaryValue", "name", "assignedTo", "pipelineId", "source"):
                if k in patch:
                    out[k] = patch[k]
            out["updatedAt"] = out["lastActionDate"] = max(out["updatedAt"], stamp)
        return out

    # ----- conversations ------------------------------------------------
    @staticmethod
    def conversation_id(contact_id: str) -> str:
        return "v" + contact_id

    @staticmethod
    def contact_of_conversation(conversation_id: str) -> Optional[str]:
        s = str(conversation_id or "")
        return s[1:] if s.startswith("v") and len(s) > 1 else None

    def messages(self, contact: dict) -> list:
        """Every message in the contact's conversation, oldest first: the generated history plus
        what was sent through the fake."""
        out = []
        lead = self.lead_of(contact)
        if lead is not None:
            out.extend(self._generated_messages(contact, lead))
        for m in self.sent:
            if m.get("contactId") == contact["id"]:
                out.append(dict(m))
        out = [m for m in out if m["dateAdded"] <= iso(self.now)]
        out.sort(key=lambda m: (m["dateAdded"], m["id"]))
        return out

    def message(self, message_id: str) -> tuple:
        """(message, contact json) by id, or (None, None)."""
        message_id = str(message_id or "")
        sent = next((m for m in self.sent if m["id"] == message_id), None)
        contact_id = sent["contactId"] if sent else (message_id[:20] if len(message_id) == 23 and message_id[20] == "M" else None)
        if not contact_id:
            return None, None
        contact, _c = self.contact(contact_id)
        if contact is None:
            return None, None
        found = next((m for m in self.messages(contact) if m["id"] == message_id), None)
        return (found, contact) if found else (None, None)

    def _message(self, contact: dict, k: int, mtype: str, direction: str, at: datetime, body: str,
                 status: str = "delivered", source: str = "workflow", meta: Optional[dict] = None) -> dict:
        return {
            "id": f"{contact['id']}M{k:02d}",
            "type": MESSAGE_TYPES.get(mtype, 0),
            "messageType": mtype,
            "locationId": contact["locationId"],
            "contactId": contact["id"],
            "conversationId": self.conversation_id(contact["id"]),
            "dateAdded": iso(at),
            "dateUpdated": iso(at),
            "body": body,
            "direction": direction,
            "status": status,
            "contentType": "text/html" if mtype == "TYPE_EMAIL" else "text/plain",
            "attachments": [],
            "source": source,
            "userId": self.agency.ghl_user_id if direction == "outbound" and source != "workflow" else None,
            "meta": meta or {},
        }

    def _generated_messages(self, contact: dict, lead: Lead) -> list:
        w, c = self.w, self.w.clients[lead.client_index]
        seed = _h(w.seed, "ghl-messages", lead.id)
        added = parse_when(contact["dateAdded"])
        first, offer, business = lead.first_name, _offer(w, lead), c.name
        out = [self._message(contact, 0, "TYPE_SMS", "outbound", added + timedelta(seconds=40 + seed % 80),
                             f"Hi {first}, thanks for your enquiry about {offer} at {business}! "
                             "One of the team will call you shortly. Reply STOP to opt out.")]
        if _u(seed, 1) < 0.75:
            out.append(self._message(
                contact, 1, "TYPE_EMAIL", "outbound", added + timedelta(minutes=4, seconds=seed % 50),
                f"<p>Hi {first},</p><p>Thanks for getting in touch about <b>{offer}</b>. "
                f"We'll be in touch today to find a time that suits you.</p><p>The {business} team</p>",
                meta={"email": {"subject": f"Your {offer} enquiry", "messageIds": [f"sbx-{lead.id}-1"]}}))
        if _u(seed, 2) < 0.35:
            reply = _pick(("Hi, yes please! When are you free?", "Thanks, what times do you have this week?",
                           "Can you tell me the price first?", "Great, mornings are best for me",
                           "Is there parking nearby?"), seed, 3)
            out.append(self._message(contact, 2, "TYPE_SMS", "inbound", added + timedelta(hours=_between(0.3, 20, seed, 4)),
                                     reply, source="app"))
        steps = dict()
        for stage, at in w._journey(lead):
            steps.setdefault(stage, at)
        if not c.call_centre:
            # Clients without a call centre ring their leads from GoHighLevel: the opportunity
            # moves to Contacted when the call connects. Calls never carry a recording.
            contacted = steps.get("Contacted")
            if contacted is not None:
                if _u(seed, 5) < 0.4:
                    missed = contacted - timedelta(hours=_between(2, 20, seed, 6))
                    if missed > added:
                        out.append(self._call(contact, 3, missed, 0, "no-answer"))
                duration = int(_between(70, 720, seed, 7))
                out.append(self._call(contact, 4, contacted + timedelta(seconds=duration), duration, "completed"))
            elif _u(seed, 8) < 0.7:
                out.append(self._call(contact, 3, added + timedelta(hours=_between(1, 30, seed, 9)), 0, "no-answer"))
        booked = steps.get("Booked")
        appt = self.appointments(contact)
        if booked is not None and appt:
            start = parse_when(appt[0]["dateAdded"])
            local = appt[0]["startTime"]
            out.append(self._message(contact, 5, "TYPE_SMS", "outbound", start + timedelta(minutes=1),
                                     f"Hi {first}, you're booked in at {business} on {local[:10]} at {local[11:16]}. "
                                     "Reply C to confirm or R to reschedule."))
        return out

    def _call(self, contact: dict, k: int, at: datetime, duration: int, status: str) -> dict:
        return self._message(contact, k, "TYPE_CALL", "outbound", at, "", status=status, source="app",
                             meta={"call": {"duration": duration, "status": status}})

    def conversation_json(self, contact: dict) -> dict:
        msgs = self.messages(contact)
        last = msgs[-1] if msgs else None
        name = f"{contact.get('firstName') or ''} {contact.get('lastName') or ''}".strip()
        return {
            "id": self.conversation_id(contact["id"]),
            "locationId": contact["locationId"],
            "contactId": contact["id"],
            "type": "TYPE_PHONE",
            "fullName": name, "contactName": name,
            "email": contact.get("email"), "phone": contact.get("phone"),
            "tags": contact.get("tags") or [],
            "unreadCount": 1 if last and last["direction"] == "inbound" else 0,
            "inbox": True, "starred": False, "deleted": False,
            "lastMessageBody": (last or {}).get("body", "")[:200],
            "lastMessageType": (last or {}).get("messageType"),
            "lastMessageDirection": (last or {}).get("direction"),
            "lastMessageDate": ms(parse_when(last["dateAdded"])) if last else None,
            "dateAdded": ms(parse_when(contact["dateAdded"])),
            "dateUpdated": ms(parse_when(last["dateAdded"])) if last else ms(parse_when(contact["dateAdded"])),
        }

    # ----- appointments, notes, tasks ----------------------------------
    def appointments(self, contact: dict) -> list:
        """The contact's appointment (leads that reached Booked). ``startTime`` is the clock time at
        the business ("YYYY-MM-DD HH:MM:SS", no offset) as GHL sends it; a booking still open may
        start after now (it is scheduled), everything else is <= now."""
        lead = self.lead_of(contact)
        if lead is None:
            return []
        w, c = self.w, self.w.clients[lead.client_index]
        steps = w._journey(lead)
        booked = next((at for s, at in steps if s == "Booked"), None)
        if booked is None or booked > self.now:
            return []
        close = next(((s, at) for s, at in steps if s in ("Won", "Lost") and at > booked), None)
        seed = _h(w.seed, "ghl-appointment", lead.id)
        if close:
            start = booked + (close[1] - booked) * _between(0.25, 0.7, seed, 1)
        else:
            start = booked + timedelta(days=_between(1, 5, seed, 1))
        local = start.astimezone(TZ).replace(hour=9 + int(_u(seed, 2) * 8), minute=30 if _u(seed, 3) < 0.5 else 0,
                                             second=0, microsecond=0)
        if close and local > close[1].astimezone(TZ) - timedelta(hours=1):
            local = (close[1] - timedelta(hours=2)).astimezone(TZ).replace(minute=0, second=0, microsecond=0)
        if local.astimezone(UTC) <= booked:
            local = (booked + timedelta(hours=3)).astimezone(TZ).replace(minute=0, second=0, microsecond=0)
        closed = close is not None and close[1] <= self.now
        if closed and close[0] == "Won":
            status = "showed"
        elif closed:
            status = "noshow" if _u(seed, 4) < 0.6 else "cancelled"
        else:
            status = "confirmed"
        end = local + timedelta(minutes=45 if c.vertical == "dental" else 30)
        return [{
            "id": "a" + contact["id"][1:],
            "calendarId": _ghl_id(w.seed, "calendar", c.index),
            "locationId": contact["locationId"],
            "contactId": contact["id"],
            "groupId": None,
            "title": f"{_offer(w, lead)} consultation - {lead.full_name}",
            "appointmentStatus": status,
            "status": status,
            "assignedUserId": self.agency.ghl_user_id,
            "address": c.address,
            "notes": "",
            "isRecurring": False,
            "startTime": local.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
            "dateAdded": iso(booked),
            "dateUpdated": iso(close[1] if closed else booked),
        }]

    def contact_notes(self, contact: dict) -> list:
        out = []
        lead = self.lead_of(contact)
        if lead is not None:
            appt = self.appointments(contact)
            if appt:
                at = parse_when(appt[0]["dateAdded"]) + timedelta(minutes=3)
                if at <= self.now:
                    out.append({"id": "t" + contact["id"][1:], "contactId": contact["id"],
                                "userId": self.agency.ghl_user_id,
                                "body": f"Booked: {appt[0]['title']} on {appt[0]['startTime'][:16]}.",
                                "dateAdded": iso(at)})
        out.extend(dict(n) for n in self.notes if n.get("contactId") == contact["id"])
        out.sort(key=lambda n: (n["dateAdded"], n["id"]), reverse=True)
        return out

    def contact_tasks(self, contact: dict) -> list:
        out = [dict(t) for t in self.tasks if t.get("contactId") == contact["id"]]
        out.sort(key=lambda t: (t.get("dateAdded") or "", t["id"]), reverse=True)
        return out

    def location_tags(self, c: Client) -> list:
        names = sorted({t for x in self.contacts(c) for t in x.get("tags") or []})
        return [{"id": _ghl_id(self.w.seed, "tag", c.index, n), "name": n, "locationId": c.ghl_location_id}
                for n in names]
