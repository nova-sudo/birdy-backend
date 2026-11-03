"""Suggestion action types for creative tests (Task 22)."""

import pytest

import ai.suggestions.actions as actions_mod
from ai.suggestions import actions, store
from ai.suggestions.contracts import (
    ACTION_ADOPT_CREATIVE_AD, ACTION_RETRY_CREATIVE_POST, ACTION_SCALE_CREATIVE,
)
from services.creatives import posting, reconcile
from services.creatives.store import TEST_ADS

U = "u1"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(actions_mod, "_sync_slack", nothing)


async def seed(db, action, sid="sug_c1", agent="creative_test_judge", rule_id="creative_x"):
    await db[store.SUGGESTIONS].insert_one({
        "_id": sid, "id": sid, "user_id": U, "status": "open", "agent": agent,
        "client_group_id": "g1", "client_name": "Client 1", "window": "weekly",
        "origin_window": "weekly", "title": "Creative thing", "rule_id": rule_id,
        "action": action, "evidence": {"creative_test_id": "t1"}})
    return sid


async def test_scale_creative_marks_applied_and_returns_redirect(mock_db, mock_mongo_client):
    sid = await seed(mock_db, {"type": ACTION_SCALE_CREATIVE, "targets": [], "params": {"test_id": "t1"}})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert res["ok"] and res["outcome"] == "applied"
    assert res["redirect"] == "/campaigns/creatives/new?scale=t1"
    doc = await store.get_suggestion(mock_db, U, sid)
    assert doc["status"] == "applied" and doc["applied_targets"] == []


async def test_execute_scale_writes_nothing(mock_mongo_client):
    out = await actions.execute_action(mock_mongo_client, U, {"type": ACTION_SCALE_CREATIVE,
                                                              "params": {"test_id": "t9"}})
    assert out == {"succeeded": [], "failed": [], "redirect": "/campaigns/creatives/new?scale=t9"}


async def test_retry_creative_post_calls_retry_with_stored_patch(mock_db, mock_mongo_client, monkeypatch):
    calls = []

    async def fake_retry(db, user_id, test_id, test_ad_id, *, patch=None, now=None):
        calls.append((user_id, test_id, test_ad_id, patch))
        return {"test_id": test_id, "test_ad_id": test_ad_id, "queued": True}

    monkeypatch.setattr(posting, "retry", fake_retry)
    sid = await seed(mock_db, {"type": ACTION_RETRY_CREATIVE_POST, "targets": [], "params": {
        "test_id": "t1", "test_ad_id": "ta1", "patch": {"destination": "website"}}})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert calls == [(U, "t1", "ta1", {"destination": "website"})]
    assert res["outcome"] == "applied" and res["succeeded"] == ["ta1"]
    assert (await store.get_suggestion(mock_db, U, sid))["status"] == "applied"


async def test_retry_failure_keeps_suggestion_open(mock_db, mock_mongo_client, monkeypatch):
    async def fake_retry(*a, **k):
        raise posting.PostingError("Only failed ads can be retried", 409)

    monkeypatch.setattr(posting, "retry", fake_retry)
    sid = await seed(mock_db, {"type": ACTION_RETRY_CREATIVE_POST, "targets": [], "params": {
        "test_id": "t1", "test_ad_id": "ta1", "patch": {}}})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert res["ok"] is False and res["outcome"] == "failed"
    assert res["detail"] == "Only failed ads can be retried"
    doc = await store.get_suggestion(mock_db, U, sid)
    assert doc["status"] == "open" and not doc.get("applying_at")


async def test_adopt_applies_then_undo_is_refused(mock_db, mock_mongo_client, monkeypatch):
    calls = []

    async def fake_adopt(db, user_id, test_id, ad_id):
        calls.append((user_id, test_id, ad_id))
        return {"id": "row1"}

    monkeypatch.setattr(reconcile, "adopt", fake_adopt)
    sid = await seed(mock_db, {"type": ACTION_ADOPT_CREATIVE_AD, "targets": [],
                               "params": {"test_id": "t1", "ad_id": "a1"}})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert res["outcome"] == "applied" and calls == [(U, "t1", "a1")]
    undo = await actions.undo_suggestion(mock_db, mock_mongo_client, U, sid)
    assert undo["ok"] is False and undo["detail"] == "Nothing to undo"
    assert (await store.get_suggestion(mock_db, U, sid))["status"] == "applied"


async def test_adopt_error_is_failed(mock_db, mock_mongo_client, monkeypatch):
    async def fake_adopt(*a, **k):
        raise ValueError("ad a1 is already tracked")

    monkeypatch.setattr(reconcile, "adopt", fake_adopt)
    sid = await seed(mock_db, {"type": ACTION_ADOPT_CREATIVE_AD, "targets": [],
                               "params": {"test_id": "t1", "ad_id": "a1"}})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert res["outcome"] == "failed" and res["detail"] == "ad a1 is already tracked"


async def test_creative_pause_mirrors_meta_status_and_undo(mock_db, mock_mongo_client, monkeypatch):
    writes = []

    async def fake_get(user_id, object_id, object_type, client):
        return {"status": "ACTIVE", "effective_status": "ACTIVE"}

    async def fake_set(user_id, object_id, object_type, status, client):
        writes.append((object_id, status))

    monkeypatch.setattr(actions_mod, "get_object_state", fake_get)
    monkeypatch.setattr(actions_mod, "set_object_status", fake_set)
    await mock_db[TEST_ADS].insert_one({"id": "ta1", "user_id": U, "test_id": "t1",
                                        "meta": {"ad_id": "ad1"}, "meta_status": "ACTIVE"})
    await mock_db[TEST_ADS].insert_one({"id": "ta2", "user_id": "u2", "test_id": "t1",
                                        "meta": {"ad_id": "ad1x"}, "meta_status": "ACTIVE"})
    sid = await seed(mock_db, {"type": "pause_ads", "params": {},
                               "targets": [{"object_id": "ad1", "object_type": "ad"}]})
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, sid)
    assert res["outcome"] == "applied" and writes == [("ad1", "PAUSED")]
    assert (await mock_db[TEST_ADS].find_one({"id": "ta1"}))["meta_status"] == "PAUSED"
    undo = await actions.undo_suggestion(mock_db, mock_mongo_client, U, sid)
    assert undo["outcome"] == "undone"
    assert (await mock_db[TEST_ADS].find_one({"id": "ta1"}))["meta_status"] == "ACTIVE"
    assert (await mock_db[TEST_ADS].find_one({"id": "ta2"}))["meta_status"] == "ACTIVE"
