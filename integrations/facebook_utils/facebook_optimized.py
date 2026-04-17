# integrations/facebook_optimized.py

import httpx
import asyncio
from typing import Dict, List, Optional
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
import os

from utils.cache_helpers import get_cached_data, save_cached_data

logger = logging.getLogger(__name__)


class MetaDataFetcher:
    """Handles parallel fetching of Meta data with intelligent caching"""

    def __init__(self, current_user: str, mongo_client: AsyncIOMotorClient, token: dict):
        self.current_user = current_user
        self.mongo_client = mongo_client
        self.token = token
        self.db = mongo_client[os.getenv("MONGODB_DB", "birdy")]

    async def fetch_account_data_parallel(self, account_id: str) -> Optional[dict]:
        """Fetch campaign/adset/ad data with 5min cache"""
        # Check cache first
        cache_path = f"facebook.accounts.{account_id}.account_data"
        cached = await get_cached_data(
            self.current_user,
            cache_path,
            self.mongo_client,
            ttl_seconds=300
        )

        if cached:
            logger.info(f"✅ Using cached data for account {account_id}")
            return cached

        # Fetch fresh data
        logger.info(f"🔄 Fetching fresh data for account {account_id}")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://graph.facebook.com/v25.0/{account_id}/campaigns",
                    params={
                        "fields": "name,insights{actions,spend,results,reach,impressions,clicks,cpc,ctr,cost_per_result},adsets{name,insights{actions,spend,results,impressions,clicks,cpc,ctr,cost_per_result}},ads{name,insights{actions,spend,results,impressions,clicks,cpc,ctr,cost_per_result}}",
                        "access_token": self.token["access_token"]
                    }
                )

                if response.status_code == 200:
                    data = response.json()
                    await save_cached_data(
                        self.current_user,
                        cache_path,
                        data,
                        self.mongo_client
                    )
                    return data
                else:
                    logger.error(f"Failed to fetch data for {account_id}: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching account data for {account_id}: {str(e)}")
            return None

    async def fetch_leads_parallel(self, account_id: str) -> List[dict]:
        """Fetch leads with 5min cache"""
        # Check cache
        cache_path = f"facebook.accounts.{account_id}.leads"
        cached = await get_cached_data(
            self.current_user,
            cache_path,
            self.mongo_client,
            ttl_seconds=300
        )

        if cached:
            logger.info(f"✅ Using cached leads for account {account_id}")
            return cached if isinstance(cached, list) else []

        # Fetch fresh
        logger.info(f"🔄 Fetching fresh leads for account {account_id}")
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://graph.facebook.com/v25.0/{account_id}/campaigns",
                    params={
                        "fields": "ads{leads{id,ad_name,field_data,,platform,created_time}}",
                        "access_token": self.token["access_token"]
                    }
                )

                if response.status_code == 200:
                    data = response.json()
                    leads = []

                    for campaign in data.get("data", []):
                        for ad in campaign.get("ads", {}).get("data", []):
                            for lead in ad.get("leads", {}).get("data", []):
                                field_data = {
                                    field["name"]: field["values"][0]
                                    for field in lead.get("field_data", [])
                                    if field.get("values")
                                }
                                leads.append({
                                    "id": lead.get("id"),
                                    "ad_name": lead.get("ad_name", ""),
                                    "campaign_name": lead.get("campaign_name", ""),
                                    "platform": lead.get("platform", ""),
                                    "created_time": lead.get("created_time", ""),
                                    "full_name": field_data.get("full_name", ""),
                                    "email": field_data.get("email", ""),
                                    "phone_number": field_data.get("phone_number", "")
                                })

                    await save_cached_data(
                        self.current_user,
                        cache_path,
                        leads,
                        self.mongo_client
                    )
                    return leads
                else:
                    return []
        except Exception as e:
            logger.error(f"Error fetching leads for {account_id}: {str(e)}")
            return []


async def fetch_all_accounts_parallel(
        current_user: str,
        mongo_client: AsyncIOMotorClient,
        token: dict,
        account_ids: List[str]
) -> Dict[str, dict]:
    """Fetch data for multiple ad accounts in parallel"""
    fetcher = MetaDataFetcher(current_user, mongo_client, token)

    # Create tasks for all accounts
    tasks = [
        fetcher.fetch_account_data_parallel(account_id)
        for account_id in account_ids
    ]

    # Execute in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Map results
    account_data_map = {}
    for account_id, result in zip(account_ids, results):
        if not isinstance(result, Exception) and result:
            account_data_map[account_id] = result

    return account_data_map