"""
tests/test_slack_suggestions.py
-------------------------------
Tests for the Slack suggestion surface:
  * build_suggestion_blocks — the message shape (2 blocks, right buttons/values).
  * shared apply/dismiss actions — used by BOTH the dashboard and Slack, with the
    Meta call stubbed so no network is hit.

Hermetic (mongomock, throwaway key, stubbed Meta). Run directly or via pytest.
"""

import os
from datetime import datetime

from cryptography.fernet import Fernet
os.environ.setdefault("AI_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.pop("ANTHROPIC_API_KEY", None)

import asyncio

from mongomock_motor import AsyncMongoMockClient

from core.database import DB_NAME
from ai.suggestions import store, actions
import ai.suggestions.actions as actions_mod
from services.slack_suggestion_notifier import build_suggestion_blocks


# --- block builder ------------------------------------------------------------

def test_build_suggestion_blocks():
    doc = {
        "id": "sug_1", "title": "Pause 2 underperforming ads",
        "description": "Both sit well above your £22 target.",
        "client_name": "Palm Peach", "platform": "Meta Ads", "severity": "HIGH",
        "stats": [{"label": "CPL", "value": "£48.00", "bad": True}],
        "action": {"type": "pause_ads", "targets": [{"object_id": "ad_1", "object_type": "ad"}]},
    }
    blocks, fallback = build_suggestion_blocks(doc)
    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert blocks[1]["type"] == "actions"
    els = blocks[1]["elements"]
    assert [e["action_id"] for e in els] == ["suggestion_apply", "suggestion_ignore"]
    assert all(e["value"] == "sug_1" for e in els)
    assert "confirm" in els[0]           # the money action gets a native confirm
    assert "Palm Peach" in fallback
    print("PASS test_build_suggestion_blocks")


def test_build_blocks_advisory_has_only_ignore():
    doc = {"id": "sug_2", "title": "Heads up", "description": "d",
           "client_name": "C", "action": None, "stats": []}
    blocks, _ = build_suggestion_blocks(doc)
    assert [e["action_id"] for e in blocks[1]["elements"]] == ["suggestion_ignore"]
    print("PASS test_build_blocks_advisory_has_only_ignore")


# --- shared actions -----------------------------------------------------------

async def _seed(db, action=None):
    now = datetime.utcnow()
    doc = {
        "_id": "sug_x", "id": "sug_x", "user_id": "u1", "client_name": "C",
        "client_group_id": "g1", "title": "T", "description": "d", "stats": [],
        "action": action, "status": store.STATUS_OPEN, "window": "weekly",
        "created_at": now, "updated_at": now,
    }
    await db[store.SUGGESTIONS].insert_one(doc)


async def _dismiss_flow():
    db = AsyncMongoMockClient()[DB_NAME]
    await _seed(db)
    res = await actions.dismiss_suggestion(db, None, "u1", "sug_x", source="slack")
    assert res["ok"] and res["outcome"] == "dismissed", res
    assert (await store.get_suggestion(db, "u1", "sug_x"))["status"] == "dismissed"
    acts = await store.list_recent_activity(db, "u1", 10)
    assert any(a["kind"] == "suggestion_dismissed" and a["source"] == "slack" for a in acts)


async def _apply_flow():
    calls = []

    async def fake_set(user_id, object_id, object_type, status, mongo_client):
        calls.append((object_id, status))
        return {"success": True}

    # Stub the Meta call the shared action reaches for (module-global lookup).
    original = actions_mod.set_object_status
    actions_mod.set_object_status = fake_set
    try:
        db = AsyncMongoMockClient()[DB_NAME]
        await _seed(db, action={"type": "pause_ads",
                                "targets": [{"object_id": "ad_1", "object_type": "ad"}]})
        res = await actions.apply_suggestion(db, None, "u1", "sug_x", source="slack")
        assert res["ok"] and res["outcome"] == "applied", res
        assert calls == [("ad_1", "PAUSED")], calls
        assert (await store.get_suggestion(db, "u1", "sug_x"))["status"] == "applied"
        acts = await store.list_recent_activity(db, "u1", 10)
        assert any(a["kind"] == "action_applied" and a["label"] == "Approved by you"
                   and a["source"] == "slack" for a in acts)
    finally:
        actions_mod.set_object_status = original


async def _apply_not_found():
    db = AsyncMongoMockClient()[DB_NAME]
    res = await actions.apply_suggestion(db, None, "u1", "missing", source="slack")
    assert res["ok"] is False and res["outcome"] == "not_found", res


async def _undo_flow():
    calls = []

    async def fake_set(user_id, object_id, object_type, status, mongo_client):
        calls.append((object_id, status))
        return {"success": True}

    original = actions_mod.set_object_status
    actions_mod.set_object_status = fake_set
    try:
        db = AsyncMongoMockClient()[DB_NAME]
        await _seed(db, action={"type": "pause_ads",
                                "targets": [{"object_id": "ad_1", "object_type": "ad", "name": "Ad One"}]})

        applied = await actions.apply_suggestion(db, None, "u1", "sug_x", source="dashboard")
        assert applied["outcome"] == "applied", applied
        assert calls == [("ad_1", "PAUSED")], calls
        doc = await store.get_suggestion(db, "u1", "sug_x")
        assert doc["status"] == "applied"
        assert [t["object_id"] for t in doc["applied_targets"]] == ["ad_1"]

        undone = await actions.undo_suggestion(db, None, "u1", "sug_x", source="dashboard")
        assert undone["ok"] and undone["outcome"] == "undone", undone
        assert calls[-1] == ("ad_1", "ACTIVE"), calls   # exactly the paused ad re-enabled
        doc2 = await store.get_suggestion(db, "u1", "sug_x")
        assert doc2["status"] == "open"                  # back on the board
        assert not doc2.get("applied_targets")
        acts = await store.list_recent_activity(db, "u1", 10)
        assert any(a["kind"] == "action_undone" and a["label"] == "Undone by you" for a in acts)
    finally:
        actions_mod.set_object_status = original


async def _undo_requires_applied():
    db = AsyncMongoMockClient()[DB_NAME]
    await _seed(db, action={"type": "pause_ads", "targets": [{"object_id": "ad_1", "object_type": "ad"}]})
    res = await actions.undo_suggestion(db, None, "u1", "sug_x")
    assert res["ok"] is False and res["outcome"] == "not_applied", res


def test_undo_reenables_and_reopens():
    asyncio.run(_undo_flow())
    print("PASS test_undo_reenables_and_reopens")


def test_undo_requires_applied():
    asyncio.run(_undo_requires_applied())
    print("PASS test_undo_requires_applied")


def test_actions_dismiss():
    asyncio.run(_dismiss_flow())
    print("PASS test_actions_dismiss")


def test_actions_apply():
    asyncio.run(_apply_flow())
    print("PASS test_actions_apply")


def test_actions_apply_not_found():
    asyncio.run(_apply_not_found())
    print("PASS test_actions_apply_not_found")


if __name__ == "__main__":
    test_build_suggestion_blocks()
    test_build_blocks_advisory_has_only_ignore()
    test_actions_dismiss()
    test_actions_apply()
    test_actions_apply_not_found()
    test_undo_reenables_and_reopens()
    test_undo_requires_applied()
    print("\nAll slack-suggestion tests passed.")
