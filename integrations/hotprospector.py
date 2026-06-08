import httpx
import json
from datetime import datetime
import logging
import os
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from typing import List, Tuple, Optional, Dict
import asyncio

from utils.phone_normalize import compute_match_keys

# Load environment variables
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# Hot Prospector API configuration
HOTPROSPECTOR_CONFIG = {
    # Documented production host is app.hotprospector.com; overridable via env so
    # the host can be flipped without a code change if the API moves.
    "base_url": os.getenv("HOTPROSPECTOR_BASE_URL", "https://app.hotprospector.com/glu/custom_api"),
    "max_concurrent_requests": 10,
    "batch_size": 500,
    "request_timeout": 60.0,
    "leads_cache_duration": 300  # 5 minutes cache for leads
}

# In-memory cache for pending requests
_pending_requests: Dict[str, asyncio.Future] = {}
_cache_lock = asyncio.Lock()


def _parse_hp_call_time(raw):
    """
    Parse HotProspector's display call_time (e.g. "Apr 15, 2026 6:34 pm") into an
    ISO 8601 string so calls can be windowed by date preset. Returns None if it
    can't be parsed. strptime's %p is case-insensitive, so lowercase am/pm works.
    """
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    for fmt in ("%b %d, %Y %I:%M %p", "%b %d, %Y %I:%M:%S %p", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            continue
    return None


class HotProspectorIntegration:
    def __init__(self, api_uid, api_key):
        self.api_uid = api_uid
        self.api_key = api_key
        self.base_url = HOTPROSPECTOR_CONFIG["base_url"]
        self._semaphore = asyncio.Semaphore(HOTPROSPECTOR_CONFIG["max_concurrent_requests"])

    async def _make_request(self, payload: dict, use_dedup: bool = True):
        """Make a request to Hot Prospector API with deduplication"""
        cache_key = json.dumps(payload, sort_keys=True)

        if use_dedup:
            async with _cache_lock:
                if cache_key in _pending_requests:
                    logger.info(f"Returning cached pending request for {payload.get('Method')}")
                    return await _pending_requests[cache_key]

                future = asyncio.Future()
                _pending_requests[cache_key] = future

        try:
            async with self._semaphore:
                async with httpx.AsyncClient(timeout=HOTPROSPECTOR_CONFIG["request_timeout"]) as client:
                    try:
                        response = await client.post(
                            self.base_url,
                            json=payload,
                            headers={"Content-Type": "application/json"}
                        )

                        if response.status_code != 200:
                            logger.error(f"Hot Prospector API error: {response.status_code} {response.text}")
                            result = (False, {
                                "error": f"API request failed with status {response.status_code}",
                                "status_code": response.status_code
                            })
                            if use_dedup:
                                future.set_result(result)
                            return result

                        data = response.json()

                        # Check for API-level errors
                        if isinstance(data, list) and len(data) > 0:
                            if data[0].get("response") == "false":
                                result = (False, {
                                    "error": data[0].get("message", "Unknown API error"),
                                    "status_code": 400
                                })
                                if use_dedup:
                                    future.set_result(result)
                                return result
                        elif isinstance(data, dict):
                            if data.get("response") == "false":
                                result = (False, {
                                    "error": data.get("message", "Unknown API error"),
                                    "status_code": 400
                                })
                                if use_dedup:
                                    future.set_result(result)
                                return result

                        result = (True, data)
                        if use_dedup:
                            future.set_result(result)
                        return result

                    except httpx.RequestError as e:
                        logger.error(f"Network error during Hot Prospector request: {str(e)}")
                        result = (False, {"error": f"Network error: {str(e)}"})
                        if use_dedup:
                            future.set_exception(e)
                        return result
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error: {str(e)}")
                        result = (False, {"error": f"Invalid JSON response: {str(e)}"})
                        if use_dedup:
                            future.set_exception(e)
                        return result
        finally:
            if use_dedup:
                async with _cache_lock:
                    _pending_requests.pop(cache_key, None)

    async def search_leads_by_ghl_location(
            self,
            ghl_location_id: str,
            search_field: str = "",
            search_text: str = ""
    ):
        """
        Search leads using GHL locationId.

        Args:
            ghl_location_id: The GHL locationId (e.g., "QAxWdczFbNx9t9Dg3Lz5")
            search_field: Field to search (e.g., "email") - leave empty for all leads
            search_text: Text to search for - leave empty for all leads
        """
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "locationId": ghl_location_id,
            "searchField": search_field,
            "searchText": search_text,
            "Method": "SearchByUserInput"
        }

        success, result = await self._make_request(payload, use_dedup=False)
        if not success:
            return False, result

        # Extract leads from response
        if isinstance(result, list) and len(result) > 0:
            if result[0].get("response") == "true":
                leads = result[0].get("Results", [])
                logger.info(f"Fetched {len(leads)} leads from GHL location {ghl_location_id}")
                return True, leads

        return False, {"error": "Unexpected response format or no results"}

    async def fetch_all_leads_from_ghl_location(self, ghl_location_id: str):
        """
        OPTIMIZED: Fetch all leads from a GHL location with single API call.
        Uses empty search to retrieve all leads at once.

        This replaces the old character-by-character batching approach.
        """
        logger.info(f"Fetching all leads for GHL location {ghl_location_id}")

        # Fetch all leads with empty search parameters
        success, leads = await self.search_leads_by_ghl_location(
            ghl_location_id=ghl_location_id,
            search_field="",  # Empty = all fields
            search_text=""  # Empty = all leads
        )

        if not success:
            logger.error(f"Failed to fetch leads for location {ghl_location_id}: {leads}")
            return False, leads

        # Deduplicate by LeadId (just in case)
        unique_leads = {}
        for lead in leads:
            lead_id = lead.get("LeadId")
            if lead_id and lead_id not in unique_leads:
                unique_leads[lead_id] = lead

        deduplicated_leads = list(unique_leads.values())

        logger.info(f"✅ Fetched {len(deduplicated_leads)} unique leads from GHL location {ghl_location_id}")
        return True, deduplicated_leads

    async def _fetch_leads_for_char(self, ghl_location_id: str, char: str) -> Tuple[bool, List]:
        """Helper method to fetch leads for a single character"""
        try:
            success, leads = await self.search_leads_by_ghl_location(
                ghl_location_id=ghl_location_id,
                search_field="email",
                search_text=char
            )
            return success, leads if success else []
        except Exception as e:
            logger.error(f"Error fetching leads for char '{char}': {e}")
            return False, []

    async def get_member_users(self):
        """Get all member users (team members)"""
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "Method": "getMemberUsers"
        }

        success, result = await self._make_request(payload)
        if not success:
            return False, result

        # Response may be a dict or a single-element list wrapping the dict.
        body = result[0] if isinstance(result, list) and result else result
        if isinstance(body, dict):
            # Per HP docs the members live under "data"; older shapes used "message".
            members = body.get("data")
            if members is None:
                members = body.get("message", [])
            if not isinstance(members, list):
                members = []
            logger.info(f"Fetched {len(members)} team members from Hot Prospector")
            return True, members

        return False, {"error": "Unexpected response format"}

    async def fetch_lead_call_logs(self, lead_id: str):
        """
        Fetch call logs for a specific lead.

        Args:
            lead_id: The LeadId from HotProspector

        Returns:
            Tuple of (success: bool, call_logs: list)
        """
        payload = {
            "api_uId": self.api_uid,
            "api_key": self.api_key,
            "LeadId": str(lead_id),
            "Method": "FetchLeadCallLogs"
        }

        success, result = await self._make_request(payload, use_dedup=False)
        if not success:
            return False, result

        # Extract call logs from response
        if isinstance(result, list) and len(result) > 0:
            if result[0].get("response") == "true":
                call_logs = result[0].get("Results", [])
                logger.info(f"Fetched {len(call_logs)} call logs for lead {lead_id}")
                return True, call_logs

        return False, {"error": "No call logs found or unexpected response format"}

    async def fetch_user_call_logs(
            self,
            ghl_location_id: str,
            from_date: str = None,
            to_date: str = None,
            call_type: str = "",
            page_size: int = 500,
            max_records: int = 100000,
    ):
        """
        Bulk-fetch ALL call logs for a GHL location via FetchUserCallLog.

        One paginated endpoint (limit/offset) instead of one request per lead, so a
        location with thousands of leads costs a handful of calls rather than thousands.

        Args:
            ghl_location_id: GHL locationId to scope the logs to.
            from_date / to_date: optional Y-m-d filters (HP caps the range at 30 days).
            call_type: "inbound", "outbound", or "" for both.
            page_size: records per request (HP default is 100; we ask for more).
            max_records: hard safety cap so a misbehaving has_more can't loop forever.

        Returns:
            (True, {"call_logs": [...raw...], "inbound_count", "outbound_count",
                    "total_records"}) on success, else (False, {"error": ...}).
        """
        all_logs = []
        offset = 0
        inbound_count = 0
        outbound_count = 0
        total_records = 0

        while True:
            payload = {
                "api_uId": self.api_uid,
                "api_key": self.api_key,
                "Method": "FetchUserCallLog",
                "locationId": ghl_location_id,
                "type": call_type or "",
                "limit": page_size,
                "offset": offset,
                "sort_by": "call_time",
                "sort_order": "DESC",
            }
            if from_date:
                payload["from_date"] = from_date
            if to_date:
                payload["to_date"] = to_date

            success, result = await self._make_request(payload, use_dedup=False)
            if not success:
                # If we already pulled some pages, return what we have so far.
                if all_logs:
                    break
                return False, result

            # Response may be a dict or a single-element list wrapping the dict.
            body = result[0] if isinstance(result, list) and result else result
            if not isinstance(body, dict) or body.get("response") != "true":
                if all_logs:
                    break
                return False, {"error": "Unexpected FetchUserCallLog response format"}

            page = body.get("Results", []) or []
            all_logs.extend(page)

            # Aggregate counts come back on every page; keep the latest non-empty values.
            inbound_count = int(body.get("inbound_count") or inbound_count or 0)
            outbound_count = int(body.get("outbound_count") or outbound_count or 0)
            total_records = int(body.get("total_records") or total_records or 0)

            has_more = bool(body.get("has_more"))
            next_offset = body.get("next_offset")
            if not has_more or not page or len(all_logs) >= max_records:
                break
            offset = int(next_offset) if next_offset is not None else offset + page_size

        logger.info(
            f"FetchUserCallLog: {len(all_logs)} call logs for location {ghl_location_id} "
            f"(inbound={inbound_count}, outbound={outbound_count}, total={total_records})"
        )
        return True, {
            "call_logs": all_logs,
            "inbound_count": inbound_count,
            "outbound_count": outbound_count,
            "total_records": total_records or len(all_logs),
        }

    def normalize_call_log(self, call_log: dict):
        """
        Normalize a call log object to a standard format.

        Args:
            call_log: Raw call log data from HotProspector API
        """
        return {
            "lead_id": call_log.get("leadId"),
            "lead_name": call_log.get("lead_Name"),
            "location_name": call_log.get("location_name"),
            "from_number": call_log.get("from_number"),
            "to_number": call_log.get("to_number"),
            "recording_url": call_log.get("recording"),
            "recording_id": call_log.get("recordingId"),
            "group": call_log.get("group"),
            "caller_name": call_log.get("caller_name"),
            "call_time": call_log.get("call_time"),
            "duration": int(call_log.get("duration", 0)),
            "city": call_log.get("city", ""),
            "state": call_log.get("state", ""),
            "transfer": call_log.get("transfer") == "Yes",
            "call_status": call_log.get("call_status"),
            "speed_to_lead": int(call_log.get("speed_to_lead", 0))
        }

    def normalize_user_call_log(self, call_log: dict, location_name: str = None):
        """
        Normalize a FetchUserCallLog row to the same shape the Sales-Hub UI consumes
        (see normalize_call_log).

        FetchUserCallLog differs from FetchLeadCallLogs: the call DIRECTION lives in
        `call_type` (the raw `call_status` is the outcome, e.g. "completed"), and there
        is no per-row location name. The frontend reads `call_status` as the direction,
        so we map it from `call_type` and stamp the GHL location name we already know.
        """
        return {
            "lead_id": call_log.get("leadId"),
            "lead_name": call_log.get("lead_Name"),
            "location_name": location_name or "",
            "from_number": call_log.get("from_number"),
            "to_number": call_log.get("to_number"),
            "recording_url": call_log.get("recording") or None,
            "recording_id": call_log.get("recordingId"),
            "group": call_log.get("group"),
            "caller_name": call_log.get("caller_name"),
            "call_time": call_log.get("call_time"),
            "call_time_iso": _parse_hp_call_time(call_log.get("call_time")),
            "duration": int(call_log.get("duration") or 0),
            "city": call_log.get("city", ""),
            "state": call_log.get("state", ""),
            "transfer": call_log.get("transfer") == "Yes",
            "call_status": (call_log.get("call_type") or "").lower(),
            "speed_to_lead": int(call_log.get("speed_to_lead") or 0),
        }

    def normalize_lead(self, lead: dict, ghl_location_id: str, ghl_location_name: str = None,
                       client_group_name: str = None):
        """
        Normalize a lead object to a standard format.

        Args:
            lead: Raw lead data from HotProspector API
            ghl_location_id: The GHL location ID
            ghl_location_name: The GHL location name (e.g., "Acme Corp - Main")
            client_group_name: The client group name from client_groups collection (e.g., "Acme Corporation")
        """
        custom_fields = lead.get("Lead_Custom_Fields") or {}

        # Parse GroupId as it might contain multiple IDs
        group_ids = []
        if lead.get("GroupId"):
            group_ids = [gid.strip() for gid in str(lead.get("GroupId")).split(",") if gid.strip()]

        # Parse tags
        tags = []
        if lead.get("Tags"):
            tags = [tag.strip() for tag in str(lead.get("Tags")).split(",") if tag.strip()]

        return {
            "id": lead.get("LeadId"),
            "ghl_location_id": ghl_location_id,
            "ghl_location_name": ghl_location_name or "Unknown Location",
            "client_name": client_group_name if client_group_name else "No Client Group",
            "group_ids": group_ids,
            "tags": tags,
            "first_name": lead.get("Firstname", ""),
            "last_name": lead.get("Lastname", ""),
            "email": lead.get("E-Mail", ""),
            "phone": lead.get("Phone", ""),
            "mobile": lead.get("Mobile", ""),
            "country_code": lead.get("CountryCode", ""),
            "zipcode": lead.get("Zipcode", ""),
            "city": lead.get("City", ""),
            "state": lead.get("State", ""),
            "address": lead.get("Address", ""),
            "company": lead.get("Company", ""),
            "website": lead.get("Website", ""),
            "custom_fields": custom_fields,
            "source": "HotProspector",
            "call_logs_count": 0,
            "call_logs": []
        }

    async def fetch_call_logs_for_leads_batch(self, lead_ids: List[str]):
        """
        Fetch call logs for multiple leads in parallel.

        Args:
            lead_ids: List of LeadId strings

        Returns:
            Dict mapping lead_id -> list of call logs
        """
        logger.info(f"Fetching call logs for {len(lead_ids)} leads")

        # Batch the requests to avoid overwhelming the API
        batch_size = HOTPROSPECTOR_CONFIG["max_concurrent_requests"]
        all_call_logs = {}

        for i in range(0, len(lead_ids), batch_size):
            batch = lead_ids[i:i + batch_size]
            batch_num = (i // batch_size) + 1
            total_batches = (len(lead_ids) + batch_size - 1) // batch_size

            logger.info(f"Processing call logs batch {batch_num}/{total_batches}")

            tasks = [self.fetch_lead_call_logs(lead_id) for lead_id in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for lead_id, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.warning(f"Error fetching call logs for lead {lead_id}: {result}")
                    all_call_logs[lead_id] = []
                    continue

                success, call_logs = result
                if success:
                    normalized_logs = [self.normalize_call_log(log) for log in call_logs]
                    all_call_logs[lead_id] = normalized_logs
                else:
                    all_call_logs[lead_id] = []

        logger.info(f"✅ Fetched call logs for {len(all_call_logs)} leads")
        return all_call_logs


# ============================================
# UPDATED MongoDB Storage Functions
# Now supports call logs in cache
# ============================================

async def save_hotprospector_leads_to_collection(
        user_id: str,
        ghl_location_id: str,
        leads: list,
        mongo_client: AsyncIOMotorClient
):
    """
    Save Hot Prospector leads WITH CALL LOGS for a specific GHL location.

    KEY CHANGE: Now saves call_logs and call_logs_count in the cache
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        leads_collection = db["hotprospector_leads"]

        # Delete existing leads for this user and GHL location
        delete_result = await leads_collection.delete_many({
            "user_id": user_id,
            "ghl_location_id": ghl_location_id
        })
        logger.info(f"Deleted {delete_result.deleted_count} existing leads for GHL location {ghl_location_id}")

        if not leads:
            logger.warning(f"No leads to save for GHL location {ghl_location_id}")
            return

        batch_size = HOTPROSPECTOR_CONFIG["batch_size"]
        total_batches = (len(leads) + batch_size - 1) // batch_size

        logger.info(f"Saving {len(leads)} leads with call logs in {total_batches} batches of {batch_size}")

        total_calls = 0
        for i in range(0, len(leads), batch_size):
            batch = leads[i:i + batch_size]
            batch_num = (i // batch_size) + 1

            lead_docs = []
            for lead in batch:
                # Count calls in this batch
                total_calls += lead.get("call_logs_count", 0)

                lead_doc = {
                    "user_id": user_id,
                    "ghl_location_id": ghl_location_id,
                    "lead_data": lead,
                    "match_keys": compute_match_keys(lead.get("email"), lead.get("phone")),
                    "created_at": datetime.now(),
                    "updated_at": datetime.now()
                }
                lead_docs.append(lead_doc)

            await leads_collection.insert_many(lead_docs, ordered=False)
            logger.info(f"Inserted batch {batch_num}/{total_batches} ({len(lead_docs)} leads)")

        # Update user document with metadata
        users_collection = db["users"]
        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    f"integrations.hotprospector.ghl_locations.{ghl_location_id}": {
                        "total_leads": len(leads),
                        "total_calls": total_calls,  # NEW: Track total calls
                        "last_fetched": datetime.now()
                    },
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )

        logger.info(
            f"✅ Saved {len(leads)} leads with {total_calls} call logs "
            f"for GHL location {ghl_location_id} for user: {user_id}"
        )

    except Exception as e:
        logger.error(f"Failed to save Hot Prospector leads for user {user_id}: {str(e)}")
        raise


async def get_hotprospector_leads_from_collection(
        user_id: str,
        ghl_location_id: str = None,
        mongo_client: AsyncIOMotorClient = None,
        skip: int = 0,
        limit: Optional[int] = None
):
    """
    Retrieve Hot Prospector leads WITH CALL LOGS from cache.

    KEY CHANGE: Call logs are now included in cached lead_data
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        leads_collection = db["hotprospector_leads"]

        # Build query
        query = {"user_id": user_id}
        if ghl_location_id:
            query["ghl_location_id"] = ghl_location_id

        # Check cache validity for specific location
        if ghl_location_id:
            users_collection = db["users"]
            user_doc = await users_collection.find_one(
                {"user_id": user_id},
                projection={
                    f"integrations.hotprospector.ghl_locations.{ghl_location_id}": 1
                }
            )

            if user_doc:
                location_meta = (user_doc.get("integrations", {})
                                 .get("hotprospector", {})
                                 .get("ghl_locations", {})
                                 .get(ghl_location_id, {}))

                last_fetched = location_meta.get("last_fetched")
                total_count = location_meta.get("total_leads", 0)
                # total_calls = location_meta.get("total_calls", 0)

                if last_fetched:
                    cache_age = (datetime.now() - last_fetched).total_seconds()
                    cache_valid = cache_age < HOTPROSPECTOR_CONFIG["leads_cache_duration"]

                    if not cache_valid:
                        logger.info(f"Cache expired for {ghl_location_id} (age: {cache_age:.0f}s)")
                        return None, 0

                    logger.info(
                        f"Cache valid for {ghl_location_id}: {total_count} leads, "
                        # f"{total_calls} calls (age: {cache_age:.0f}s)"
                    )

        # Fetch leads with call logs
        cursor = leads_collection.find(
            query,
            projection={"lead_data": 1, "_id": 0}
        ).skip(skip)

        if limit is not None and limit > 0:
            cursor = cursor.limit(limit)

        leads = []
        # total_calls = 0
        async for doc in cursor:
            lead_data = doc["lead_data"]
            # Call logs are already in lead_data!
            leads.append(lead_data)
            # total_calls += lead_data.get("call_logs_count", 0)

        # Get total count
        total_count = await leads_collection.count_documents(query)

        logger.info(
            # f"✅ Returned {len(leads)} cached leads with {total_calls} call logs "
            f"(total: {total_count})"
        )
        return leads, total_count

    except Exception as e:
        logger.error(f"Failed to retrieve Hot Prospector leads for user {user_id}: {str(e)}")
        return None, 0


async def save_hotprospector_credentials(
        user_id: str,
        api_uid: str,
        api_key: str,
        mongo_client: AsyncIOMotorClient
):
    """Save Hot Prospector API credentials to MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]

        credentials_doc = {
            "api_uid": api_uid,
            "api_key": api_key,
            "connected": True,
            "created_at": datetime.now(),
            "updated_at": datetime.now()
        }

        await users_collection.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "integrations.hotprospector.credentials": credentials_doc,
                    "updated_at": datetime.now()
                }
            },
            upsert=True
        )

        logger.info(f"Saved Hot Prospector credentials for user: {user_id}")

    except Exception as e:
        logger.error(f"Failed to save Hot Prospector credentials for user {user_id}: {str(e)}")
        raise


async def get_hotprospector_credentials(user_id: str, mongo_client: AsyncIOMotorClient):
    """Retrieve Hot Prospector credentials from MongoDB"""
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        users_collection = db["users"]

        user_doc = await users_collection.find_one(
            {"user_id": user_id},
            projection={"integrations.hotprospector.credentials": 1}
        )

        if (user_doc and
                user_doc.get("integrations", {})
                        .get("hotprospector", {})
                        .get("credentials")):
            return user_doc["integrations"]["hotprospector"]["credentials"]

        return None

    except Exception as e:
        logger.error(f"Failed to retrieve Hot Prospector credentials for user {user_id}: {str(e)}")
        return None


async def get_client_group_mapping(user_id: str, mongo_client: AsyncIOMotorClient) -> Dict[str, str]:
    """
    Get a mapping of GHL location IDs to client group names.
    If multiple client groups share the same GHL location, concatenate their names.

    Returns:
        Dict mapping ghl_location_id -> "Group1, Group2, Group3"
    """
    try:
        db = mongo_client[os.getenv("MONGODB_DB", "birdyaidev")]
        client_groups_collection = db["client_groups"]

        # Find all client groups for this user that have a ghl_location_id
        client_groups = await client_groups_collection.find({
            "user_id": user_id,
            "ghl_location_id": {"$exists": True, "$ne": None}
        }).to_list(None)

        logger.info(f"Found {len(client_groups)} client groups for user {user_id}")

        # Create mapping: ghl_location_id -> list of client_group_names
        location_to_groups = {}
        for group in client_groups:
            ghl_location_id = group.get("ghl_location_id")
            client_name = group.get("name")  # This is the "Birdy Ai" field

            if ghl_location_id and client_name:
                if ghl_location_id not in location_to_groups:
                    location_to_groups[ghl_location_id] = []
                location_to_groups[ghl_location_id].append(client_name)
                logger.info(f"  Added mapping: {ghl_location_id} -> {client_name}")

        # Convert lists to comma-separated strings
        mapping = {}
        for location_id, group_names in location_to_groups.items():
            # Sort for consistent ordering
            group_names.sort()
            mapping[location_id] = ", ".join(group_names)
            logger.info(f"  Final mapping: {location_id} -> {mapping[location_id]}")

        logger.info(f"Created client group mapping for user {user_id}: {len(mapping)} unique locations mapped")
        logger.debug(f"Full mapping: {mapping}")
        return mapping

    except Exception as e:
        logger.error(f"Error getting client group mapping for user {user_id}: {str(e)}")
        return {}