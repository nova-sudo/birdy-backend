"""Mongo store for creative tests: tests, per-account ads, code counter, indexes."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, ReturnDocument

from core.database import DB_NAME
from services.creatives.naming import format_code, meta_ad_name, slugify

TESTS = "creative_tests"
TEST_ADS = "creative_test_ads"
AD_DAYS = "creative_ad_days"
COUNTERS = "creative_test_counters"
POST_JOBS = "creative_post_jobs"

DRAFT = "draft"
POSTING = "posting"
TESTING = "testing"
WINNER = "winner"
LOSER = "loser"
INCONCLUSIVE = "inconclusive"
ARCHIVED = "archived"
FINAL_VERDICTS = (WINNER, LOSER, INCONCLUSIVE)

DEFAULT_TARGETS: dict[str, Any] = {
    "mode": "thresholds",
    "metrics": {},
    "baseline_pct": 15,
    "min_spend": 150,
    "min_days": 7,
    "win_share": 0.6,
}

_PROTECTED_FIELDS = ("code", "user_id", "id", "_id")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    doc = dict(doc)
    if doc.get("_id") != doc.get("id"):
        doc.pop("_id", None)
    return doc


async def next_code(db, user_id: str) -> str:
    row = await db[COUNTERS].find_one_and_update(
        {"_id": user_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return format_code(row["seq"])


async def create_test(
    db,
    user_id: str,
    *,
    name: str,
    origin: str,
    creative: dict,
    targets: dict,
    source: dict | None = None,
    slug: str | None = None,
    created_via: str = "wizard",
    on_verdict: dict | None = None,
) -> dict:
    code = await next_code(db, user_id)
    final_slug = (slugify(slug) if slug else slugify(name)) or "untitled"
    now = _now()
    doc = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "name": name,
        "code": code,
        "slug": final_slug,
        "meta_ad_name": meta_ad_name(code, final_slug),
        "origin": origin,
        "source": source,
        "creative": creative,
        "targets": {**copy.deepcopy(DEFAULT_TARGETS), **(targets or {})},
        "status": DRAFT,
        "created_via": created_via,
        "on_verdict": on_verdict,
        "created_at": now,
        "updated_at": now,
    }
    await db[TESTS].insert_one(dict(doc))
    return _clean(doc)


async def get_test(db, user_id: str, test_id: str) -> dict | None:
    return _clean(await db[TESTS].find_one({"id": test_id, "user_id": user_id}))


async def get_test_by_code(db, user_id: str, code: str) -> dict | None:
    return _clean(await db[TESTS].find_one({"code": code.strip().upper(), "user_id": user_id}))


async def list_tests(
    db,
    user_id: str,
    *,
    status: str | None = None,
    ad_account_ids: list[str] | None = None,
) -> list[dict]:
    query: dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    else:
        query["status"] = {"$ne": ARCHIVED}
    if ad_account_ids is not None:
        test_ids = await db[TEST_ADS].distinct(
            "test_id", {"user_id": user_id, "ad_account_id": {"$in": ad_account_ids}}
        )
        query["id"] = {"$in": test_ids}
    cursor = db[TESTS].find(query).sort("created_at", DESCENDING)
    return [_clean(d) async for d in cursor]


async def update_test(db, user_id: str, test_id: str, fields: dict) -> dict | None:
    safe = {k: v for k, v in fields.items() if k not in _PROTECTED_FIELDS}
    safe["updated_at"] = _now()
    row = await db[TESTS].find_one_and_update(
        {"id": test_id, "user_id": user_id},
        {"$set": safe},
        return_document=ReturnDocument.AFTER,
    )
    return _clean(row)


async def add_test_ads(db, user_id: str, test_id: str, rows: list[dict]) -> list[dict]:
    now = _now()
    docs = []
    for row in rows:
        doc = {
            "post_state": "pending",
            "score": {"state": "scoring"},
            **row,
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "test_id": test_id,
            "created_at": now,
        }
        docs.append(doc)
    if docs:
        await db[TEST_ADS].insert_many([dict(d) for d in docs])
    return [_clean(d) for d in docs]


async def list_test_ads(db, test_id: str, *, user_id: str) -> list[dict]:
    query = {"test_id": test_id, "user_id": user_id}
    return [_clean(d) async for d in db[TEST_ADS].find(query).sort("created_at", ASCENDING)]


async def create_creative_indexes(mongo_client) -> None:
    """Idempotent index creation for the creatives collections."""
    db = mongo_client[DB_NAME]
    await db[TESTS].create_index([("user_id", 1), ("code", 1)], unique=True, name="user_code_unique")
    await db[TESTS].create_index([("user_id", 1), ("status", 1)], name="user_status")
    await db[TEST_ADS].create_index([("test_id", 1)], name="test_id")
    await db[TEST_ADS].create_index([("user_id", 1), ("ad_account_id", 1)], name="user_account")
    # Partial (string-typed) rather than plain sparse: rows with meta.ad_id=None
    # must not collide with each other.
    await db[TEST_ADS].create_index(
        [("meta.ad_id", 1)],
        unique=True,
        partialFilterExpression={"meta.ad_id": {"$type": "string"}},
        name="meta_ad_id_unique",
    )
    await db[AD_DAYS].create_index(
        [("user_id", 1), ("ad_id", 1), ("date", 1)], unique=True, name="user_ad_date_unique"
    )
    await db[POST_JOBS].create_index([("state", 1), ("lease_until", 1)], name="state_lease")
