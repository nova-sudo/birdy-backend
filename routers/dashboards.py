"""
routers/dashboards.py
---------------------
Persistence for dashkit dashboard documents — the "My dashboards" feature.

One document per (user, dashboard id), stored whole under `doc` and validated
against dashkit's JSON Schema on every save, so the database never holds a
dashboard the renderer cannot open. Every query is scoped by user: another
account's dashboard is a 404, not a 403.

Stored shape: {_id, id, user_id, doc, created_at, updated_at}.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Response

from core.database import DB_NAME
from core.models import BriefingBody, QueryBody, ResolveBody
from dependencies import get_mongo_client, get_current_user
from services.dashboards_briefing import generate_briefing
from services.dashboards_catalog import build_catalog
from services.dashboards_query import run_queries
from services.dashboards_resolve import MAX_BINDINGS, resolve_bindings
from services.dashboards_schema import schema_has_meta_updated_at, validate_doc

logger = logging.getLogger(__name__)

router = APIRouter()

_COLLECTION = "dashboards"

MAX_DASHBOARDS_RETURNED = 500


def _iso(dt: datetime) -> str:
    """UTC ISO 8601 with millisecond precision and a `Z`, matching dashkit's `meta` timestamps."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.get("/api/dashboards")
async def list_dashboards(current_user: str = Depends(get_current_user)):
    """The caller's dashboards, most recently updated first."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            rows = await db[_COLLECTION].find(
                {"user_id": current_user},
                {"_id": 0, "id": 1, "doc.title": 1, "updated_at": 1},
            ).sort([("updated_at", -1), ("_id", -1)]).to_list(length=MAX_DASHBOARDS_RETURNED)

            return {"items": [
                {
                    "id": r["id"],
                    "title": (r.get("doc") or {}).get("title", ""),
                    "updatedAt": _iso(r["updated_at"]),
                }
                for r in rows
            ]}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error listing dashboards for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load dashboards")


@router.get("/api/dashboards/catalog")
async def get_catalog(current_user: str = Depends(get_current_user)):
    """What a dashboard can bind to: metrics, dimensions and time windows."""
    return build_catalog()


@router.post("/api/dashboards/resolve")
async def resolve(body: ResolveBody, current_user: str = Depends(get_current_user)):
    """Live data for a dashboard's widgets: one slot per binding, null where a binding failed (see `errors`)."""
    if len(body.bindings) > MAX_BINDINGS:
        raise HTTPException(status_code=422, detail={
            "code": "TOO_MANY_BINDINGS",
            "message": f"At most {MAX_BINDINGS} bindings per request (got {len(body.bindings)})",
        })
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            return await resolve_bindings(db, current_user, body.bindings, body.shapes)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error resolving dashboard bindings for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to resolve dashboard data")


@router.post("/api/dashboards/query")
async def query(body: QueryBody, current_user: str = Depends(get_current_user)):
    """dashkit Frames for a board's widgets: one per query, in order, null where a query failed on its own."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            return {"frames": await run_queries(db, current_user, body.queries)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error running dashboard queries for {current_user}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load dashboard data")


@router.post("/api/dashboards/{dashboard_id}/briefing")
async def briefing(dashboard_id: str, body: BriefingBody = Body(default_factory=BriefingBody),
                   current_user: str = Depends(get_current_user)):
    """A short AI briefing of the dashboard's live numbers, every figure checked against the data."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            return await generate_briefing(db, current_user, dashboard_id, window=body.window,
                                           filters=body.filters, focus=body.focus, refresh=body.refresh)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error writing briefing for dashboard {dashboard_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to write briefing")


# --- fixed-path routes (/catalog, /resolve, /query) go above this line; /{id} must stay last ---


@router.get("/api/dashboards/{dashboard_id}")
async def get_dashboard(dashboard_id: str, current_user: str = Depends(get_current_user)):
    """One dashboard document."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            row = await db[_COLLECTION].find_one(
                {"id": dashboard_id, "user_id": current_user}, {"_id": 0, "doc": 1}
            )
            if not row:
                raise HTTPException(status_code=404, detail="Dashboard not found")
            return row["doc"]
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error loading dashboard {dashboard_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to load dashboard")


@router.put("/api/dashboards/{dashboard_id}")
async def put_dashboard(
    dashboard_id: str,
    doc: dict = Body(...),
    current_user: str = Depends(get_current_user),
):
    """Create or replace a dashboard. Returns the stored document."""
    errors = validate_doc(doc)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    if doc.get("id") != dashboard_id:
        raise HTTPException(status_code=400, detail="Dashboard id in the body must match the URL")

    now = datetime.now(timezone.utc)
    if schema_has_meta_updated_at():
        doc["meta"]["updatedAt"] = _iso(now)

    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            await db[_COLLECTION].update_one(
                {"id": dashboard_id, "user_id": current_user},
                {
                    "$set": {"doc": doc, "updated_at": now},
                    "$setOnInsert": {"id": dashboard_id, "user_id": current_user, "created_at": now},
                },
                upsert=True,
            )
            logger.info(f"Dashboard {dashboard_id} saved by {current_user}")
            return doc
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error saving dashboard {dashboard_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to save dashboard")


@router.delete("/api/dashboards/{dashboard_id}", status_code=204)
async def delete_dashboard(dashboard_id: str, current_user: str = Depends(get_current_user)):
    """Remove a dashboard."""
    async with get_mongo_client() as mongo_client:
        try:
            db = mongo_client[DB_NAME]
            result = await db[_COLLECTION].delete_one({"id": dashboard_id, "user_id": current_user})
            if result.deleted_count == 0:
                raise HTTPException(status_code=404, detail="Dashboard not found")
            return Response(status_code=204)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error deleting dashboard {dashboard_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to delete dashboard")


async def create_dashboards_indexes(mongo_client) -> None:
    """One document per (user, dashboard id); every lookup is by that pair."""
    db = mongo_client[DB_NAME]
    await db[_COLLECTION].create_index(
        [("user_id", 1), ("id", 1)],
        name="uniq_dashboards_user_id", unique=True, background=True,
    )
