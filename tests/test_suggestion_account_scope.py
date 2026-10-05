"""
A suggestion belongs to the account that produced it. The same ad flagged for
two accounts — which happens when an agency's clients move between Birdy
logins — must give each account its own suggestion, and one account's
history (applied, declined) must never suppress or absorb the other's.
"""
from datetime import timedelta

from ai.suggestions import store
from ai.suggestions.contracts import Action, Evidence, Finding, ACTION_PAUSE_ADS


def _finding(ad_id="ad_1"):
    return Finding(
        agent="useless_ad_purger",
        client_group_id="g1",
        client_name="The Contour Co",
        severity="HIGH",
        title="Pause underperforming ad — Video",
        description="Spent £60 with 0 leads.",
        evidence=Evidence(window="weekly", stats=[], raw={}),
        action=Action(type=ACTION_PAUSE_ADS, targets=[{"object_id": ad_id, "object_type": "ad"}], params={}),
    )


async def test_two_accounts_flagging_one_ad_each_get_their_own(mock_db):
    a, created_a, _ = await store.upsert_finding(mock_db, "old@agency.com", _finding())
    b, created_b, _ = await store.upsert_finding(mock_db, "new@agency.com", _finding())

    assert created_a and created_b
    assert a["_id"] != b["_id"]
    assert b["user_id"] == "new@agency.com"
    assert await mock_db[store.SUGGESTIONS].count_documents({}) == 2


async def test_another_accounts_applied_suggestion_does_not_suppress(mock_db):
    old, _, _ = await store.upsert_finding(mock_db, "old@agency.com", _finding())
    await mock_db[store.SUGGESTIONS].update_one({"_id": old["_id"]}, {"$set": {"status": store.STATUS_APPLIED}})

    doc, created, _ = await store.upsert_finding(mock_db, "new@agency.com", _finding())

    assert created and doc["user_id"] == "new@agency.com"


async def test_a_legacy_unscoped_suggestion_from_another_account_is_left_alone(mock_db):
    """Before ids were scoped, the _id was the bare finding key."""
    legacy_id = _finding().compute_dedup_key()
    await mock_db[store.SUGGESTIONS].insert_one({
        "_id": legacy_id, "id": legacy_id, "user_id": "old@agency.com",
        "status": store.STATUS_RESOLVED, "origin_window": "weekly", "window": "weekly",
    })

    doc, created, _ = await store.upsert_finding(mock_db, "new@agency.com", _finding())

    assert created and doc["user_id"] == "new@agency.com" and doc["_id"] != legacy_id
    legacy = await mock_db[store.SUGGESTIONS].find_one({"_id": legacy_id})
    assert legacy["status"] == store.STATUS_RESOLVED and legacy["user_id"] == "old@agency.com"


async def test_the_same_accounts_legacy_history_still_counts(mock_db):
    legacy_id = _finding().compute_dedup_key()
    await mock_db[store.SUGGESTIONS].insert_one({
        "_id": legacy_id, "id": legacy_id, "user_id": "old@agency.com",
        "status": store.STATUS_DISMISSED, "dismissed_at": store._now() - timedelta(days=2),
        "origin_window": "weekly", "window": "weekly",
    })

    doc, created, _ = await store.upsert_finding(mock_db, "old@agency.com", _finding())

    assert doc is None and not created            # still inside the 14-day cooldown
    assert await mock_db[store.SUGGESTIONS].count_documents({}) == 1


async def test_rerunning_refreshes_in_place(mock_db):
    first, created, _ = await store.upsert_finding(mock_db, "new@agency.com", _finding())
    again, created_again, _ = await store.upsert_finding(mock_db, "new@agency.com", _finding())

    assert created and not created_again
    assert first["_id"] == again["_id"]
    assert await mock_db[store.SUGGESTIONS].count_documents({}) == 1
