"""
services/hp_service.py
----------------------
HotProspector helper / service functions extracted from main.py.
"""

import logging
from datetime import datetime

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


def _compute_call_stats_all_presets(leads, total_leads):
    """
    Bucket every call (across all leads) into each date preset by call_time_iso.
    Returns {preset: {total_calls, inbound_count, outbound_count, transfers,
    leads_with_calls, total_leads}}. "maximum" = all-time (no date filter).
    Mirrors GHL's cache_ghl_opp_stats_all_presets: fetch once, derive every preset.
    """
    all_calls = [log for lead in leads for log in (lead.get("call_logs") or [])]
    cache = {}
    for preset in META_CACHE_PRESETS:
        start, end = ghl_date_bounds(preset)  # (None, None) for "maximum"
        total = inbound = outbound = transfers = 0
        leads_with = set()
        for log in all_calls:
            in_win = True if (start is None and end is None) else _in_window(log.get("call_time_iso"), start, end)
            if not in_win:
                continue
            total += 1
            cs = log.get("call_status")
            if cs == "inbound":
                inbound += 1
            elif cs == "outbound":
                outbound += 1
            if log.get("transfer"):
                transfers += 1
            lid = log.get("lead_id")
            if lid is not None:
                leads_with.add(str(lid))
        cache[preset] = {
            "total_calls": total,
            "inbound_count": inbound,
            "outbound_count": outbound,
            "transfers": transfers,
            "leads_with_calls": len(leads_with),
            "total_leads": total_leads,
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


async def fetch_and_cache_hp_call_center(
        user_id: str,
        ghl_location_id: str,
        mongo_client,
        integration: HotProspectorIntegration = None,
        location_name: str = None,
        client_group_name: str = None,
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

    # 1. Leads for this location (single SearchByUserInput call).
    success, hp_leads = await integration.fetch_all_leads_from_ghl_location(ghl_location_id)
    if not success:
        logger.warning(f"HP leads fetch failed for {ghl_location_id}: {hp_leads}")
        return _fail(hp_leads.get("error") if isinstance(hp_leads, dict) else "fetch_failed")

    normalized_leads = [
        integration.normalize_lead(lead, ghl_location_id, location_name, client_group_name)
        for lead in hp_leads
    ]

    # 2. Call logs — fetched ACCOUNT-WIDE (no locationId). HotProspector does not
    #    reliably tag call logs with the GHL locationId, so we pull all of the
    #    account's calls and assign each to this client by matching the call's phone
    #    numbers (or leadId) to this location's leads. Phone/email is the reliable
    #    join — the call's leadId scheme doesn't line up with SearchByUserInput's.
    calls_ok, calls_payload = await integration.fetch_user_call_logs()
    raw_calls = calls_payload.get("call_logs", []) if calls_ok else []
    if not calls_ok:
        logger.warning(f"HP call-log fetch failed: {calls_payload}")

    # 3. Index this location's leads by phone / email / leadId, then assign each
    #    call to the matching lead. Leads with no calls keep an empty list.
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

    for raw in raw_calls:
        match = None
        # Phone match on either leg of the call (lead's number is one of them).
        for num in (raw.get("from_number"), raw.get("to_number")):
            np = normalize_phone(num)
            if np and f"phone:{np}" in lead_by_key:
                match = lead_by_key[f"phone:{np}"]
                break
        # leadId fallback when phone didn't resolve.
        if match is None and raw.get("leadId") is not None:
            match = lead_by_key.get(f"lead:{str(raw.get('leadId'))}")
        if match is None:
            continue  # belongs to another client's leads
        match["call_logs"].append(integration.normalize_user_call_log(raw, location_name))

    # 3b. Tally counts from the matched calls.
    total_calls = inbound = outbound = 0
    for lead in normalized_leads:
        logs = lead["call_logs"]
        lead["call_logs_count"] = len(logs)
        total_calls += len(logs)
        for log in logs:
            if log.get("call_status") == "outbound":
                outbound += 1
            elif log.get("call_status") == "inbound":
                inbound += 1

    # 4. Derive per-preset call stats (windowed by call_time) so the Sales-Hub
    #    Overview can show date-filtered KPIs per client — mirrors GHL's
    #    cache_ghl_opp_stats_all_presets (fetch once, bucket every preset).
    call_cache = _compute_call_stats_all_presets(normalized_leads, len(normalized_leads))

    # 5. Persist leads (with nested call logs) to the cache collection.
    await save_hotprospector_leads_to_collection(
        user_id, ghl_location_id, normalized_leads, mongo_client
    )

    # 6. Refresh metrics + per-preset call stats + freshness on every
    #    client_group at this location (dot-notation so presets don't clobber).
    set_fields = {
        "hotprospector_cache.ghl_location_id": ghl_location_id,
        "hotprospector_cache.name": location_name,
        "hotprospector_cache.metrics.total_leads": len(normalized_leads),
        "hotprospector_cache.metrics.total_calls": total_calls,
        "hotprospector_cache.metrics.inbound_count": inbound,
        "hotprospector_cache.metrics.outbound_count": outbound,
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
        f"{len(normalized_leads)} leads, {total_calls} calls (in={inbound}, out={outbound})"
    )
    return {
        "ghl_location_id": ghl_location_id,
        "total_leads": len(normalized_leads),
        "total_calls": total_calls,
        "inbound": inbound,
        "outbound": outbound,
        "success": True,
    }
