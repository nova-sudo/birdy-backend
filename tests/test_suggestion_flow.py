"""
tests/test_suggestion_flow.py
-----------------------------
Integration test for the suggestion store + orchestrator lifecycle against an
in-memory Mongo (mongomock). No LLM (no BYOK cred + no global key → composer uses
the deterministic template), so this is fully hermetic.

Covers the subtle bits: create + activity logging, dedup on re-run, dismiss
cooldown (a declined suggestion isn't recreated), and reconcile (an open
suggestion is auto-resolved once the finding stops reproducing).

Runnable via pytest OR directly: `PYTHONPATH=. python tests/test_suggestion_flow.py`
"""

import os

# Must be set before importing the orchestrator chain (core/crypto reads it at
# import time). A throwaway key — no real credentials are decrypted in this test.
from cryptography.fernet import Fernet
os.environ.setdefault("AI_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.pop("ANTHROPIC_API_KEY", None)  # force the template composer path

import asyncio

from mongomock_motor import AsyncMongoMockClient

from core.database import DB_NAME
from ai.suggestions import store
from ai.suggestions.orchestrator import run_pass_for_user


def _ad(id, name, status, spend, results):
    return {"id": id, "name": name, "status": status, "spend": spend, "results": results,
            "clicks": 50, "impressions": 5000, "reach": 3000}


def _group(ads):
    return {
        "id": "grp1", "user_id": "u1", "name": "Palm Peach", "ad_account_currency": "GBP",
        "facebook_cache": {"last_7d": {"ads": ads}},
    }


_BAD_ADS = [
    _ad("ad_zero", "Zero Lead Ad", "ACTIVE", 312, 0),
    _ad("ad_exp", "Expensive Ad", "ACTIVE", 96, 2),
    _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),
    _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),
]
_HEALTHY_ADS = [
    _ad("ad_zero", "Zero Lead Ad", "ACTIVE", 312, 40),   # now converting well
    _ad("ad_exp", "Expensive Ad", "ACTIVE", 96, 12),
    _ad("ad_good1", "Good Ad 1", "ACTIVE", 100, 10),
    _ad("ad_good2", "Good Ad 2", "ACTIVE", 120, 10),
]


async def _fresh_db_with(ads):
    client = AsyncMongoMockClient()
    db = client[DB_NAME]
    await db["client_groups"].insert_one(_group(ads))
    return client, db


async def _create_and_dedupe():
    client, db = await _fresh_db_with(_BAD_ADS)

    s1 = await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    open1 = await store.list_open_suggestions(db, "u1")
    # _BAD_ADS has two offending ads → one suggestion per ad.
    assert len(open1) == 2, f"expected 2 (one per ad), got {len(open1)}"
    assert all(s["action"]["type"] == "pause_ads" for s in open1)
    assert all(len(s["action"]["targets"]) == 1 for s in open1)  # one ad each
    assert all(s["composer"] == "template" for s in open1)  # no LLM in this test
    assert s1["created"] == 2

    acts = await store.list_recent_activity(db, "u1", 50)
    kinds = {a["kind"] for a in acts}
    assert "analysis_pass" in kinds, kinds
    assert "suggestion_created" in kinds, kinds

    # Re-run: same ads → refreshed in place, no duplicates, nothing "created".
    s2 = await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    open2 = await store.list_open_suggestions(db, "u1")
    assert len(open2) == 2, f"dedup failed: {len(open2)} suggestions"
    assert s2["created"] == 0
    print("PASS test_create_and_dedupe")


async def _dismiss_cooldown():
    client, db = await _fresh_db_with(_BAD_ADS)
    await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    open_sugs = await store.list_open_suggestions(db, "u1")
    assert len(open_sugs) >= 1
    for s in open_sugs:
        await store.mark_dismissed(db, "u1", s["_id"])

    # Re-run within cooldown → must NOT be recreated.
    await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    assert await store.list_open_suggestions(db, "u1") == []
    for s in open_sugs:
        doc = await store.get_suggestion(db, "u1", s["_id"])
        assert doc["status"] == "dismissed"
    print("PASS test_dismiss_cooldown")


async def _reconcile_resolves_stale():
    client, db = await _fresh_db_with(_BAD_ADS)
    await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    assert len(await store.list_open_suggestions(db, "u1")) == 2  # one per offending ad

    # Ads all become healthy → the finding no longer reproduces.
    await db["client_groups"].update_one(
        {"id": "grp1"}, {"$set": {"facebook_cache.last_7d.ads": _HEALTHY_ADS}}
    )
    s = await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    assert await store.list_open_suggestions(db, "u1") == []
    assert s["resolved"] >= 1, s
    all_docs = await db["ai_suggestions"].find({"user_id": "u1"}).to_list(length=None)
    assert any(d["status"] == "resolved" for d in all_docs)
    print("PASS test_reconcile_resolves_stale")


async def _cross_window_dedupe():
    # A single zero-lead ad flagged identically in BOTH windows (same offender set)
    # → ONE suggestion, not two. This mirrors the real case (one bad ad per client).
    single_bad = [
        _ad("ad_bad", "Wasteful Ad", "ACTIVE", 400, 0),   # 0 leads, over both floors
        _ad("ad_good1", "Good 1", "ACTIVE", 200, 20),
        _ad("ad_good2", "Good 2", "ACTIVE", 200, 20),
    ]
    client = AsyncMongoMockClient()
    db = client[DB_NAME]
    await db["client_groups"].insert_one({
        "id": "grp1", "user_id": "u1", "name": "Palm Peach", "ad_account_currency": "GBP",
        "facebook_cache": {"last_7d": {"ads": single_bad}, "last_30d": {"ads": single_bad}},
    })
    await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    await run_pass_for_user(db, "u1", "monthly", mongo_client=client)
    open_sugs = await store.list_open_suggestions(db, "u1")
    assert len(open_sugs) == 1, [s["id"] for s in open_sugs]
    # weekly created it; monthly only refreshed → origin_window stays weekly
    assert open_sugs[0]["origin_window"] == "weekly", open_sugs[0]


async def _skips_inactive_clients():
    client = AsyncMongoMockClient()
    db = client[DB_NAME]
    g = _group(_BAD_ADS)
    g["client_status"] = "Inactive"
    await db["client_groups"].insert_one(g)
    s = await run_pass_for_user(db, "u1", "weekly", mongo_client=client)
    assert s["clients"] == 0, s  # inactive client excluded by the query
    assert await store.list_open_suggestions(db, "u1") == []


def test_cross_window_dedupe():
    asyncio.run(_cross_window_dedupe())
    print("PASS test_cross_window_dedupe")


def test_skips_inactive_clients():
    asyncio.run(_skips_inactive_clients())
    print("PASS test_skips_inactive_clients")


def test_create_and_dedupe():
    asyncio.run(_create_and_dedupe())


def test_dismiss_cooldown():
    asyncio.run(_dismiss_cooldown())


def test_reconcile_resolves_stale():
    asyncio.run(_reconcile_resolves_stale())


if __name__ == "__main__":
    test_create_and_dedupe()
    test_dismiss_cooldown()
    test_reconcile_resolves_stale()
    test_cross_window_dedupe()
    test_skips_inactive_clients()
    print("\nAll suggestion-flow tests passed.")
