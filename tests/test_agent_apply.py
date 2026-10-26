"""
tests/test_agent_apply.py
-------------------------
apply_suggestion's safety layer: guardrails for agent suggestions, the re-read
before writing, and the atomic open → applied transition. Meta is stubbed.
"""

import asyncio
import copy
from datetime import datetime, timedelta

import httpx
import pytest

import ai.suggestions.actions as actions_mod
from ai.agents.defaults import DEFAULT_GUARDRAILS, DEFAULT_PERMISSIONS
from ai.agents.meta_state import AD_STATE
from ai.suggestions import actions, store
import services.meta_status_service as mss

GID = "cg1"


@pytest.fixture
def meta(monkeypatch):
    """Stubbed Meta: `status` maps object_id → effective status (None = deleted)."""
    state = {"status": {}, "writes": [], "reads": [], "guardrail_calls": 0}

    async def fake_get(user_id, object_id, object_type, mongo_client):
        """Value in state["status"]: None (deleted), an effective status string
        (own status assumed equal), or a full {status, effective_status} dict."""
        state["reads"].append(object_id)
        await asyncio.sleep(0)
        v = state["status"].get(object_id, "ACTIVE")
        if v is None or isinstance(v, dict):
            return v
        return {"status": v, "effective_status": v}

    async def fake_set(user_id, object_id, object_type, status, mongo_client):
        await asyncio.sleep(0)
        state["writes"].append((object_id, status))
        return {"success": True}

    monkeypatch.setattr(actions_mod, "get_object_state", fake_get)
    monkeypatch.setattr(actions_mod, "set_object_status", fake_set)

    real_check = actions_mod.check_guardrails

    async def counting_check(*a, **kw):
        state["guardrail_calls"] += 1
        return await real_check(*a, **kw)

    monkeypatch.setattr(actions_mod, "check_guardrails", counting_check)
    return state


async def _seed_agent(db, **over):
    agent = {
        "_id": "ag1", "user_id": "u1", "type": "media_buying",
        "scope": {"client_group_ids": [GID], "schedule": {"time": "07:00", "tz": "Europe/London"}},
        "permissions": copy.deepcopy(DEFAULT_PERMISSIONS),
        "guardrails": copy.deepcopy(DEFAULT_GUARDRAILS),
        "client_caps": {},
    }
    agent.update(over)
    await db["agents"].insert_one(agent)
    return agent


async def _seed_suggestion(db, *, agent=True, targets=None, atype="pause_ads", sid="sug_1"):
    now = datetime.utcnow()
    doc = {
        "_id": sid, "id": sid, "user_id": "u1", "client_name": "C", "client_group_id": GID,
        "title": "Pause Ad one", "description": "d", "stats": [], "window": "agent",
        "action": {"type": atype, "params": {},
                   "targets": targets or [{"object_id": "a1", "object_type": "ad", "name": "Ad one"}]},
        "status": store.STATUS_OPEN, "created_at": now, "updated_at": now,
    }
    if agent:
        doc.update({"agent_id": "ag1", "rule_id": "r1", "rule_version": 1})
    await db[store.SUGGESTIONS].insert_one(doc)
    return doc


async def _seed_ad_state(db):
    for ad_id, status in (("a1", "ACTIVE"), ("a2", "ACTIVE")):
        await db[AD_STATE].insert_one({"client_group_id": GID, "ad_id": ad_id, "adset_id": "s1",
                                       "effective_status": status, "name": ad_id})


async def test_agent_apply_writes_and_logs_agent_fields(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)

    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1", actor=store.ACTOR_BIRDY,
                                         source="agent")
    assert res["ok"] and res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "PAUSED")]
    assert meta["guardrail_calls"] == 1
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "applied"
    assert "applying_at" not in doc
    assert doc["decided_by"]["actor"] == store.ACTOR_BIRDY

    act = await mock_db[store.ACTIVITY].find_one({"ref_id": "sug_1"})
    assert act["agent_id"] == "ag1" and act["rule_id"] == "r1"
    assert act["applied_action_type"] == "pause_ads"
    assert act["expires_at"] - act["created_at"] == timedelta(days=395)


async def test_blocked_keeps_open_with_reason(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db, targets=[{"object_id": "a1", "object_type": "ad",
                                              "name": "Evergreen hero"}])

    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "blocked" and res["ok"] is False, res
    assert res["blocked_reason"]["code"] == "protected_tag"
    assert meta["writes"] == [] and meta["reads"] == []
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "open"
    assert doc["blocked_reason"]["code"] == "protected_tag"
    assert doc["blocked_reason"]["message"]
    assert "applying_at" not in doc  # claim released → can be retried

    # Guardrail relaxed → retry succeeds and clears the reason.
    await mock_db["agents"].update_one({"_id": "ag1"}, {"$set": {"guardrails.protected_tags": []}})
    res2 = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res2["outcome"] == "applied", res2
    doc2 = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert "blocked_reason" not in doc2


async def test_already_in_meta_skips_write(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = "PAUSED"

    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["ok"] and res["outcome"] == "already_in_meta", res
    assert meta["writes"] == []
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "applied" and doc["already_in_meta"] is True
    assert doc["applied_targets"] == []
    # Nothing was paused by us → it must not count towards the pause cap.
    act = await mock_db[store.ACTIVITY].find_one({"ref_id": "sug_1"})
    assert act.get("applied_action_type") is None


async def test_enable_with_parent_paused_still_writes(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_suggestion(mock_db, atype="enable_ads")
    meta["status"]["a1"] = {"status": "PAUSED", "effective_status": "ADSET_PAUSED"}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "ACTIVE")]


async def test_enable_decides_from_effective_status(mock_db, meta):
    # Enable is unchanged: only an effective ACTIVE counts as done.
    await _seed_agent(mock_db)
    await _seed_suggestion(mock_db, atype="enable_ads")
    meta["status"]["a1"] = {"status": "ACTIVE", "effective_status": "ACTIVE"}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "already_in_meta", res


async def test_adset_paused_ad_still_gets_paused(mock_db, meta):
    # Own status ACTIVE: it would start spending again once the ad set is re-enabled.
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = {"status": "ACTIVE", "effective_status": "ADSET_PAUSED"}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "PAUSED")]


@pytest.mark.parametrize("eff", ["PENDING_REVIEW", "WITH_ISSUES", "CAMPAIGN_PAUSED", "IN_PROCESS"])
async def test_not_yet_delivering_ad_still_gets_paused(mock_db, meta, eff):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = {"status": "ACTIVE", "effective_status": eff}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "PAUSED")]


async def test_archived_is_already_in_meta(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = {"status": "ARCHIVED", "effective_status": "ARCHIVED"}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "already_in_meta", res
    assert meta["writes"] == []


async def test_effective_only_accepts_paused_archived_deleted(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = {"status": None, "effective_status": "ADSET_PAUSED"}
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "PAUSED")]


async def test_deleted_ad_is_already_in_meta(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = None

    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "already_in_meta", res
    assert meta["writes"] == []


async def test_only_targets_not_in_desired_state_are_written(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await mock_db[AD_STATE].insert_one({"client_group_id": GID, "ad_id": "a3", "adset_id": "s1",
                                        "effective_status": "ACTIVE", "name": "a3"})
    await _seed_suggestion(mock_db, targets=[{"object_id": "a1", "object_type": "ad", "name": "a1"},
                                             {"object_id": "a2", "object_type": "ad", "name": "a2"}])
    meta["status"]["a2"] = "PAUSED"
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res
    assert meta["writes"] == [("a1", "PAUSED")]
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert [t["object_id"] for t in doc["applied_targets"]] == ["a1"]  # undo leaves a2 alone


async def test_double_apply_second_gets_already(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)

    r1, r2 = await asyncio.gather(
        actions.apply_suggestion(mock_db, None, "u1", "sug_1", source="dashboard"),
        actions.apply_suggestion(mock_db, None, "u1", "sug_1", source="slack", by="U123"),
    )
    assert len(meta["writes"]) == 1, meta["writes"]
    outcomes = sorted([r1["outcome"], r2["outcome"]])
    assert outcomes == ["already", "applied"], (r1, r2)
    loser = r1 if r1["outcome"] == "already" else r2
    assert "decided_by" in loser
    # Only one activity row.
    assert await mock_db[store.ACTIVITY].count_documents({"ref_id": "sug_1"}) == 1


async def test_double_apply_non_agent_also_atomic(mock_db, meta):
    await _seed_suggestion(mock_db, agent=False)
    r1, r2 = await asyncio.gather(
        actions.apply_suggestion(mock_db, None, "u1", "sug_1"),
        actions.apply_suggestion(mock_db, None, "u1", "sug_1"),
    )
    assert len(meta["writes"]) == 1
    assert sorted([r1["outcome"], r2["outcome"]]) == ["already", "applied"]


async def test_already_applied_returns_decided_by(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    await actions.apply_suggestion(mock_db, None, "u1", "sug_1", source="slack", by="U9")
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "already"
    assert res["decided_by"]["source"] == "slack" and res["decided_by"]["by"] == "U9"
    assert len(meta["writes"]) == 1


async def test_stale_claim_can_be_reclaimed(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    await mock_db[store.SUGGESTIONS].update_one(
        {"_id": "sug_1"}, {"$set": {"applying_at": datetime.utcnow() - timedelta(minutes=6)}})
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "applied", res

    # A fresh claim held by someone else → already.
    await _seed_suggestion(mock_db, sid="sug_2")
    await mock_db[store.SUGGESTIONS].update_one(
        {"_id": "sug_2"}, {"$set": {"applying_at": datetime.utcnow() - timedelta(minutes=1)}})
    res2 = await actions.apply_suggestion(mock_db, None, "u1", "sug_2")
    assert res2["outcome"] == "already", res2
    assert len(meta["writes"]) == 1


async def test_write_exception_releases_claim(mock_db, meta, monkeypatch):
    await _seed_suggestion(mock_db, agent=False)

    async def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(actions_mod, "set_object_status", boom)
    with pytest.raises(RuntimeError):
        await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "open" and "applying_at" not in doc


async def test_failed_write_releases_claim(mock_db, meta, monkeypatch):
    await _seed_suggestion(mock_db, agent=False)

    async def fail(*a, **kw):
        raise mss.MetaStatusError("nope", 400)

    monkeypatch.setattr(actions_mod, "set_object_status", fail)
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "failed"
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "open" and "applying_at" not in doc


async def test_non_agent_suggestion_unchanged(mock_db, meta):
    await _seed_suggestion(mock_db, agent=False,
                           targets=[{"object_id": "a1", "object_type": "ad", "name": "Evergreen hero"}])
    res = await actions.apply_suggestion(mock_db, None, "u1", "sug_1", source="dashboard")
    assert res["ok"] and res["outcome"] == "applied", res
    assert meta["guardrail_calls"] == 0  # purger path never touches guardrails
    assert meta["writes"] == [("a1", "PAUSED")]
    act = await mock_db[store.ACTIVITY].find_one({"ref_id": "sug_1"})
    assert "agent_id" not in act
    assert act["expires_at"] - act["created_at"] == timedelta(days=store.ACTIVITY_TTL_DAYS)


# --- get_object_status --------------------------------------------------------

@pytest.fixture
def graph(monkeypatch):
    state = {"resp": None, "calls": []}

    async def fake_token(user_id, mongo_client):
        return {"access_token": "tok"}

    async def fake_get(self, url, params=None, **kw):
        state["calls"].append((str(url), dict(params or {})))
        code, body = state["resp"]
        return httpx.Response(code, json=body, request=httpx.Request("GET", str(url)))

    monkeypatch.setattr(mss, "get_facebook_token", fake_token)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return state


async def test_get_object_status_reads_effective_status(graph):
    graph["resp"] = (200, {"id": "a1", "effective_status": "CAMPAIGN_PAUSED", "status": "ACTIVE"})
    assert await mss.get_object_status("u1", "a1", "ad", None) == "CAMPAIGN_PAUSED"
    assert "effective_status" in graph["calls"][0][1]["fields"]

    graph["resp"] = (200, {"id": "c1", "status": "PAUSED"})
    assert await mss.get_object_status("u1", "c1", "campaign", None) == "PAUSED"


async def test_get_object_state_reads_both_fields_in_one_call(graph):
    graph["resp"] = (200, {"id": "a1", "effective_status": "adset_paused", "status": "ACTIVE"})
    st = await mss.get_object_state("u1", "a1", "ad", None)
    assert st == {"status": "ACTIVE", "effective_status": "ADSET_PAUSED"}
    assert len(graph["calls"]) == 1
    assert set(graph["calls"][0][1]["fields"].split(",")) == {"effective_status", "status"}

    graph["resp"] = (400, {"error": {"code": 100, "message": "does not exist"}})
    assert await mss.get_object_state("u1", "a1", "ad", None) is None


async def test_get_object_status_deleted_returns_none(graph):
    graph["resp"] = (400, {"error": {"code": 100, "error_subcode": 33,
                                     "message": "Object with ID 'a1' does not exist"}})
    assert await mss.get_object_status("u1", "a1", "ad", None) is None


async def test_get_object_status_other_error_raises(graph):
    graph["resp"] = (500, {"error": {"code": 2, "message": "Service temporarily unavailable"}})
    with pytest.raises(mss.MetaStatusError):
        await mss.get_object_status("u1", "a1", "ad", None)


# --- fix round 1: claims respected by dismiss / auto-resolve (I1) --------------

async def test_dismiss_during_inflight_claim_is_busy(mock_db, meta):
    await _seed_suggestion(mock_db, agent=False)
    token = await store.claim_for_apply(mock_db, "u1", "sug_1")
    assert token

    res = await actions.dismiss_suggestion(mock_db, None, "u1", "sug_1", source="slack")
    assert res["outcome"] == "busy" and res["ok"] is False, res
    doc = await store.get_suggestion(mock_db, "u1", "sug_1")
    assert doc["status"] == "open"
    assert doc["applying_token"] == token  # claim intact
    assert await mock_db[store.ACTIVITY].count_documents({"ref_id": "sug_1"}) == 0


async def test_dismiss_with_stale_claim_goes_through(mock_db, meta):
    await _seed_suggestion(mock_db, agent=False)
    await mock_db[store.SUGGESTIONS].update_one(
        {"_id": "sug_1"}, {"$set": {"applying_at": datetime.utcnow() - timedelta(minutes=6)}})
    res = await actions.dismiss_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "dismissed", res


async def test_close_stale_open_skips_claimed_docs(mock_db, meta):
    for sid in ("sug_a", "sug_b"):
        await _seed_suggestion(mock_db, agent=False, sid=sid)
        await mock_db[store.SUGGESTIONS].update_one({"_id": sid}, {"$set": {"agent": "purger",
                                                                              "origin_window": "weekly"}})
    assert await store.claim_for_apply(mock_db, "u1", "sug_a")
    closed = await store.close_stale_open(mock_db, "u1", "purger", "weekly", keep_ids=set())
    assert closed == 1
    assert (await store.get_suggestion(mock_db, "u1", "sug_a"))["status"] == "open"
    assert (await store.get_suggestion(mock_db, "u1", "sug_b"))["status"] == "resolved"


# --- fix round 1: meta_ad_state written back after a write (I2) ----------------

async def test_sequential_pauses_cannot_empty_an_adset(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)  # a1, a2: the only ACTIVE ads in s1
    await _seed_suggestion(mock_db, sid="sug_1",
                           targets=[{"object_id": "a1", "object_type": "ad", "name": "a1"}])
    await _seed_suggestion(mock_db, sid="sug_2",
                           targets=[{"object_id": "a2", "object_type": "ad", "name": "a2"}])

    r1 = await actions.apply_suggestion(mock_db, None, "u1", "sug_1", actor=store.ACTOR_BIRDY)
    assert r1["outcome"] == "applied", r1
    st = await mock_db[AD_STATE].find_one({"client_group_id": GID, "ad_id": "a1"})
    assert st["effective_status"] == "PAUSED" and st["status"] == "PAUSED"
    assert st["paused_at"]  # local today, ISO date

    r2 = await actions.apply_suggestion(mock_db, None, "u1", "sug_2", actor=store.ACTOR_BIRDY)
    assert r2["outcome"] == "blocked", r2
    assert r2["blocked_reason"]["code"] == "last_live_ad"
    assert meta["writes"] == [("a1", "PAUSED")]


async def test_undo_writes_back_active_and_clears_paused_at(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await _seed_suggestion(mock_db)
    await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    res = await actions.undo_suggestion(mock_db, None, "u1", "sug_1")
    assert res["outcome"] == "undone", res
    st = await mock_db[AD_STATE].find_one({"client_group_id": GID, "ad_id": "a1"})
    assert st["effective_status"] == "ACTIVE" and st["status"] == "ACTIVE"
    assert st["paused_at"] is None


async def test_writeback_keeps_existing_paused_at(mock_db, meta):
    await _seed_agent(mock_db)
    await _seed_ad_state(mock_db)
    await mock_db[AD_STATE].update_one({"ad_id": "a1"}, {"$set": {"paused_at": "2026-01-01"}})
    await _seed_suggestion(mock_db)
    meta["status"]["a1"] = {"status": "ACTIVE", "effective_status": "ADSET_PAUSED"}
    await actions.apply_suggestion(mock_db, None, "u1", "sug_1")
    st = await mock_db[AD_STATE].find_one({"client_group_id": GID, "ad_id": "a1"})
    assert st["paused_at"] == "2026-01-01"
