"""
services/ghl_lead_push.py
-------------------------
Pushing a landing-page lead into the client's own GoHighLevel.

A lead captured off a landing page lands in Birdy, which is enough to report on
and useless to the person who has to ring them. Their whole workflow — the
nurture sequence, the pipeline, the dialler — lives in GoHighLevel, and a lead
that never gets there never gets called.

So this creates the contact. It is **opt-in per client**
(`lead_collection.push_to_ghl`) and off by default, because writing into someone
else's live CRM is not a thing to do by assumption: it fires their automations
and, done carelessly, litters their database with duplicates.

Hence three guards, in order of how much damage they prevent:

  1. **Never push a person who is already there.** `match_keys` is checked
     against `ghl_contacts` first. GoHighLevel does its own upsert by email, but
     relying on that would still fire the client's "new lead" workflows for
     somebody they have been talking to for a month.
  2. **Never push the same lead twice.** `pushed_to_ghl` is stamped on success,
     and the query that finds work excludes it, so a retry after a timeout can't
     duplicate.
  3. **Never lose the lead to a failed push.** The Birdy row is written first and
     independently; this runs afterwards and is allowed to fail. A GHL outage
     costs the CRM copy, never the lead itself.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from core.database import DB_NAME
from integrations.gohighlevel import get_subaccount_tokens
from services import lead_collection as lead_collection_service
from services.tracked_leads import TRACKED_LEADS

logger = logging.getLogger(__name__)

_CONTACTS_URL = "https://services.leadconnectorhq.com/contacts/"
_GHL_VERSION = "2021-07-28"

# Marks the contact in the client's CRM as ours, so an agency looking at a
# contact can see where it came from rather than finding a mystery record.
SOURCE_LABEL = "Birdy (landing page)"


def _split_name(name: str | None) -> tuple[str, str]:
    if not name:
        return "", ""
    parts = name.strip().split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


async def _already_in_crm(db, lead: dict) -> str | None:
    """The existing contact id for this person, if the CRM already has them."""
    keys = lead.get("match_keys") or []
    if not keys:
        return None
    existing = await db["ghl_contacts"].find_one(
        {"client_group_id": lead["client_group_id"], "match_keys": {"$in": keys}},
        projection={"contact_id": 1},
    )
    return existing.get("contact_id") if existing else None


async def push_lead(lead: dict, location_id: str, access_token: str, db) -> dict:
    """
    Create one contact in GoHighLevel.

    Returns `{"pushed": bool, "reason": str, "contact_id": str | None}`. Never
    raises: the caller is a background sweep over many leads and one failure must
    not stop the rest.
    """
    existing = await _already_in_crm(db, lead)
    if existing:
        # Not a failure. Mark it done so the sweep stops reconsidering them.
        await db[TRACKED_LEADS].update_one(
            {"_id": lead["_id"]},
            {"$set": {"pushed_to_ghl": True, "ghl_contact_id": existing,
                      "pushed_at": datetime.utcnow()}},
        )
        return {"pushed": False, "reason": "already_in_crm", "contact_id": existing}

    first, last = _split_name(lead.get("name"))
    body = {
        "locationId": location_id,
        "firstName": first,
        "lastName": last,
        "email": lead.get("email") or None,
        "phone": lead.get("phone") or None,
        "source": SOURCE_LABEL,
    }
    # GHL rejects explicit nulls on some fields, so send only what we have.
    body = {k: v for k, v in body.items() if v not in (None, "")}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                _CONTACTS_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Version": _GHL_VERSION,
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as e:
        logger.warning("GHL push failed for lead %s: %s", lead.get("_id"), e)
        return {"pushed": False, "reason": "network_error", "contact_id": None}

    if response.status_code >= 400:
        logger.warning(
            "GHL refused contact for lead %s: %s %s",
            lead.get("_id"), response.status_code, response.text[:300],
        )
        return {"pushed": False, "reason": f"http_{response.status_code}", "contact_id": None}

    payload = response.json() or {}
    contact = payload.get("contact") or payload
    contact_id = contact.get("id")

    await db[TRACKED_LEADS].update_one(
        {"_id": lead["_id"]},
        {"$set": {"pushed_to_ghl": True, "ghl_contact_id": contact_id,
                  "pushed_at": datetime.utcnow()}},
    )
    logger.info("Pushed lead %s into GHL as contact %s", lead.get("_id"), contact_id)
    return {"pushed": True, "reason": "created", "contact_id": contact_id}


async def push_pending_for_group(group: dict, mongo_client, limit: int = 100) -> dict:
    """
    Push this client's un-pushed leads, if they asked for that.

    Returns a small summary for the cron log. A client who hasn't opted in, or
    whose GHL token is missing, is a no-op rather than an error — neither is
    something going wrong.
    """
    config = lead_collection_service.read(group)
    if not config["push_to_ghl"]:
        return {"skipped": "not_enabled"}

    location_id = group.get("ghl_location_id")
    if not location_id:
        return {"skipped": "no_ghl_location"}

    tokens = await get_subaccount_tokens(group["user_id"], mongo_client)
    access_token = (tokens.get(location_id) or {}).get("access_token")
    if not access_token:
        return {"skipped": "no_access_token"}

    db = mongo_client[DB_NAME]
    pending = await db[TRACKED_LEADS].find(
        {"client_group_id": group["id"], "pushed_to_ghl": False},
        sort=[("submitted_at", 1)],
        limit=limit,
    ).to_list(length=limit)

    pushed = skipped = failed = 0
    for lead in pending:
        try:
            result = await push_lead(lead, location_id, access_token, db)
        except Exception as e:  # one bad lead must not stall the rest
            logger.error("GHL push raised for lead %s: %s", lead.get("_id"), e, exc_info=True)
            failed += 1
            continue
        if result["pushed"]:
            pushed += 1
        elif result["reason"] == "already_in_crm":
            skipped += 1
        else:
            failed += 1

    return {"considered": len(pending), "pushed": pushed, "skipped": skipped, "failed": failed}
