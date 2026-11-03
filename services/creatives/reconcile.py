"""Find and adopt hand-posted ads whose name starts with a creative test code."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from services.creatives.naming import parse_code
from services.creatives.store import ARCHIVED, TEST_ADS, TESTS, add_test_ads

logger = logging.getLogger(__name__)


async def _cached_ads(db, user_id: str):
    groups = await db["client_groups"].find(
        {"user_id": user_id},
        {"id": 1, "meta_ad_account_id": 1, "facebook_cache.entities.ads": 1,
         "facebook_cache.entities.adsets": 1},
    ).to_list(None)
    for g in groups:
        entities = (g.get("facebook_cache") or {}).get("entities") or {}
        for ad in entities.get("ads") or []:
            yield g, ad, entities


async def find_adoptable(db, user_id: str) -> list[dict]:
    tests = await db[TESTS].find({"user_id": user_id, "status": {"$ne": ARCHIVED}}).to_list(None)
    by_code = {t["code"]: t for t in tests}
    if not by_code:
        return []
    cached = [(g, ad) async for g, ad, _ in _cached_ads(db, user_id)
              if parse_code(ad.get("name")) in by_code and ad.get("id")]
    # Any Birdy user's tracking of the ad counts: ad accounts can be shared across users.
    tracked = set(await db[TEST_ADS].distinct(
        "meta.ad_id", {"meta.ad_id": {"$in": [ad["id"] for _, ad in cached]}}))
    out, seen = [], set()
    for g, ad in cached:
        code = parse_code(ad.get("name"))
        ad_id = ad.get("id")
        if code not in by_code or not ad_id or ad_id in tracked or ad_id in seen:
            continue
        seen.add(ad_id)
        out.append({
            "test_id": by_code[code]["id"], "code": code, "ad_id": ad_id, "ad_name": ad.get("name"),
            "ad_account_id": g.get("meta_ad_account_id"), "client_group_id": g["id"],
            "adset_id": ad.get("adset_id"), "campaign_id": ad.get("campaign_id"),
        })
    return out


async def adopt(db, user_id: str, test_id: str, ad_id: str) -> dict:
    test = await db[TESTS].find_one({"id": test_id, "user_id": user_id})
    if test is None:
        raise ValueError("test not found")
    for item in await find_adoptable(db, user_id):
        if item["ad_id"] == ad_id:
            if item["test_id"] != test_id:
                raise ValueError(f"ad {ad_id} is named for {item['code']}, not {test['code']}")
            break
    else:
        if await db[TEST_ADS].find_one({"meta.ad_id": ad_id}):
            raise ValueError(f"ad {ad_id} is already tracked")
        raise ValueError(f"ad {ad_id} is not an adoptable {test['code']} ad")
    rows = await add_test_ads(db, user_id, test_id, [{
        "client_group_id": item["client_group_id"], "ad_account_id": item["ad_account_id"],
        "adset_id": item["adset_id"], "campaign_id": item.get("campaign_id"), "meta": {"ad_id": ad_id},
        "post_state": "active", "origin": "adopted",
    }])
    return rows[0]


async def reconcile_all(db) -> dict:
    user_ids = await db[TESTS].distinct("user_id", {"status": {"$ne": ARCHIVED}})
    per_user: dict[str, int] = {}
    for uid in user_ids:
        try:
            n = len(await find_adoptable(db, uid))
        except Exception as e:
            logger.error(f"[creatives-reconcile] {uid} failed: {e}", exc_info=True)
            continue
        if n:
            per_user[uid] = n
    return {"users": len(user_ids), "adoptable": sum(per_user.values()), "by_user": per_user}
