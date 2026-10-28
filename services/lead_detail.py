"""Assemble the single-lead detail payload from data Birdy already stores."""
import asyncio
import re
from datetime import datetime, timezone

from services.contact_classifier import classify_contact_type
from services.ghl_live import live_opportunities
from services.ghl_pipelines import stage_lookup
from utils.phone_normalize import compute_match_keys, normalize_phone


ANSWERED = {"completed", "answered"}
GHL_BUDGET_SECONDS = 3.0  # overall cap on live GHL work before a page falls back to stored data


def _parse_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def _iso(value):
    if isinstance(value, datetime):
        return _parse_dt(value).isoformat()  # naive Mongo datetimes are UTC
    return value


def _contact_name(contact: dict) -> str:
    name = (contact.get("contactName") or "").strip()
    if name:
        return name
    first = (contact.get("firstName") or "").strip()
    last = (contact.get("lastName") or "").strip()
    return f"{first} {last}".strip()


def _opportunities(opps: list, stages: dict) -> list[dict]:
    rows = []
    for opp in opps:
        stage = stages.get(opp.get("pipelineStageId")) or {}
        rows.append({
            "id": opp.get("id"),
            "name": opp.get("name"),
            "status": opp.get("status"),
            "monetary_value": opp.get("monetaryValue"),
            "pipeline_name": stage.get("pipeline_name"),
            "stage_name": stage.get("stage_name"),
            "created_at": opp.get("createdAt") or opp.get("dateAdded"),
            "last_status_change_at": opp.get("lastStatusChangeAt"),
            "last_stage_change_at": opp.get("lastStageChangeAt"),
        })
    rows.sort(key=lambda r: _parse_dt(r["created_at"]) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return rows


def _find_entity(group: dict | None, kind: str, entity_id) -> dict:
    if not group or entity_id in (None, ""):
        return {}
    fb = group.get("facebook_cache") or {}
    rows = (fb.get("entities") or {}).get(kind) or (fb.get("maximum") or {}).get(kind) or []
    target = str(entity_id)
    return next((r for r in rows if isinstance(r, dict) and str(r.get("id")) == target), {})


def _form_answers(field_data) -> list[dict]:
    if isinstance(field_data, dict):
        return [{"question": k, "answer": v} for k, v in field_data.items()]
    answers = []
    for f in field_data or []:
        if not isinstance(f, dict):
            continue
        values = f.get("values") or []
        answers.append({"question": f.get("name"), "answer": values[0] if values else None})
    return answers


async def _attribution(db, user_id: str, doc: dict, contact: dict):
    keys = doc.get("match_keys") or compute_match_keys(contact.get("email"), contact.get("phone"))
    meta = None
    if keys:
        query = {"user_id": user_id, "match_keys": {"$in": keys}}
        if doc.get("client_group_id"):
            # Some Meta leads were stored before grouping; keep them matchable.
            query["client_group_id"] = {"$in": [doc["client_group_id"], None, ""]}
        meta = await db["facebook_leads"].find_one(query, sort=[("lead_data.created_time", -1)])
    src = contact.get("attributionSource") or {}
    if not meta and not (src.get("adId") or src.get("campaign") or src.get("utmSource")):
        return None

    ld = (meta or {}).get("lead_data") or {}
    ad_id = ld.get("ad_id") or src.get("adId") or ""
    group = None
    if doc.get("client_group_id"):
        group = await db["client_groups"].find_one({"id": doc["client_group_id"], "user_id": user_id})
    ad = _find_entity(group, "ads", ad_id)
    adset_id = ld.get("adset_id") or ad.get("adset_id")
    campaign_id = ld.get("campaign_id") or src.get("campaignId") or ad.get("campaign_id")
    adset = _find_entity(group, "adsets", adset_id)
    campaign = _find_entity(group, "campaigns", campaign_id)

    created = ld.get("created_time")
    parsed = _parse_dt(created)
    submitted_at = _iso(parsed) if parsed else created

    return {
        "platform": ld.get("platform"),
        "is_organic": ld.get("is_organic"),
        "ad_id": str(ad_id) if ad_id else None,
        "ad_name": ld.get("ad_name") or ad.get("name") or src.get("utmContent") or None,
        "adset_name": ld.get("adset_name") or adset.get("name") or src.get("utmMedium") or None,
        "campaign_name": ld.get("campaign_name") or campaign.get("name") or src.get("campaign") or None,
        "campaign_id": campaign_id or None,
        "creative_title": ad.get("creative_title"),
        "creative_body": ad.get("creative_body"),
        "creative_image": ad.get("creative_image"),
        "creative_thumbnail": ad.get("creative_thumbnail"),
        "submitted_at": submitted_at,
        "form_answers": _form_answers(ld.get("field_data")),
        "utm": {
            "source": src.get("utmSource"),
            "medium": src.get("utmMedium"),
            "campaign": src.get("utmCampaign") or src.get("campaign"),
            "content": src.get("utmContent"),
        },
    }


async def _calls(db, user_id: str, contact: dict) -> list[dict]:
    ors = [{"contact_id": contact.get("id")}]
    email = (contact.get("email") or "").strip()
    if email:
        ors.append({"contact_email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}})
    phone_key = normalize_phone(contact.get("phone"))
    if phone_key:
        # Tolerate any separators/country prefix between the digits of the canonical 10.
        pattern = r"\D*".join(phone_key) + r"$"
        ors.append({"contact_phone": {"$regex": pattern}})

    rows = await db["call_logs"].find({"user_id": user_id, "$or": ors}).to_list(None)
    out = []
    for r in rows:
        by_id = r.get("contact_id") == contact.get("id")
        by_email = bool(email) and (r.get("contact_email") or "").strip().lower() == email.lower()
        by_phone = bool(phone_key) and normalize_phone(r.get("contact_phone")) == phone_key
        if not (by_id or by_email or by_phone):
            continue
        row = {
            "id": str(r["_id"]),
            "source": r.get("source"),
            "direction": r.get("direction"),
            "status": r.get("status"),
            "duration_seconds": r.get("duration_seconds"),
            "started_at": _iso(r.get("started_at")),
            "recording_url": r.get("recording_url"),
            "_sort": _parse_dt(r.get("started_at")) or datetime.min.replace(tzinfo=timezone.utc),
        }
        if "transcript" in r:
            row["transcript"] = r["transcript"]
        out.append(row)
    out.sort(key=lambda c: c["_sort"], reverse=True)
    for c in out:
        del c["_sort"]
    return out


def _time_to_close(contact: dict, opps: list):
    """Seconds from the contact being added to the earliest won opportunity."""
    ends = [_parse_dt(o.get("lastStatusChangeAt")) for o in opps if o.get("status") == "won"]
    ends = [e for e in ends if e]
    start = _parse_dt(contact.get("dateAdded"))
    if not ends or not start:
        return None
    seconds = int((min(ends) - start).total_seconds())
    return seconds if seconds >= 0 else None


def _engagement(contact: dict, calls: list[dict], opps: list) -> dict:
    return {
        "dials": sum(1 for c in calls if c.get("direction") == "outbound"),
        "calls": len(calls),
        "answered": sum(1 for c in calls if c.get("status") in ANSWERED),
        "time_to_close_seconds": _time_to_close(contact, opps),
        "touchpoints": len(calls),
    }


async def resolve_opportunities(db, user_id: str, doc: dict, contact: dict, **live_kwargs):
    """(status, raw opportunities): live from GHL when it answers, else the stored ones.

    status is ok | reconnect | unavailable; the list is always usable.
    """
    stored = [o for o in contact.get("opportunities") or [] if isinstance(o, dict)]
    if not doc.get("location_id"):
        return "unavailable", stored
    status, live = await live_opportunities(db, user_id, doc["location_id"], contact.get("id"), **live_kwargs)
    return (status, live) if status == "ok" else (status, stored)


async def _opportunities_within_budget(db, user_id: str, doc: dict, contact: dict):
    try:
        return await asyncio.wait_for(resolve_opportunities(db, user_id, doc, contact), GHL_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return "unavailable", [o for o in contact.get("opportunities") or [] if isinstance(o, dict)]


async def _stages_within_budget(db, user_id: str, location_id):
    if not location_id:
        return {}
    try:
        return await asyncio.wait_for(stage_lookup(db, user_id, location_id), GHL_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        return {}


async def build_lead_detail(db, user_id: str, contact_id: str) -> dict | None:
    doc = await db["ghl_contacts"].find_one({"user_id": user_id, "contact_data.id": contact_id})
    if not doc:
        return None
    contact = doc.get("contact_data") or {}

    (_, opps), calls, attribution, stages = await asyncio.gather(
        _opportunities_within_budget(db, user_id, doc, contact),
        _calls(db, user_id, contact),
        _attribution(db, user_id, doc, contact),
        _stages_within_budget(db, user_id, doc.get("location_id")),  # only needs the location
    )

    return {
        "contact": {
            "id": contact.get("id"),
            "name": _contact_name(contact),
            "first_name": contact.get("firstName") or "",
            "last_name": contact.get("lastName") or "",
            "email": contact.get("email") or "",
            "phone": contact.get("phone") or "",
            "tags": contact.get("tags") or [],
            "source": contact.get("source") or "",
            "date_added": contact.get("dateAdded") or "",
            "assigned_to": contact.get("assignedTo"),
            "location_id": doc.get("location_id"),
            "group_id": doc.get("client_group_id"),
            "group_name": doc.get("client_group_name"),
            "type": classify_contact_type(contact),
        },
        "opportunities": _opportunities(opps, stages or {}),
        "attribution": attribution,
        "calls": calls,
        "engagement": _engagement(contact, calls, opps),
    }
