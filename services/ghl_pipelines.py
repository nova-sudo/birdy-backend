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


async def _pipelines(db, user_id: str, location_id: str) -> list:
    """The location's raw pipelines, from the 6h cache or one GHL refresh. Never raises.

    A fresh cache hit costs one find_one. On a miss it refreshes from GHL and
    falls back to the stale copy (or []) when there is no token or the fetch fails.
    """
    try:
        cached = await db[COLLECTION].find_one({"location_id": location_id, "user_id": user_id})
        if cached and _is_fresh(cached.get("fetched_at")):
            return cached.get("pipelines") or []

        stale = (cached.get("pipelines") or []) if cached else []

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
        return pipelines or []
    except Exception as e:
        logger.warning("pipeline lookup failed for location %s: %s", location_id, e)
        return []


async def stage_lookup(db, user_id: str, location_id: str) -> dict:
    """Map stageId -> {stage_name, pipeline_id, pipeline_name}. Never raises."""
    return _build_lookup(await _pipelines(db, user_id, location_id))


def _stage_position(stage: dict):
    pos = stage.get("position")
    return pos if isinstance(pos, (int, float)) and not isinstance(pos, bool) else float("inf")


def _ordered(pipelines: list) -> list:
    out = []
    for pipeline in pipelines or []:
        if not isinstance(pipeline, dict) or not pipeline.get("id"):
            continue
        # GHL lists stages in board order; `position` is the authority when present.
        stages = sorted(
            (s for s in (pipeline.get("stages") or []) if isinstance(s, dict) and s.get("id")),
            key=_stage_position,
        )
        out.append({
            "id": pipeline["id"],
            "name": pipeline.get("name"),
            "stages": [{"id": s["id"], "name": s.get("name")} for s in stages],
        })
    return out


async def ordered_pipelines(db, user_id: str, location_id: str) -> list:
    """The location's pipelines in the CRM's own order, each with its stages in board order.

    [{"id", "name", "stages": [{"id", "name"}]}]. Same cache/refresh path as
    stage_lookup; [] when the pipelines cannot be read. Never raises.
    """
    return _ordered(await _pipelines(db, user_id, location_id))
