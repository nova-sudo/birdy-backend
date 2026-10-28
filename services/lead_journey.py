"""Lead journey: a newest-first timeline of one lead, from data Birdy stores
plus live GHL opportunities and appointments. Nothing here is persisted."""
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

from integrations.gohighlevel import ghl_integration, get_subaccount_tokens
from services.ghl_pipelines import stage_lookup
from services.lead_detail import _calls, _iso, _parse_dt, resolve_opportunities
from utils.phone_normalize import compute_match_keys

logger = logging.getLogger(__name__)

# Worst last: "reconnect" is the actionable one, so it wins over "unavailable".
_SEVERITY = {"ok": 0, "unavailable": 1, "reconnect": 2}
_MISSED = {"cancelled": "Appointment cancelled", "canceled": "Appointment cancelled",
           "noshow": "Appointment no-show", "no_show": "Appointment no-show", "no-show": "Appointment no-show"}


def _status_for(code: int) -> str:
    if code == 200:
        return "ok"
    return "reconnect" if code in (401, 403) else "unavailable"


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


async def _token_for(db, user_id: str, location_id):
    if not location_id:
        return None
    tokens = await get_subaccount_tokens(user_id, db.client)
    return (tokens or {}).get(location_id, {}).get("access_token")


async def live_opportunities(db, user_id: str, location_id, contact_id):
    """(status, raw GHL opportunities). status: ok | reconnect | unavailable. Never raises."""
    try:
        token = await _token_for(db, user_id, location_id)
        if not token or not contact_id:
            return "unavailable", []
        code, rows = await ghl_integration.search_contact_opportunities(location_id, token, contact_id)
        if code != 200:
            return _status_for(code), []
        return "ok", [r for r in rows if isinstance(r, dict)]
    except Exception as e:
        logger.warning("live_opportunities failed for location %s: %s", location_id, type(e).__name__)
        return "unavailable", []


async def _live_appointments(db, user_id: str, location_id, contact_id):
    try:
        token = await _token_for(db, user_id, location_id)
        if not token or not contact_id:
            return "unavailable", []
        code, rows = await ghl_integration.fetch_contact_appointments(token, contact_id)
        if code != 200:
            return _status_for(code), []
        return "ok", [r for r in rows if isinstance(r, dict)]
    except Exception as e:
        logger.warning("live_appointments failed for location %s: %s", location_id, type(e).__name__)
        return "unavailable", []


def _event(id_, kind, title, detail, at, **extra):
    dt = _parse_dt(at)
    if not dt:
        return None  # unparseable or missing: never sort to 1970
    return {"id": id_, "kind": kind, "title": title, "detail": detail,
            "at": dt.astimezone(timezone.utc).isoformat(), **extra}


def _path(url) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    return urlparse(url.strip()).path or "/"


def _host(url) -> str:
    if not isinstance(url, str) or not url.strip():
        return ""
    host = urlparse(url.strip()).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _duration(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _lead_event(contact: dict):
    src = contact.get("attributionSource") or {}
    detail = (contact.get("source") or "").strip() or src.get("utmSource") or src.get("campaign") or "GoHighLevel"
    return _event(f"lead:{contact.get('id')}", "lead", "Lead created", detail, contact.get("dateAdded"))


async def _meta_events(db, user_id, doc, keys):
    if not keys:
        return []
    query = {"user_id": user_id, "match_keys": {"$in": keys}}
    if doc.get("client_group_id"):
        # Same rule as the detail's attribution: pre-grouping Meta leads stay matchable.
        query["client_group_id"] = {"$in": [doc["client_group_id"], None, ""]}
    out = []
    for r in await db["facebook_leads"].find(query).to_list(None):
        ld = r.get("lead_data") or {}
        channel = "Instagram" if ld.get("platform") == "ig" else "Meta Ads"
        detail = f"{channel} · {ld['ad_name']}" if ld.get("ad_name") else channel
        out.append(_event(f"meta:{r['_id']}", "conversion", "Meta lead form submitted", detail,
                          ld.get("created_time")))
    return out


async def _website_events(db, user_id, group_id, keys):
    if not keys or not group_id:
        return []
    out = []
    rows = await db["tracked_leads"].find(
        {"user_id": user_id, "client_group_id": group_id, "match_keys": {"$in": keys}}).to_list(None)
    for r in rows:
        parts = [p for p in (_path(r.get("landing_page")), r.get("utm_source")) if p]
        out.append(_event(f"form:{r['_id']}", "conversion", "Website form submitted",
                          " · ".join(parts), r.get("created_at") or r.get("submitted_at")))
    return out


async def _visit_events(db, user_id, group_id, contact_id, keys):
    if not group_id:
        return []
    ors = []
    if keys:
        ors.append({"match_keys": {"$in": keys}})
    matches = await db["attribution_matches"].find(
        {"user_id": user_id, "client_group_id": group_id, "ghl_contact_id": contact_id}).to_list(None)
    ids = [m["visitor_id"] for m in matches if m.get("visitor_id")]
    if ids:
        ors.append({"_id": {"$in": ids}})
    if not ors:
        return []
    visitors = await db["attribution_visitors"].find(
        {"user_id": user_id, "client_group_id": group_id, "$or": ors}).to_list(None)

    out = []
    for v in visitors:
        first = v.get("first_touch") or {}
        paid = v.get("last_paid_touch") or {}
        touches = [("first", first)]
        if paid and _parse_dt(paid.get("ts")) != _parse_dt(first.get("ts")):
            touches.append(("paid", paid))
        for label, t in touches:
            path = _path(t.get("landing_page"))
            origin = _host(t.get("referrer")) or t.get("utm_source") or "Direct"
            out.append(_event(f"visit:{v['_id']}:{label}", "visit",
                              f"Visited {path}" if path else "Visited website", f"From {origin}", t.get("ts")))
    return out


def _call_events(calls):
    out = []
    for c in calls:
        direction = (c.get("direction") or "").capitalize()
        title = " ".join(p for p in (direction, c.get("status")) if p) or "Call"
        seconds = c.get("duration_seconds")
        extra = {}
        if isinstance(seconds, (int, float)) and seconds > 0:
            title += f" · {_duration(seconds)}"
            extra["duration_seconds"] = int(seconds)
        out.append(_event(f"call:{c['id']}", "call", title, "Call", c.get("started_at"), **extra))
    return out


def _appointment_events(appointments):
    out = []
    for a in appointments:
        title = (a.get("title") or "").strip() or "Appointment"
        status = str(a.get("appointmentStatus") or a.get("status") or "").lower()
        missed = _MISSED.get(status)
        at = (a.get("dateUpdated") or a.get("dateAdded")) if missed else a.get("dateAdded")
        out.append(_event(f"appt:{a.get('id')}", "appointment", missed or "Appointment booked", title, at,
                          starts_at=_iso(a.get("startTime"))))
    return out


def _stage_events(opps, stages):
    out = []
    for o in opps:
        info = stages.get(o.get("pipelineStageId")) or {}
        pipeline, stage = info.get("pipeline_name"), info.get("stage_name")
        oid = o.get("id")
        detail = " · ".join(p for p in (pipeline, stage) if p)
        out.append(_event(f"opp:{oid}:created", "stage", "Opportunity created", detail, o.get("createdAt")))
        status = o.get("status")
        if status and status != "open":
            out.append(_event(f"opp:{oid}:{status}", "stage", f"Opportunity {status}",
                              o.get("name") or pipeline or "", o.get("lastStatusChangeAt")))
        moved, created = _parse_dt(o.get("lastStageChangeAt")), _parse_dt(o.get("createdAt"))
        if moved and moved != created:
            out.append(_event(f"opp:{oid}:moved", "stage", f"Moved to {stage}" if stage else "Moved stage",
                              pipeline or "", o.get("lastStageChangeAt")))
    return out


async def build_lead_journey(db, user_id: str, contact_id: str) -> dict | None:
    doc = await db["ghl_contacts"].find_one({"user_id": user_id, "contact_data.id": contact_id})
    if not doc:
        return None
    contact = doc.get("contact_data") or {}
    location_id, group_id = doc.get("location_id"), doc.get("client_group_id")
    keys = doc.get("match_keys") or compute_match_keys(contact.get("email"), contact.get("phone"))

    opp_status, opps = await resolve_opportunities(db, user_id, doc, contact)
    appt_status, appointments = await _live_appointments(db, user_id, location_id, contact_id)
    stages = await stage_lookup(db, user_id, location_id) if (opps and location_id) else {}

    events = [_lead_event(contact)]
    events += await _meta_events(db, user_id, doc, keys)
    events += await _website_events(db, user_id, group_id, keys)
    events += await _visit_events(db, user_id, group_id, contact_id, keys)
    events += _call_events(await _calls(db, user_id, contact))
    events += _appointment_events(appointments)
    events += _stage_events(opps, stages or {})

    events = [e for e in events if e]
    events.sort(key=lambda e: (_parse_dt(e["at"]), e["id"]), reverse=True)
    return {"events": events, "ghl": _worse(opp_status, appt_status)}
