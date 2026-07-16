"""
services/hp_service.py
----------------------
HotProspector helper / service functions extracted from main.py.
"""

import hashlib
import logging
from datetime import datetime, date, timedelta

from pymongo import UpdateOne

from core.database import DB_NAME
from core.constants import META_CACHE_PRESETS, ghl_date_bounds
from utils.phone_normalize import normalize_phone, normalize_email
from integrations.gohighlevel import get_subaccount_tokens
from integrations.hotprospector import (
    HotProspectorIntegration,
    get_hotprospector_credentials,
    save_hotprospector_leads_to_collection,
    get_hotprospector_leads_from_collection,
    get_client_group_mapping,
)

logger = logging.getLogger(__name__)


def _in_window(iso, start, end):
    """True if an ISO timestamp's date falls within [start, end] (yyyy-mm-dd strings)."""
    if not iso:
        return False
    d = iso[:10]
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def _dur_seconds(c):
    """Parse a normalized call's duration (may be int/float/stringy) to seconds (float)."""
    v = c.get("duration")
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _compute_call_stats_all_presets(calls, total_leads):
    """
    Bucket every call into each date preset by call_time_iso. Returns
    {preset: {total_calls, inbound_count, outbound_count, transfers,
    leads_with_calls, total_leads, answered_calls, total_talk_min}}.
    "maximum" = all-time (no date filter).
    Mirrors GHL's cache_ghl_opp_stats_all_presets: fetch once, derive every preset.

    `calls` is the flat list of normalized calls for the location (matched or not);
    `leads_with_calls` counts distinct leads a call was matched to (matched_lead_id);
    `answered_calls` counts calls that connected (duration > 0); `total_talk_min`
    is the summed talk time in minutes.
    """
    cache = {}
    for preset in META_CACHE_PRESETS:
        start, end = ghl_date_bounds(preset)  # (None, None) for "maximum"
        total = inbound = outbound = transfers = answered = 0
        talk_seconds = 0.0
        leads_with = set()
        for c in calls:
            in_win = True if (start is None and end is None) else _in_window(c.get("call_time_iso"), start, end)
            if not in_win:
                continue
            total += 1
            cs = c.get("call_status")
            if cs == "inbound":
                inbound += 1
            elif cs == "outbound":
                outbound += 1
            if c.get("transfer"):
                transfers += 1
            dur = _dur_seconds(c)
            if dur > 0:
                answered += 1
                talk_seconds += dur
            # Count distinct contacted leads from each call's own HotProspector
            # leadId (phone-matched id as a fallback) so it isn't capped by the
            # paginated lead page.
            lid = c.get("lead_id") or c.get("matched_lead_id")
            if lid is not None and str(lid).strip():
                leads_with.add(str(lid))
        cache[preset] = {
            "total_calls": total,
            "inbound_count": inbound,
            "outbound_count": outbound,
            "transfers": transfers,
            "leads_with_calls": len(leads_with),
            "total_leads": total_leads,
            "answered_calls": answered,
            "total_talk_min": round(talk_seconds / 60, 1),
        }
    return cache


async def fetch_hp_leads_for_group(
        ghl_location_id: str,
        current_user: str,
        mongo_client,
        hp_credentials: dict,
        location_name: str
):
    """Fetch HotProspector leads for a single group in parallel"""
    try:
        if not hp_credentials:
            return ghl_location_id, 0

        # Try cache first
        cached_leads, total_count = await get_hotprospector_leads_from_collection(
            current_user,
            ghl_location_id,
            mongo_client,
            skip=0,
            limit=None
        )

        # Fetch fresh if no cache
        if not cached_leads:
            integration = HotProspectorIntegration(
                hp_credentials.get("api_uid"),
                hp_credentials.get("api_key")
            )

            try:
                success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)
                if success:
                    normalized_leads = [
                        integration.normalize_lead(lead, ghl_location_id, location_name)
                        for lead in hp_leads
                    ]
                    await save_hotprospector_leads_to_collection(
                        current_user,
                        ghl_location_id,
                        normalized_leads,
                        mongo_client
                    )
                    total_count = len(normalized_leads)
                else:
                    total_count = 0
            except Exception as e:
                logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
                total_count = 0

        return ghl_location_id, total_count

    except Exception as e:
        logger.error(f"Error fetching HP leads for {ghl_location_id}: {str(e)}")
        return ghl_location_id, 0


async def fetch_and_cache_hp_data(
        group_id: str,
        ghl_location_id: str,
        user_id: str,
        mongo_client
):
    """Fetch Hot Prospector data and save to cache"""
    try:
        db = mongo_client[DB_NAME]
        client_groups_collection = db["client_groups"]

        # Get HP credentials
        hp_credentials = await get_hotprospector_credentials(user_id, mongo_client)
        if not hp_credentials:
            logger.info(f"No HP credentials for user {user_id}")
            return

        # Get location name
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_name = subaccount_tokens.get(ghl_location_id, {}).get("name", "Unknown Location")

        # Fetch HP leads
        location_id, count = await fetch_hp_leads_for_group(
            ghl_location_id,
            user_id,
            mongo_client,
            hp_credentials,
            location_name
        )

        cache_data = {
            "ghl_location_id": location_id,
            "name": location_name,
            "metrics": {
                "total_leads": count
            }
        }

        await client_groups_collection.update_one(
            {"id": group_id},
            {
                "$set": {
                    "hotprospector_cache": cache_data,
                    "last_hp_refresh": datetime.utcnow()
                }
            }
        )

        logger.info(f"Cached HP data for group {group_id}: {count} leads")

    except Exception as e:
        logger.error(f"Error caching HP data: {e}")
        raise


def _call_identity(c):
    """Stable de-dup key for a normalized call log."""
    rid = c.get("recording_id")
    if rid:
        return f"rid:{rid}"
    return f"k:{c.get('call_time_iso')}|{c.get('from_number')}|{c.get('to_number')}|{c.get('lead_id')}"


def _merge_calls(existing, recent):
    """Union of two normalized-call lists, de-duped by call identity."""
    seen, out = set(), []
    for c in (existing or []) + (recent or []):
        ident = _call_identity(c)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(c)
    return out


async def _load_stored_calls(user_id, ghl_location_id, mongo_client):
    """All previously-stored (normalized) call logs for a location, flattened."""
    col = mongo_client[DB_NAME]["hotprospector_leads"]
    calls = []
    async for doc in col.find(
        {"user_id": user_id, "ghl_location_id": ghl_location_id},
        {"lead_data.call_logs": 1},
    ):
        calls.extend((doc.get("lead_data") or {}).get("call_logs") or [])
    return calls


def _hp_call_source_event_id(call: dict, user_id: str) -> str:
    """
    Stable idempotency key for an HP call. Prefer HP's recordingId; when it
    isn't present (short abandoned calls, transfer legs, older records),
    fall back to a deterministic hash of the call's identifying fields so
    re-runs still dedupe on the (source, source_event_id) unique index.
    """
    rid = call.get("recording_id")
    if rid:
        return f"hp_{rid}"
    parts = [
        str(user_id or ""),
        str(call.get("from_number") or ""),
        str(call.get("to_number") or ""),
        str(call.get("call_time_iso") or call.get("call_time") or ""),
        str(call.get("duration") or ""),
        str(call.get("lead_id") or ""),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"hp_h_{digest}"


def _parse_iso_maybe(value):
    """Best-effort ISO-8601 string → datetime; returns None on any failure."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def _persist_hp_calls_to_call_logs(
    calls,
    *,
    user_id: str,
    location_id: str,
    mongo_client,
):
    """
    Bulk-upsert each normalized HP call into the `call_logs` collection so
    /api/call_logs can serve HP calls the same way it serves GHL webhook
    calls. Dedup key: (source, source_event_id) — matches the existing
    unique index created by services/call_logs_service.create_call_logs_indexes.

    Field mapping — HP normalized call → call_logs schema:
        source          → "hotprospector"
        source_event_id → HP recordingId (or fallback hash — see above)
        contact_id      → matched_lead_id (NULL for unmatched calls; they
                          land under the "Unmatched" pseudo-lead in the UI)
        contact_phone   → from_number or to_number, whichever we've got
        direction       → call_status ("inbound" / "outbound")
        duration_seconds→ duration (already seconds per HP API convention)
        started_at      → parsed call_time_iso
        recording_url   → recording_url
        raw_payload     → full normalized call (keeps transfer, group,
                          city, state, speed_to_lead, etc. for debug)

    We $set on every upsert so a re-fetched call refreshes mutable fields
    (transfer often flips post-call). created_at / received_at stay stable
    via $setOnInsert.
    """
    if not calls:
        return

    col = mongo_client[DB_NAME]["call_logs"]
    now = datetime.utcnow()
    ops = []

    for c in calls:
        event_id = _hp_call_source_event_id(c, user_id)
        matched = c.get("matched_lead_id")

        doc = {
            "source": "hotprospector",
            "source_event_id": event_id,
            "user_id": user_id,
            "location_id": location_id,
            "contact_id": str(matched) if matched is not None else None,
            "contact_phone": c.get("from_number") or c.get("to_number") or None,
            "contact_email": None,
            "direction": (c.get("call_status") or None),
            "status": None,
            "duration_seconds": int(c.get("duration") or 0),
            "started_at": _parse_iso_maybe(c.get("call_time_iso") or c.get("call_time")),
            "ended_at": None,
            "recording_url": c.get("recording_url") or None,
            "raw_payload": c,
            "updated_at": now,
        }

        ops.append(UpdateOne(
            {"source": "hotprospector", "source_event_id": event_id},
            {
                "$set": doc,
                "$setOnInsert": {
                    "created_at": now,
                    "received_at": now,
                    "headers": {},
                },
            },
            upsert=True,
        ))

    # Batch to keep individual bulk_write payloads bounded on large locations.
    BATCH = 1000
    for i in range(0, len(ops), BATCH):
        await col.bulk_write(ops[i:i + BATCH], ordered=False)


async def fetch_and_cache_hp_call_center(
        user_id: str,
        ghl_location_id: str,
        mongo_client,
        integration: HotProspectorIntegration = None,
        location_name: str = None,
        client_group_name: str = None,
        mode: str = "backfill",
        recent_days: int = 3,
):
    """
    Fetch + cache the full call-center dataset for ONE GHL location: leads
    (SearchByUserInput) matched to their call logs (bulk FetchUserCallLog).

    Leads with no calls are kept with call_logs_count == 0. Persists leads (with
    nested call_logs) to the hotprospector_leads collection and refreshes metrics +
    last_hp_refresh on every client_group pointing at this location, so the
    cache-first endpoint can serve without re-hitting HotProspector.

    `integration` / `location_name` / `client_group_name` may be passed in by the
    cron to skip duplicate credential/token lookups; otherwise they're resolved here.
    This is the single canonical sync path shared by the endpoint and the cron — it
    replaces the old per-lead (N+1) call-log fetch.

    Returns:
        {ghl_location_id, total_leads, total_calls, inbound, outbound, success[, error]}
    """
    db = mongo_client[DB_NAME]
    client_groups_collection = db["client_groups"]

    def _fail(error: str):
        return {
            "ghl_location_id": ghl_location_id,
            "total_leads": 0, "total_calls": 0, "inbound": 0, "outbound": 0,
            "success": False, "error": error,
        }

    # Resolve credentials / integration if the caller didn't supply one.
    if integration is None:
        credentials = await get_hotprospector_credentials(user_id, mongo_client)
        if not credentials:
            logger.info(f"No HP credentials for user {user_id}")
            return _fail("not_connected")
        integration = HotProspectorIntegration(
            credentials.get("api_uid"),
            credentials.get("api_key"),
        )

    # Resolve display names if not supplied.
    if location_name is None:
        subaccount_tokens = await get_subaccount_tokens(user_id, mongo_client)
        location_name = subaccount_tokens.get(ghl_location_id, {}).get("name", "Unknown Location")
    if client_group_name is None:
        mapping = await get_client_group_mapping(user_id, mongo_client)
        client_group_name = mapping.get(ghl_location_id)

    # 1. Leads for this location — now paginates every page of SearchByUserInput
    #    up to MAX_PAGES (see integrations/hotprospector.py). `truncated` is
    #    True when the loop stopped short (page-N failure or MAX_PAGES hit) —
    #    log-only for visibility; the save function is already safe against
    #    partial data by default (upsert, never delete missing).
    success, leads_info = await integration.fetch_all_leads_from_ghl_location(
        ghl_location_id, with_meta=True
    )
    if not success:
        logger.warning(f"HP leads fetch failed for {ghl_location_id}: {leads_info}")
        return _fail(leads_info.get("error") if isinstance(leads_info, dict) else "fetch_failed")

    hp_leads = leads_info["leads"]
    total_lead_count = leads_info["total_count"]
    leads_truncated = bool(leads_info.get("truncated"))
    if leads_truncated:
        logger.warning(
            f"HP leads pagination truncated for {ghl_location_id}: "
            f"got {len(hp_leads)} of {total_lead_count} — existing leads left in place."
        )

    normalized_leads = [
        integration.normalize_lead(lead, ghl_location_id, location_name, client_group_name)
        for lead in hp_leads
    ]

    # 2. Call logs for this location (date-windowed; FetchUserCallLog needs dates).
    #    - backfill: pull the full history once.
    #    - incremental: keep the stored history and only re-pull the last few days,
    #      merging by call identity — so the daily run stops re-fetching ~400 days.
    if mode == "incremental":
        existing = await _load_stored_calls(user_id, ghl_location_id, mongo_client)
        rec_ok, recent_raw = await integration.fetch_call_logs_for_location(
            ghl_location_id, lookback_days=recent_days
        )
        if not rec_ok:
            logger.warning(f"HP incremental call fetch failed for {ghl_location_id}")
        recent_norm = [integration.normalize_user_call_log(raw, location_name) for raw in (recent_raw or [])]
        normalized_calls = _merge_calls(existing, recent_norm)
    else:
        calls_ok, raw_calls = await integration.fetch_call_logs_for_location(ghl_location_id)
        if not calls_ok:
            logger.warning(f"HP call-log fetch failed for {ghl_location_id}")
        normalized_calls = [integration.normalize_user_call_log(raw, location_name) for raw in (raw_calls or [])]

    # 3. Nest each call under its lead, matched by phone (then email, then leadId).
    #    Leads with no calls keep an empty list. Calls that match no lead are still
    #    counted in the stats below (they just aren't shown per-lead).
    lead_by_key = {}
    for lead in normalized_leads:
        lead["call_logs"] = []
        for raw_phone in (lead.get("phone"), lead.get("mobile")):
            np = normalize_phone(raw_phone)
            if np:
                lead_by_key.setdefault(f"phone:{np}", lead)
        ne = normalize_email(lead.get("email"))
        if ne:
            lead_by_key.setdefault(f"email:{ne}", lead)
        lid = lead.get("id")
        if lid is not None:
            lead_by_key.setdefault(f"lead:{lid}", lead)

    for call in normalized_calls:
        match = None
        # Phone match on either leg of the call (the lead's number is one of them).
        for num in (call.get("from_number"), call.get("to_number")):
            np = normalize_phone(num)
            if np and f"phone:{np}" in lead_by_key:
                match = lead_by_key[f"phone:{np}"]
                break
        # leadId fallback when phone didn't resolve.
        if match is None and call.get("lead_id") is not None:
            match = lead_by_key.get(f"lead:{str(call.get('lead_id'))}")
        if match is not None:
            match["call_logs"].append(call)
            call["matched_lead_id"] = match.get("id")

    for lead in normalized_leads:
        lead["call_logs_count"] = len(lead["call_logs"])

    # 3c. Persist EVERY call (matched or not) as its own document in the
    #     shared call_logs collection. Sales-Hub doesn't consume this yet
    #     (it reads /api/hotprospector/call-center), but /api/call_logs now
    #     serves HP rows for future unification with the GHL webhook feed.
    await _persist_hp_calls_to_call_logs(
        normalized_calls,
        user_id=user_id,
        location_id=ghl_location_id,
        mongo_client=mongo_client,
    )

    # 3d. Synthetic "Unmatched calls" pseudo-lead. Every call that didn't
    #     hit a real lead by phone / email / leadId gets bucketed under one
    #     synthetic lead here, so the Sales-Hub "Leads" tab shows them and
    #     the visible call durations reconcile with Overview's Talk Time
    #     card (which sums ALL calls, matched or not). Only added when
    #     there are actually unmatched calls — an all-matched location
    #     stays clean.
    unmatched_calls = [c for c in normalized_calls if not c.get("matched_lead_id")]
    if unmatched_calls:
        unmatched_lead_id = f"__unmatched_{ghl_location_id}__"
        unmatched_lead = {
            "id": unmatched_lead_id,
            # first_name is what Sales-Hub's mapLead reads for the display
            # name (fullName = f"{first_name} {last_name}".trim()), so we
            # put the whole label there and leave last_name blank.
            "first_name": "Unmatched calls",
            "last_name": "",
            "email": None,
            "phone": None,
            "mobile": None,
            "company": None,
            "city": None,
            "state": None,
            "country_code": None,
            "tags": [],
            "ghl_location_id": ghl_location_id,
            "ghl_location_name": location_name,
            "client_name": client_group_name,
            "call_logs": unmatched_calls,
            "call_logs_count": len(unmatched_calls),
            "_is_unmatched_bucket": True,  # marker for future UI polish / filtering
        }
        normalized_leads.append(unmatched_lead)

    # 3b. All-time totals from ALL calls (matched or not).
    total_calls = len(normalized_calls)
    inbound = sum(1 for c in normalized_calls if c.get("call_status") == "inbound")
    outbound = sum(1 for c in normalized_calls if c.get("call_status") == "outbound")
    answered = sum(1 for c in normalized_calls if _dur_seconds(c) > 0)
    talk_min = round(sum(_dur_seconds(c) for c in normalized_calls) / 60, 1)

    # 4. Derive per-preset call stats (windowed by call_time) from ALL calls so the
    #    Sales-Hub Overview shows date-filtered KPIs per client — mirrors GHL's
    #    cache_ghl_opp_stats_all_presets (fetch once, bucket every preset).
    call_cache = _compute_call_stats_all_presets(normalized_calls, total_lead_count)

    # 5. Persist leads (with nested call logs) to the cache collection.
    await save_hotprospector_leads_to_collection(
        user_id, ghl_location_id, normalized_leads, mongo_client
    )

    # 6. Refresh metrics + per-preset call stats + freshness on every
    #    client_group at this location (dot-notation so presets don't clobber).
    set_fields = {
        "hotprospector_cache.ghl_location_id": ghl_location_id,
        "hotprospector_cache.name": location_name,
        "hotprospector_cache.metrics.total_leads": total_lead_count,
        "hotprospector_cache.metrics.total_calls": total_calls,
        "hotprospector_cache.metrics.inbound_count": inbound,
        "hotprospector_cache.metrics.outbound_count": outbound,
        "hotprospector_cache.metrics.answered_calls": answered,
        "hotprospector_cache.metrics.total_talk_min": talk_min,
        "last_hp_refresh": datetime.utcnow(),
    }
    for preset, stats in call_cache.items():
        set_fields[f"hotprospector_call_cache.{preset}"] = stats
    set_fields["hotprospector_call_cache.updated_at"] = datetime.utcnow().isoformat()

    await client_groups_collection.update_many(
        {"user_id": user_id, "ghl_location_id": ghl_location_id},
        {"$set": set_fields},
    )

    logger.info(
        f"HP call-center cached for {ghl_location_id}: "
        f"{total_lead_count} leads ({len(normalized_leads)} on page), "
        f"{total_calls} calls (in={inbound}, out={outbound})"
    )
    return {
        "ghl_location_id": ghl_location_id,
        "total_leads": total_lead_count,
        "total_calls": total_calls,
        "inbound": inbound,
        "outbound": outbound,
        "success": True,
    }
