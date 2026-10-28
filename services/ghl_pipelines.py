"""Cached GHL pipeline stage lookup (stageId -> stage/pipeline names)."""
import logging
from datetime import datetime, timedelta, timezone

from integrations.gohighlevel import ghl_integration, get_subaccount_tokens

logger = logging.getLogger(__name__)

CACHE_TTL = timedelta(hours=6)
COLLECTION = "ghl_pipelines"


def _build_lookup(pipelines: list) -> dict:
    lookup: dict = {}
    for pipeline in pipelines or []:
        for stage in pipeline.get("stages") or []:
            if stage.get("id"):
                lookup[stage["id"]] = {
                    "stage_name": stage.get("name"),
                    "pipeline_id": pipeline.get("id"),
                    "pipeline_name": pipeline.get("name"),
                }
    return lookup


def _is_fresh(fetched_at) -> bool:
    if not isinstance(fetched_at, datetime):
        return False
    if fetched_at.tzinfo is None:  # mongomock/Mongo return naive UTC
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched_at < CACHE_TTL


async def stage_lookup(db, user_id: str, location_id: str) -> dict:
    """Map stageId -> {stage_name, pipeline_id, pipeline_name}. Never raises."""
    try:
        cached = await db[COLLECTION].find_one({"location_id": location_id, "user_id": user_id})
        if cached and _is_fresh(cached.get("fetched_at")):
            return _build_lookup(cached.get("pipelines"))

        stale = _build_lookup(cached.get("pipelines")) if cached else {}

        tokens = await get_subaccount_tokens(user_id, db.client)
        access_token = (tokens or {}).get(location_id, {}).get("access_token")
        if not access_token:
            return stale

        ok, pipelines = await ghl_integration.fetch_pipelines(location_id, access_token)
        if not ok:
            return stale

        await db[COLLECTION].update_one(
            {"location_id": location_id, "user_id": user_id},
            {"$set": {"pipelines": pipelines, "fetched_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
        return _build_lookup(pipelines)
    except Exception as e:
        logger.warning("stage_lookup failed for location %s: %s", location_id, e)
        return {}
