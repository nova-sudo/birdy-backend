"""
routers/creatives.py
--------------------
Creative tests API (Phase 1). Every route is owner-scoped: another user's test
is a 404, never a 403. Archiving never touches Meta ads.
"""

from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from core.database import DB_NAME
from dependencies import get_current_user, get_mongo_client
from integrations.facebook_utils.facebook import get_facebook_token
from integrations.facebook_utils.meta_creative_reader import (
    MetaReadError,
    adset_compatible,
    list_adsets,
    list_lead_forms,
    list_promote_pages,
)
from jobs.creative_tracking import track_test
from services.facebook_cache_shape import read_preset
from services.creatives import reconcile, store
from services.creatives.naming import meta_ad_name, slugify
from services.creatives.targets import owning_group, prefill_targets

router = APIRouter(prefix="/api/creatives", tags=["creatives"])

Mode = Literal["thresholds", "baseline", "cpl_only"]


class Targets(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: Optional[Mode] = None


class CreateBody(BaseModel):
    name: str
    origin: Literal["new", "revive"]
    creative: dict
    targets: Optional[Targets] = None
    source: Optional[dict] = None
    slug: Optional[str] = None
    on_verdict: Optional[dict] = None


class PatchBody(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    creative: Optional[dict] = None
    targets: Optional[Targets] = None
    on_verdict: Optional[dict] = None


class AdoptBody(BaseModel):
    ad_id: str


def _json(value):
    """Strip Mongo ids and make dates ISO strings."""
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


async def _owned(db, user_id: str, test_id: str) -> dict:
    test = await store.get_test(db, user_id, test_id)
    if test is None:
        raise HTTPException(404, "Creative test not found")
    return test


@router.get("")
async def list_creatives(
    status: Optional[str] = None,
    client_group_id: Optional[str] = None,
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        accounts = None
        if client_group_id:
            group = await db["client_groups"].find_one(
                {"id": client_group_id, "user_id": current_user}, {"meta_ad_account_id": 1}
            )
            account = (group or {}).get("meta_ad_account_id")
            accounts = [account] if account else []
        tests = await store.list_tests(db, current_user, status=status, ad_account_ids=accounts)
        items = []
        for t in tests:
            ads = await store.list_test_ads(db, t["id"], user_id=current_user)
            items.append(_json({**t, "ads": ads}))
        return {"items": items}


@router.get("/adoptable")
async def adoptable(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        return _json(await reconcile.find_adoptable(mongo_client[DB_NAME], current_user))


@router.get("/targets/prefill")
async def targets_prefill(
    client_group_ids: str = Query(""),
    current_user: str = Depends(get_current_user),
):
    ids = [i for i in client_group_ids.split(",") if i]
    async with get_mongo_client() as mongo_client:
        return _json(await prefill_targets(mongo_client[DB_NAME], current_user, ids))


async def _account_groups(db, user_id: str, act: str) -> list[dict]:
    groups = await db["client_groups"].find(
        {"user_id": user_id, "meta_ad_account_id": act}, {"_id": 0}
    ).to_list(length=None)
    if not groups:
        raise HTTPException(404, "Ad account not found")
    return groups


async def _meta_token(mongo_client, user_id: str) -> str:
    fb = await get_facebook_token(user_id, mongo_client)
    token = (fb or {}).get("access_token")
    if not token:
        raise HTTPException(400, "Meta is not connected")
    return token


def _meta_error(e: MetaReadError) -> HTTPException:
    return HTTPException(502, {"message": str(e), "code": e.code})


@router.get("/ad-accounts")
async def ad_accounts(current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        groups = await mongo_client[DB_NAME]["client_groups"].find(
            {"user_id": current_user, "meta_ad_account_id": {"$nin": [None, ""]}},
            {"_id": 0, "id": 1, "name": 1, "meta_ad_account_id": 1, "ad_account_currency": 1},
        ).to_list(length=None)
    rows: dict[str, dict] = {}
    for g in groups:
        row = rows.setdefault(g["meta_ad_account_id"], {
            "ad_account_id": g["meta_ad_account_id"], "name": None, "currency": None, "clients": []})
        row["clients"].append({"id": g["id"], "name": g.get("name")})
        row["currency"] = row["currency"] or g.get("ad_account_currency")
    for row in rows.values():
        row["name"] = ", ".join(c["name"] for c in row["clients"] if c["name"]) or row["ad_account_id"]
    return list(rows.values())


@router.get("/ad-accounts/{act}/adsets")
async def account_adsets(
    act: str,
    destination: Literal["lead_form", "website", "whatsapp"],
    current_user: str = Depends(get_current_user),
):
    async with get_mongo_client() as mongo_client:
        groups = await _account_groups(mongo_client[DB_NAME], current_user, act)
        token = await _meta_token(mongo_client, current_user)
    try:
        adsets = await list_adsets(act, token)
    except MetaReadError as e:
        raise _meta_error(e)
    out = []
    for a in adsets:
        ok, reason = adset_compatible(a, destination)
        owner = owning_group(groups, (a.get("campaign") or {}).get("id"))
        out.append({**a, "compatible": ok, "reason": reason, "owning_client_group_id": owner.get("id")})
    return out


@router.get("/ad-accounts/{act}/pages")
async def account_pages(act: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        await _account_groups(mongo_client[DB_NAME], current_user, act)
        token = await _meta_token(mongo_client, current_user)
    try:
        return await list_promote_pages(act, token)
    except MetaReadError as e:
        raise _meta_error(e)


@router.get("/pages/{page_id}/lead-forms")
async def page_lead_forms(page_id: str, current_user: str = Depends(get_current_user)):
    """Allowed only when the page is a promote page of one of the user's ad accounts."""
    async with get_mongo_client() as mongo_client:
        groups = await mongo_client[DB_NAME]["client_groups"].find(
            {"user_id": current_user, "meta_ad_account_id": {"$nin": [None, ""]}},
            {"_id": 0, "meta_ad_account_id": 1},
        ).to_list(length=None)
        token = await _meta_token(mongo_client, current_user)
    try:
        for act in sorted({g["meta_ad_account_id"] for g in groups}):
            if any(p.get("id") == page_id for p in await list_promote_pages(act, token)):
                return await list_lead_forms(page_id, token)
    except MetaReadError as e:
        raise _meta_error(e)
    raise HTTPException(404, "Page not found")


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _rank(name: str, q: str):
    """0 exact code, 1 name prefix, 2 substring, None no match (everything is 3 without q)."""
    if not q:
        return 3
    n = (name or "").lower()
    if n.split(" | ", 1)[0].strip() == q:
        return 0
    if n.startswith(q):
        return 1
    return 2 if q in n else None


@router.get("/ads/search")
async def search_ads(
    q: str = "",
    ad_account_id: Optional[str] = None,
    limit: int = Query(30, ge=1, le=100),
    current_user: str = Depends(get_current_user),
):
    query: dict = {"user_id": current_user, "meta_ad_account_id": {"$nin": [None, ""]}}
    if ad_account_id:
        query["meta_ad_account_id"] = ad_account_id
    async with get_mongo_client() as mongo_client:
        groups = await mongo_client[DB_NAME]["client_groups"].find(query, {"_id": 0}).to_list(length=None)
    needle = q.strip().lower()
    seen: set[str] = set()
    hits = []
    for g in groups:
        for ad in read_preset(g.get("facebook_cache") or {}, "last_30d").get("ads") or []:
            ad_id = str(ad.get("id"))
            tier = _rank(ad.get("name"), needle)
            if tier is None or ad_id in seen:
                continue
            seen.add(ad_id)
            spend = _num(ad.get("spend"))
            leads = _num(ad.get("total_leads") or ad.get("results"))
            hits.append((tier, -spend, {
                "ad_id": ad_id, "name": ad.get("name"), "ad_account_id": g["meta_ad_account_id"],
                "client_name": g.get("name"), "status": ad.get("status"),
                "thumbnail": ad.get("creative_thumbnail"), "image": ad.get("creative_image"),
                "video_id": ad.get("creative_video_id"), "title": ad.get("creative_title"),
                "body": ad.get("creative_body"),
                "metrics": {"spend": spend, "leads": leads, "cpl": spend / leads if leads else None},
            }))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [h[2] for h in hits[:limit]]


@router.post("")
async def create_creative(body: CreateBody, current_user: str = Depends(get_current_user)):
    targets = body.targets.model_dump(exclude_none=True) if body.targets else {}
    async with get_mongo_client() as mongo_client:
        test = await store.create_test(
            mongo_client[DB_NAME], current_user,
            name=body.name, origin=body.origin, creative=body.creative, targets=targets,
            source=body.source, slug=body.slug, on_verdict=body.on_verdict,
        )
        return _json(test)


@router.get("/{test_id}")
async def get_creative(test_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        test = await _owned(db, current_user, test_id)
        ads = await store.list_test_ads(db, test_id, user_id=current_user)
        days: dict[str, list] = {}
        ad_ids = [a["meta"]["ad_id"] for a in ads if (a.get("meta") or {}).get("ad_id")]
        if ad_ids:
            cursor = db[store.AD_DAYS].find(
                {"user_id": current_user, "ad_id": {"$in": ad_ids}}
            ).sort("date", 1)
            async for d in cursor:
                days.setdefault(d["ad_id"], []).append(d)
        return _json({**test, "ads": ads, "days": days})


@router.patch("/{test_id}")
async def patch_creative(
    test_id: str, body: PatchBody, current_user: str = Depends(get_current_user)
):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        test = await _owned(db, current_user, test_id)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        if "targets" in fields:
            fields["targets"] = {**(test.get("targets") or {}), **fields["targets"]}
        if "slug" in fields:
            if test.get("status") != store.DRAFT:
                raise HTTPException(409, "Slug can only be changed while the test is a draft")
            slug = slugify(fields["slug"]) or "untitled"
            fields["slug"] = slug
            fields["meta_ad_name"] = meta_ad_name(test["code"], slug)
        updated = await store.update_test(db, current_user, test_id, fields)
        return _json(updated)


@router.post("/{test_id}/archive")
async def archive_creative(test_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        await _owned(db, current_user, test_id)
        updated = await store.update_test(
            db, current_user, test_id,
            {"status": store.ARCHIVED, "archived_at": datetime.now(timezone.utc)},
        )
        return _json(updated)


@router.post("/{test_id}/refresh")
async def refresh_creative(test_id: str, current_user: str = Depends(get_current_user)):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        test = await _owned(db, current_user, test_id)
        result = await track_test(db, mongo_client, test, today=datetime.now(timezone.utc).date())
        return _json(result)


@router.post("/{test_id}/adopt")
async def adopt_creative(
    test_id: str, body: AdoptBody, current_user: str = Depends(get_current_user)
):
    async with get_mongo_client() as mongo_client:
        db = mongo_client[DB_NAME]
        test = await _owned(db, current_user, test_id)
        try:
            ad = await reconcile.adopt(db, current_user, test_id, body.ad_id)
        except ValueError as e:
            raise HTTPException(409, str(e))
        if test.get("status") == store.DRAFT:
            await store.update_test(
                db, current_user, test_id,
                {"status": store.TESTING, "started_at": datetime.now(timezone.utc)},
            )
        return _json(ad)
