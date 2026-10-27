"""Assemble the single-lead detail payload from data Birdy already stores."""
import re
from datetime import datetime, timezone

from services.contact_classifier import classify_contact_type
from services.ghl_pipelines import stage_lookup
from utils.phone_normalize import compute_match_keys, normalize_phone


ANSWERED = {"completed", "answered"}


def _parse_dt(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
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


def _opportunities(contact: dict, stages: dict) -> list[dict]:
    rows = []
    for opp in contact.get("opportunities") or []:
        if not isinstance(opp, dict):
            continue
        stage = stages.get(opp.get("pipelineStageId")) or {}
        rows.append({
            "id": opp.get("id"),
            "status": opp.get("status"),
            "monetary_value": opp.get("monetaryValue"),
            "pipeline_name": stage.get("pipeline_name"),
            "stage_name": stage.get("stage_name"),
            "created_at": opp.get("createdAt") or opp.get("dateAdded"),
            "last_status_change_at": opp.get("lastStatusChangeAt"),
        })
    rows.sort(key=lambda r: _parse_dt(r["created_at"]) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    return rows


def _find_ad(group: dict | None, ad_id: str) -> dict:
    if not group or not ad_id:
        return {}
    fb = group.get("facebook_cache") or {}
    ads = (fb.get("entities") or {}).get("ads") or (fb.get("maximum") or {}).get("ads") or []
    return next((a for a in ads if a.get("id") == ad_id), {})


async def _attribution(db, user_id: str, doc: dict, contact: dict):
    keys = doc.get("match_keys") or compute_match_keys(contact.get("email"), contact.get("phone"))
    meta = None
    if keys:
        meta = await db["facebook_leads"].find_one({"user_id": user_id, "match_keys": {"$in": keys}})
    src = contact.get("attributionSource") or {}
    if not meta and not (src.get("adId") or src.get("campaign") or src.get("utmSource")):
        return None

    ld = (meta or {}).get("lead_data") or {}
    ad_id = ld.get("ad_id") or src.get("adId") or ""
    group = None
    if doc.get("client_group_id"):
        group = await db["client_groups"].find_one({"id": doc["client_group_id"], "user_id": user_id})
    ad = _find_ad(group, ad_id)

    answers = []
    for f in ld.get("field_data") or []:
        values = f.get("values") or []
        answers.append({"question": f.get("name"), "answer": values[0] if values else None})

    return {
        "platform": ld.get("platform"),
        "is_organic": ld.get("is_organic"),
        "ad_id": ad_id or None,
        "ad_name": ld.get("ad_name") or src.get("utmContent") or ad.get("name") or None,
        "adset_name": ld.get("adset_name") or src.get("utmMedium") or None,
        "campaign_name": ld.get("campaign_name") or src.get("campaign") or None,
        "campaign_id": ld.get("campaign_id") or src.get("campaignId") or ad.get("campaign_id") or None,
        "creative_title": ad.get("creative_title"),
        "creative_body": ad.get("creative_body"),
        "creative_image": ad.get("creative_image"),
        "creative_thumbnail": ad.get("creative_thumbnail"),
        "submitted_at": ld.get("created_time"),
        "form_answers": answers,
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


def _time_to_close(contact: dict):
    won = [o for o in contact.get("opportunities") or []
           if isinstance(o, dict) and o.get("status") == "won"]
    won.sort(key=lambda o: _parse_dt(o.get("lastStatusChangeAt")) or datetime.min.replace(tzinfo=timezone.utc),
             reverse=True)
    if not won:
        return None
    opp = won[0]
    start = _parse_dt(contact.get("dateAdded"))
    end = _parse_dt(opp.get("lastStatusChangeAt"))
    if not start or not end:
        return None
    return int((end - start).total_seconds())


def _engagement(contact: dict, calls: list[dict]) -> dict:
    return {
        "dials": sum(1 for c in calls if c.get("direction") == "outbound"),
        "calls": len(calls),
        "answered": sum(1 for c in calls if c.get("status") in ANSWERED),
        "time_to_close_seconds": _time_to_close(contact),
        "touchpoints": len(calls),
    }


async def build_lead_detail(db, user_id: str, contact_id: str) -> dict | None:
    doc = await db["ghl_contacts"].find_one({"user_id": user_id, "contact_data.id": contact_id})
    if not doc:
        return None
    contact = doc.get("contact_data") or {}

    stages = await stage_lookup(db, user_id, doc.get("location_id")) if doc.get("location_id") else {}
    calls = await _calls(db, user_id, contact)

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
        "opportunities": _opportunities(contact, stages or {}),
        "attribution": await _attribution(db, user_id, doc, contact),
        "calls": calls,
        "engagement": _engagement(contact, calls),
    }
