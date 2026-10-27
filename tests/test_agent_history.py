import asyncio
from datetime import date, datetime, timedelta

from ai.agents import history
from ai.agents.snapshot import load_ad_days

NOW = datetime(2026, 10, 27, 12, 0)


async def _seed_groups(db):
    await db.client_groups.insert_many([
        {"id": "g1", "user_id": "u1", "meta_ad_account_id": "act_1", "meta_campaign_ids": ["c1"]},
        {"id": "g2", "user_id": "u1", "meta_ad_account_id": "act_2"},
        {"id": "g3", "user_id": "u1"},  # no meta account
    ])


async def test_ensure_docs_covers_every_watched_group_once(mock_db):
    await _seed_groups(mock_db)
    await mock_db.agents.insert_many([
        {"_id": "a1", "user_id": "u1", "status": "draft", "scope": {"client_group_ids": ["g1", "g2"]}},
        {"_id": "a2", "user_id": "u1", "status": "active", "scope": {"client_group_ids": ["g2", "g3"]}},
    ])
    assert await history.ensure_docs(mock_db) == 2
    docs = {d["_id"]: d async for d in mock_db[history.HISTORY].find()}
    assert set(docs) == {"g1", "g2"}
    assert docs["g1"]["user_id"] == "u1"
    assert docs["g1"]["ad_account_id"] == "act_1"
    assert docs["g1"]["campaign_ids"] == ["c1"]
    assert docs["g1"]["status"] == "pending"
    assert docs["g1"]["oldest_done"] is None and docs["g1"]["claimed_at"] is None

    await mock_db[history.HISTORY].update_one({"_id": "g1"}, {"$set": {"status": "ready"}})
    assert await history.ensure_docs(mock_db) == 0
    assert (await mock_db[history.HISTORY].find_one({"_id": "g1"}))["status"] == "ready"


async def _doc(db, gid, **fields):
    await db[history.HISTORY].insert_one({"_id": gid, "user_id": "u1", "claimed_at": None, **fields})


async def test_claim_order_cas_and_stale_reclaim(mock_db):
    await _doc(mock_db, "ready_due", status="ready", last_topup_at=NOW - timedelta(hours=6),
               state_refreshed_at=NOW)
    await _doc(mock_db, "pending", status="pending")
    await _doc(mock_db, "ready_fresh", status="ready", last_topup_at=NOW - timedelta(hours=1),
               state_refreshed_at=NOW)
    await _doc(mock_db, "fresh_claim", status="pending", claimed_at=NOW - timedelta(minutes=2))
    await _doc(mock_db, "stale_claim", status="running", claimed_at=NOW - timedelta(minutes=11))

    got = await history.claim(mock_db, NOW, 10)
    ids = [d["_id"] for d in got]
    assert set(ids) == {"pending", "stale_claim", "ready_due"}
    assert ids[-1] == "ready_due"  # pending/running first
    assert all(d["claimed_at"] == NOW for d in got)

    # nothing left to claim
    assert await history.claim(mock_db, NOW, 10) == []


async def test_claim_respects_limit_and_prefers_in_flight(mock_db):
    await _doc(mock_db, "ready_due", status="ready", last_topup_at=NOW - timedelta(hours=6),
               state_refreshed_at=NOW)
    await _doc(mock_db, "pending", status="pending")
    got = await history.claim(mock_db, NOW, 1)
    assert [d["_id"] for d in got] == ["pending"]


async def test_claim_ready_due_for_state_refresh_and_error_retry(mock_db):
    await _doc(mock_db, "stale_state", status="ready", last_topup_at=NOW - timedelta(hours=1),
               state_refreshed_at=NOW - timedelta(days=2))
    await _doc(mock_db, "err_old", status="error", updated_at=NOW - timedelta(hours=7))
    await _doc(mock_db, "err_new", status="error", updated_at=NOW - timedelta(hours=1))
    ids = {d["_id"] for d in await history.claim(mock_db, NOW, 10)}
    assert ids == {"stale_state", "err_old"}


async def test_concurrent_claims_never_share_a_group(mock_db):
    for i in range(6):
        await _doc(mock_db, f"g{i}", status="pending")
    a, b = await asyncio.gather(history.claim(mock_db, NOW, 6), history.claim(mock_db, NOW, 6))
    ids = [d["_id"] for d in a + b]
    assert len(ids) == len(set(ids)) == 6


async def test_release_clears_claim_and_sets_fields(mock_db):
    await _doc(mock_db, "g1", status="running", claimed_at=NOW)
    await history.release(mock_db, "g1", status="ready", oldest_done="2025-09-28")
    d = await mock_db[history.HISTORY].find_one({"_id": "g1"})
    assert d["status"] == "ready" and d["oldest_done"] == "2025-09-28"
    assert d["claimed_at"] is None and d["updated_at"] is not None


async def test_statuses_reads_missing_as_pending(mock_db):
    await _doc(mock_db, "g1", status="error", error="no_access", oldest_done="2026-01-01",
               newest_done="2026-10-01", last_topup_at=None)
    out = await history.statuses(mock_db, ["g1", "gx"])
    assert out["g1"]["status"] == "error" and out["g1"]["error"] == "no_access"
    assert out["g1"]["oldest_done"] == "2026-01-01"
    assert out["gx"] == {"status": "pending", "error": None, "oldest_done": None,
                         "newest_done": None, "last_topup_at": None}


def test_is_ready_keeps_a_backfilled_client_usable_after_a_topup_error():
    assert history.is_ready({"status": "ready"})
    assert not history.is_ready({"status": "running", "oldest_done": "2025-09-27"})
    assert history.is_ready({"status": "error", "last_topup_at": NOW})
    assert not history.is_ready({"status": "error", "last_topup_at": None})
    assert not history.is_ready(None)


def test_window_start():
    assert history.window_start(date(2026, 10, 27)) == date(2025, 9, 28)


async def test_load_ad_days_only_reads_this_users_rows(mock_db):
    def row(user, ad):
        return {"user_id": user, "client_group_id": "cg", "ad_id": ad, "date_start": "2026-10-01",
                "insight_data": {"spend": "5"}}
    await mock_db.facebook_ad_insights.insert_many([row("u1", "mine"), row("u2", "theirs")])
    out = await load_ad_days(mock_db, "u1", "cg", date(2026, 10, 1), date(2026, 10, 2))
    assert set(out) == {"mine"}
