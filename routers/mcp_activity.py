"""
routers/mcp_activity.py
-----------------------
Serves the MCP activity log (services/mcp_activity.py) to the Settings page: a filtered, cursor-paged list,
the apps seen in the log, one full redacted entry, and a CSV export. Everything is scoped to the signed-in user.
"""

import base64
import csv
import io
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from core.database import get_db
from dependencies import get_mongo_client, get_current_user
from services.mcp_activity import COLLECTION

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE_MAX = 50
_CSV_MAX_ROWS = 10_000
_DEFAULT_DAYS = 7
_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _naive_utc(dt: datetime) -> datetime:
    """Mongo hands datetimes back naive (UTC); queries use the same form so comparisons never mix kinds."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _iso(dt: datetime | None) -> str | None:
    return _naive_utc(dt).isoformat() + "Z" if isinstance(dt, datetime) else None


def _parse_day(value: str, name: str) -> datetime:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d") if len(value) >= 10 else datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"`{name}` must be an ISO date (YYYY-MM-DD)")


def _encode_cursor(row: dict) -> str:
    raw = f"{_iso(row['created_at'])}|{row['id']}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        stamp, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(stamp.rstrip("Z")), row_id
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor")


def _filter(user_id: str, app, kind, client_group_id, date_from, date_to, confirmed_after) -> dict:
    query: dict = {"user_id": user_id}
    if app:
        query["grant_id"] = app
    if kind:
        query["kind"] = kind
    if client_group_id:
        query["client_group_ids"] = client_group_id
    if confirmed_after:
        query["confirmed_after"] = confirmed_after
    created: dict = {}
    if not date_from and not date_to:
        created["$gte"] = _naive_utc(datetime.now(timezone.utc)) - timedelta(days=_DEFAULT_DAYS)
    if date_from:
        created["$gte"] = _parse_day(date_from, "from")
    if date_to:
        created["$lt"] = _parse_day(date_to, "to") + timedelta(days=1)  # inclusive through the end of that day
    if created:
        query["created_at"] = created
    return query


async def _revoked_by_grant(db, user_id: str, grant_ids) -> dict[str, bool]:
    """grant_id -> revoked, for every distinct id at once. A connection that no longer exists counts as revoked."""
    ids = sorted({g for g in grant_ids if g})
    if not ids:
        return {}
    live = set()
    async for g in db["oauth_grants"].find({"id": {"$in": ids}, "user_id": user_id, "revoked": {"$ne": True}}, {"id": 1}):
        live.add(g["id"])
    async for t in db["mcp_tokens"].find(
        {"id": {"$in": ids}, "user_id": user_id, "kind": {"$ne": "oauth"}, "revoked": {"$ne": True}}, {"id": 1}
    ):
        live.add(t["id"])
    return {g: g not in live for g in ids}


def _clients(row: dict) -> list[dict]:
    ids, names = row.get("client_group_ids") or [], row.get("client_names") or []
    return [{"id": cid, "name": names[i] if i < len(names) else None} for i, cid in enumerate(ids)]


def _list_item(row: dict, revoked: dict[str, bool]) -> dict:
    return {
        "id": row["id"],
        "created_at": _iso(row["created_at"]),
        "app_name": row.get("app_name"),
        "app_kind": row.get("app_kind"),
        "kind": row.get("kind"),
        "summary": row.get("summary"),
        "clients": _clients(row),
        "credits": {"total": (row.get("credits") or {}).get("total", 0)},
        "result_status": row.get("result_status"),
        "revoked": bool(revoked.get(row.get("grant_id"), False)),
    }


async def _totals(db, query: dict) -> dict:
    # A rate-limit row stands for `count` refused requests; every other row is one request.
    rows = await db[COLLECTION].aggregate([
        {"$match": query},
        {"$group": {"_id": None, "requests": {"$sum": {"$ifNull": ["$count", 1]}},
                    "credits": {"$sum": {"$ifNull": ["$credits.total", 0]}}}},
    ]).to_list(1)
    if not rows:
        return {"requests": 0, "credits": 0.0}
    return {"requests": int(rows[0]["requests"]), "credits": round(float(rows[0]["credits"]), 2)}


@router.get("/api/mcp/activity")
async def list_activity(
    app: str | None = None,
    kind: str | None = None,
    client_group_id: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    confirmed_after: str | None = None,
    cursor: str | None = None,
    limit: int = Query(_PAGE_MAX, ge=1, le=_PAGE_MAX),
    current_user: str = Depends(get_current_user),
):
    query = _filter(current_user, app, kind, client_group_id, from_, to, confirmed_after)
    page_query = dict(query)
    if cursor:
        at, row_id = _decode_cursor(cursor)
        page_query = {"$and": [query, {"$or": [
            {"created_at": {"$lt": at}},
            {"created_at": at, "id": {"$lt": row_id}},
        ]}]}
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        rows = await db[COLLECTION].find(page_query).sort([("created_at", -1), ("id", -1)]).to_list(limit + 1)
        has_more = len(rows) > limit
        rows = rows[:limit]
        revoked = await _revoked_by_grant(db, current_user, (r.get("grant_id") for r in rows))
        totals = None if cursor else await _totals(db, query)
    return {
        "items": [_list_item(r, revoked) for r in rows],
        "next_cursor": _encode_cursor(rows[-1]) if has_more else None,
        "totals": totals,
    }


@router.get("/api/mcp/activity/apps")
async def list_activity_apps(current_user: str = Depends(get_current_user)):
    """The distinct apps in this user's log (newest name wins), whether or not they are still connected."""
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        rows = await db[COLLECTION].find(
            {"user_id": current_user, "grant_id": {"$ne": None}}, {"grant_id": 1, "app_name": 1, "app_kind": 1}
        ).sort("created_at", -1).to_list(None)
        apps: dict[str, dict] = {}
        for r in rows:
            apps.setdefault(r["grant_id"], {"grant_id": r["grant_id"], "app_name": r.get("app_name"),
                                            "app_kind": r.get("app_kind")})
        revoked = await _revoked_by_grant(db, current_user, apps.keys())
    return [{**a, "revoked": revoked.get(gid, False)} for gid, a in apps.items()]


def _cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_CHARS) else text  # keep spreadsheets from running a cell as a formula


@router.get("/api/mcp/activity.csv")
async def export_activity_csv(
    app: str | None = None,
    kind: str | None = None,
    client_group_id: str | None = None,
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    confirmed_after: str | None = None,
    current_user: str = Depends(get_current_user),
):
    query = _filter(current_user, app, kind, client_group_id, from_, to, confirmed_after)
    async with get_mongo_client() as mongo_client:
        rows = await get_db(mongo_client)[COLLECTION].find(
            query, {"created_at": 1, "app_name": 1, "kind": 1, "summary": 1, "client_names": 1,
                    "result_status": 1, "credits": 1}
        ).sort([("created_at", -1), ("id", -1)]).to_list(_CSV_MAX_ROWS)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["time", "app", "kind", "summary", "client", "result", "credits"])
    for r in rows:
        writer.writerow([
            _cell(_iso(r["created_at"])), _cell(r.get("app_name")), _cell(r.get("kind")), _cell(r.get("summary")),
            _cell("; ".join(n for n in (r.get("client_names") or []) if n)), _cell(r.get("result_status")),
            (r.get("credits") or {}).get("total", 0),
        ])
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="birdy-ai-activity.csv"'},
    )


@router.get("/api/mcp/activity/{activity_id}")
async def get_activity(activity_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = get_db(mongo_client)
        row = await db[COLLECTION].find_one({"id": activity_id, "user_id": current_user})
        if not row:
            raise HTTPException(status_code=404, detail="Activity not found")
        revoked = await _revoked_by_grant(db, current_user, [row.get("grant_id")])
    entry = {k: v for k, v in row.items() if k not in ("_id", "user_id", "minute", "client_group_ids", "client_names")}
    entry["created_at"] = _iso(row["created_at"])
    entry["clients"] = _clients(row)
    entry["revoked"] = bool(revoked.get(row.get("grant_id"), False))
    return entry
