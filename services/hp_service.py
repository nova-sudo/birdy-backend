"""
services/hp_service.py
----------------------
HotProspector helper / service functions extracted from main.py.
"""

import logging
from datetime import datetime

from core.database import DB_NAME
from integrations.gohighlevel import get_subaccount_tokens
from integrations.hotprospector import (
    HotProspectorIntegration,
    get_hotprospector_credentials,
    save_hotprospector_leads_to_collection,
    get_hotprospector_leads_from_collection,
)

logger = logging.getLogger(__name__)


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
