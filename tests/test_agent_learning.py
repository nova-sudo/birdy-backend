"""
tests/test_agent_learning.py
----------------------------
The learning engine (ai/agents/learning.py): streaks, graduation offers, the
brake, memory from deny notes, and rule-change proposals — plus the hooks in
ai/suggestions/actions.py and the routes in routers/agents.py. Meta and the
LLM are stubbed; nothing touches the network.
"""

import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import ai.suggestions.actions as actions_mod
import routers.agents as agents_router
import routers.dashboard as dashboard_router
from ai.agents import learning, replay
from ai.agents.store import (
    PROPOSALS,
    RULE_VERSIONS,
    RULES,
    create_agent,
    list_memory,
    update_agent,
)
from ai.providers.base import ProviderResponse
from ai.suggestions import actions, store
from dependencies import get_current_user
from jobs import agent_jobs
from main import app

U = "u1@example.com"
OTHER = "u2@example.com"
NOW = datetime(2026, 10, 26, 9, 0)


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

@pytest.fixture
def meta(monkeypatch):
    writes = []

    async def fake_get(user_id, object_id, object_type, mongo_client):
        return {"status": "ACTIVE", "effective_status": "ACTIVE"}

    async def fake_set(user_id, object_id, object_type, status, mongo_client):
        writes.append((object_id, status))
        return {"success": True}

    monkeypatch.setattr(actions_mod, "get_object_state", fake_get)
    monkeypatch.setattr(actions_mod, "set_object_status", fake_set)
    return writes


@pytest.fixture
def api(mock_mongo_client, monkeypatch, meta):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(dashboard_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


class FakeProvider:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def chat_completion(self, messages, tools=None, tool_choice="auto", temperature=0.3, max_tokens=2048):
        self.calls.append(messages)
        if self.error:
            raise self.error
        return ProviderResponse(content=self.reply)


def use_provider(monkeypatch, provider):
    async def for_user(user_id, db):
        return provider

    monkeypatch.setattr("ai.provider_factory.get_provider_for_user", for_user)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Any LLM call not set up by a test fails (and must fall back to a fact)."""
    use_provider(monkeypatch, FakeProvider(error=RuntimeError("no network in tests")))


async def seed_agent(db, user=U, gids=("g1",)):
    agent = await create_agent(db, user)
    agent = await update_agent(db, user, agent["_id"], {
        "status": "active",
        "scope": {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "Europe/London"}},
    })
    rules = {r["kind"]: r for r in await db[RULES].find({"agent_id": agent["_id"]}).to_list(length=None)}
    return agent, rules


async def rule_doc(db, rule_id):
    return await db[RULES].find_one({"_id": rule_id})


async def seed_suggestion(db, agent, rule, sid, *, status=store.STATUS_OPEN, gid="g1", raw=None,
                          decided_by=None, applied_at=None, object_id=None, user=U, rule_mode=None):
    doc = {
        "_id": sid, "id": sid, "user_id": user, "client_name": gid, "client_group_id": gid,
        "agent": f"media_buying:{rule['kind']}", "title": f"Item {sid}", "description": "d",
        "stats": [], "window": "agent", "origin_window": "agent",
        "action": {"type": "pause_ads" if rule["kind"] == "pause_over_target" else "enable_ads", "params": {},
                   "targets": [{"object_id": object_id or f"ad_{sid}", "object_type": "ad", "name": f"Ad {sid}"}]},
        "evidence": raw or {}, "status": status, "created_at": NOW, "updated_at": NOW,
        "agent_id": agent["_id"], "rule_id": rule["_id"], "rule_version": 1,
        "rule_mode": rule_mode or rule["mode"],
    }
    if status == store.STATUS_APPLIED:
        doc["applied_at"] = applied_at or datetime.utcnow()
        doc["applied_targets"] = doc["action"]["targets"]
        doc["applied_action_type"] = doc["action"]["type"]
        doc["decided_by"] = decided_by or {"actor": store.ACTOR_USER}
    await db[store.SUGGESTIONS].insert_one(doc)
    return doc


async def deny(db, agent, rule, sid, reason, *, raw=None, gid="g1", at=None):
    """A denied agent item: the suggestion plus its dismissed activity row."""
    await seed_suggestion(db, agent, rule, sid, status=store.STATUS_DISMISSED, raw=raw, gid=gid)
    row = await store.log_activity(
        db, agent["user_id"], actor=store.ACTOR_USER, kind=store.KIND_SUGGESTION_DISMISSED,
        title=f"Dismissed: {sid}", client_group_id=gid, ref_id=sid, agent_id=agent["_id"],
        rule_id=rule["_id"], reason_code=reason)
    await db[store.ACTIVITY].update_one({"_id": row["_id"]}, {"$set": {"created_at": at or NOW}})


@pytest.fixture
def fake_replay(monkeypatch):
    """Replay stub: before fires on every target given, after fires on none."""
    state = {"before": set(), "after": set(), "calls": []}

    async def fired(db, agent, variants, *, end, days=30):
        state["calls"].append([v.get("params") for v in variants])
        return [set(state["before"]), set(state["after"])]

    monkeypatch.setattr(replay, "fired_targets", fired)
    return state


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

async def test_streak_and_last10(mock_db):
    agent, rules = await seed_agent(mock_db)
    rid = rules["pause_over_target"]["_id"]
    for _ in range(12):
        await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    stats = (await rule_doc(mock_db, rid))["stats"]
    assert stats["streak"] == 12 and stats["approved"] == 12
    assert stats["last10"] == ["ok"] * 10

    await learning.record_decision(mock_db, agent["_id"], rid, "denied", reason_code="other", now=NOW)
    stats = (await rule_doc(mock_db, rid))["stats"]
    assert stats["streak"] == 0 and stats["denied"] == 1 and stats["last10"][-1] == "no"
    assert len(stats["last10"]) == 10

    await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    await learning.record_decision(mock_db, agent["_id"], rid, "undone_user", now=NOW)
    stats = (await rule_doc(mock_db, rid))["stats"]
    assert stats["streak"] == 0 and stats["last10"][-2:] == ["ok", "undo"]
    assert stats["undone_auto"] == []


# ---------------------------------------------------------------------------
# Graduation
# ---------------------------------------------------------------------------

async def test_graduation_offer_at_10_then_keep_asking_offers_at_20(mock_db):
    agent, rules = await seed_agent(mock_db)
    rid = rules["pause_over_target"]["_id"]
    for _ in range(9):
        await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    assert await learning.pending_offers(mock_db, agent) == []

    await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    offers = await learning.pending_offers(mock_db, agent)
    assert [(o["type"], o["rule_id"]) for o in offers] == [("graduation", rid)]
    assert offers[0]["streak"] == 10

    await learning.graduate(mock_db, agent, rid, "keep_asking", now=NOW)
    rule = await rule_doc(mock_db, rid)
    assert rule["mode"] == "ask" and rule["stats"]["offer_after"] == 20
    assert await learning.pending_offers(mock_db, agent) == []

    for _ in range(9):
        await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    assert await learning.pending_offers(mock_db, agent) == []
    await learning.record_decision(mock_db, agent["_id"], rid, "approved", now=NOW)
    assert [o["type"] for o in await learning.pending_offers(mock_db, agent)] == ["graduation"]

    await seed_suggestion(mock_db, agent, rules["pause_over_target"], "open1")
    await learning.graduate(mock_db, agent, rid, "auto_notify", now=NOW)
    rule = await rule_doc(mock_db, rid)
    assert rule["mode"] == "auto" and rule["notify"] is True and rule["auto_since"] == NOW
    assert (await mock_db[store.SUGGESTIONS].find_one({"_id": "open1"}))["rule_mode"] == "auto"
    assert await learning.pending_offers(mock_db, agent) == []


async def test_graduate_without_offer_is_refused(mock_db):
    agent, rules = await seed_agent(mock_db)
    with pytest.raises(learning.NoOffer):
        await learning.graduate(mock_db, agent, rules["pause_over_target"]["_id"], "auto_silent", now=NOW)


# ---------------------------------------------------------------------------
# Brake
# ---------------------------------------------------------------------------

async def _auto_rule(db, agent, rid):
    await db[RULES].update_one({"_id": rid}, {"$set": {"stats.streak": 10}})
    await learning.graduate(db, agent, rid, "auto_silent", now=NOW - timedelta(days=20))


async def test_brake_after_two_auto_undos_in_7_days(mock_db):
    agent, rules = await seed_agent(mock_db)
    rid = rules["pause_over_target"]["_id"]
    await _auto_rule(mock_db, agent, rid)
    await seed_suggestion(mock_db, agent, rules["pause_over_target"], "open1", rule_mode="auto")

    # One undo 8 days ago and one now: not within 7 days of each other.
    await learning.record_decision(mock_db, agent["_id"], rid, "undone_auto", now=NOW - timedelta(days=8))
    await learning.record_decision(mock_db, agent["_id"], rid, "undone_auto", now=NOW)
    assert (await rule_doc(mock_db, rid))["mode"] == "auto"
    assert await learning.pending_offers(mock_db, agent) == []

    res = await learning.record_decision(mock_db, agent["_id"], rid, "undone_auto", now=NOW + timedelta(hours=1))
    assert res["brake"] is True
    rule = await rule_doc(mock_db, rid)
    assert rule["mode"] == "ask" and len(rule["stats"]["undone_auto"]) == 3
    assert (await mock_db[store.SUGGESTIONS].find_one({"_id": "open1"}))["rule_mode"] == "ask"
    offers = await learning.pending_offers(mock_db, agent)
    assert [(o["type"], o["rule_id"]) for o in offers] == [("brake", rid)]
    explained = await mock_db[store.ACTIVITY].find_one({"kind": learning.KIND_BRAKE})
    assert explained and explained["rule_id"] == rid and explained["agent_id"] == agent["_id"]

    # A further undo does not brake twice.
    again = await learning.record_decision(mock_db, agent["_id"], rid, "undone_auto", now=NOW + timedelta(hours=2))
    assert again["brake"] is False
    assert len((await mock_db.agents.find_one({"_id": agent["_id"]}))["offers"]) == 1

    # Picking a choice dismisses the brake notice.
    await learning.graduate(mock_db, agent, rid, "keep_asking", now=NOW + timedelta(hours=3))
    assert await learning.pending_offers(mock_db, agent) == []


# ---------------------------------------------------------------------------
# Hooks in actions.py
# ---------------------------------------------------------------------------

async def test_apply_records_approved_once_and_already_does_not_double_count(mock_db, mock_mongo_client, meta):
    agent, rules = await seed_agent(mock_db)
    rule = rules["pause_over_target"]
    await seed_suggestion(mock_db, agent, rule, "s1")
    res = await actions.apply_suggestion(mock_db, mock_mongo_client, U, "s1", actor=store.ACTOR_USER)
    assert res["outcome"] == "applied"
    again = await actions.apply_suggestion(mock_db, mock_mongo_client, U, "s1", actor=store.ACTOR_USER)
    assert again["outcome"] == "already"
    stats = (await rule_doc(mock_db, rule["_id"]))["stats"]
    assert stats["streak"] == 1 and stats["approved"] == 1 and stats["last10"] == ["ok"]

    # Birdy's own auto-apply is not a user approval.
    await seed_suggestion(mock_db, agent, rule, "s2")
    await actions.apply_suggestion(mock_db, mock_mongo_client, U, "s2", actor=store.ACTOR_BIRDY)
    assert (await rule_doc(mock_db, rule["_id"]))["stats"]["approved"] == 1


async def test_learning_failure_never_fails_the_decision(mock_db, mock_mongo_client, meta, monkeypatch):
    agent, rules = await seed_agent(mock_db)
    rule = rules["pause_over_target"]
    await seed_suggestion(mock_db, agent, rule, "s1")
    await seed_suggestion(mock_db, agent, rule, "s2")

    async def boom(*a, **k):
        raise RuntimeError("learning down")

    monkeypatch.setattr(learning, "record_decision", boom)
    assert (await actions.apply_suggestion(mock_db, mock_mongo_client, U, "s1"))["outcome"] == "applied"
    assert (await actions.undo_suggestion(mock_db, mock_mongo_client, U, "s1"))["outcome"] == "undone"
    assert (await actions.dismiss_suggestion(mock_db, mock_mongo_client, U, "s2",
                                             reason_code="other", note="x"))["outcome"] == "dismissed"


async def test_deny_records_denied_and_note_becomes_memory(mock_db, mock_mongo_client, monkeypatch):
    agent, rules = await seed_agent(mock_db)
    rule = rules["pause_over_target"]
    await seed_suggestion(mock_db, agent, rule, "s1")
    provider = FakeProvider(reply='{"kind": "exception", "text": "Leave g1 alone", '
                                  '"filter": {"client_group_id": "g1"}}')
    use_provider(monkeypatch, provider)
    res = await actions.dismiss_suggestion(mock_db, mock_mongo_client, U, "s1",
                                           reason_code="client_wants_on", note="client wants these on")
    assert res["outcome"] == "dismissed"
    assert res["memory"]["kind"] == "exception" and res["memory"]["filter"] == {"client_group_id": "g1"}
    stats = (await rule_doc(mock_db, rule["_id"]))["stats"]
    assert stats["denied"] == 1 and stats["last10"] == ["no"]
    mem = await list_memory(mock_db, agent["_id"])
    assert len(mem) == 1 and mem[0]["source"] == "denial" and mem[0]["source_ref"] == "s1"

    # Dismissing again (nothing changes) does not count a second denial.
    await actions.dismiss_suggestion(mock_db, mock_mongo_client, U, "s1", reason_code="other")
    assert (await rule_doc(mock_db, rule["_id"]))["stats"]["denied"] == 1


async def test_undo_stamps_agent_and_rule_and_records_by_actor(mock_db, mock_mongo_client, meta):
    agent, rules = await seed_agent(mock_db)
    rule = rules["pause_over_target"]
    await seed_suggestion(mock_db, agent, rule, "u_user", status=store.STATUS_APPLIED,
                          decided_by={"actor": store.ACTOR_USER})
    await seed_suggestion(mock_db, agent, rule, "u_auto", status=store.STATUS_APPLIED,
                          decided_by={"actor": store.ACTOR_BIRDY})
    assert (await actions.undo_suggestion(mock_db, mock_mongo_client, U, "u_user"))["outcome"] == "undone"
    assert (await actions.undo_suggestion(mock_db, mock_mongo_client, U, "u_auto"))["outcome"] == "undone"
    # A second undo is a no-op and records nothing.
    assert (await actions.undo_suggestion(mock_db, mock_mongo_client, U, "u_auto"))["outcome"] == "noop"

    rows = await mock_db[store.ACTIVITY].find({"kind": store.KIND_ACTION_UNDONE}).to_list(length=None)
    assert len(rows) == 2
    assert all(r["agent_id"] == agent["_id"] and r["rule_id"] == rule["_id"] for r in rows)
    stats = (await rule_doc(mock_db, rule["_id"]))["stats"]
    assert stats["last10"] == ["undo", "undo"] and len(stats["undone_auto"]) == 1


# ---------------------------------------------------------------------------
# undo-auto route
# ---------------------------------------------------------------------------

async def test_undo_auto_410_after_24h(api, mock_db):
    agent, rules = await seed_agent(mock_db)
    rule = rules["pause_over_target"]
    birdy = {"actor": store.ACTOR_BIRDY, "source": "agent"}
    await seed_suggestion(mock_db, agent, rule, "old", status=store.STATUS_APPLIED, decided_by=birdy,
                          applied_at=datetime.utcnow() - timedelta(hours=25))
    await seed_suggestion(mock_db, agent, rule, "fresh", status=store.STATUS_APPLIED, decided_by=birdy,
                          applied_at=datetime.utcnow() - timedelta(hours=1))
    await seed_suggestion(mock_db, agent, rule, "mine", status=store.STATUS_APPLIED,
                          decided_by={"actor": store.ACTOR_USER})
    base = f"/api/agents/{agent['_id']}/suggestions"

    assert api.post(f"{base}/old/undo-auto").status_code == 410
    assert (await mock_db[store.SUGGESTIONS].find_one({"_id": "old"}))["status"] == store.STATUS_APPLIED
    assert api.post(f"{base}/mine/undo-auto").status_code == 409
    assert api.post(f"{base}/nope/undo-auto").status_code == 404

    r = api.post(f"{base}/fresh/undo-auto")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "undone"
    doc = await mock_db[store.SUGGESTIONS].find_one({"_id": "fresh"})
    assert doc["status"] == store.STATUS_OPEN and doc.get("undone_at")
    assert len((await rule_doc(mock_db, rule["_id"]))["stats"]["undone_auto"]) == 1

    api.state["user"] = OTHER
    assert api.post(f"{base}/fresh/undo-auto").status_code == 404


async def test_offers_and_graduate_routes(api, mock_db):
    agent, rules = await seed_agent(mock_db)
    rid = rules["revive_winner"]["_id"]
    base = f"/api/agents/{agent['_id']}"
    assert api.get(f"{base}/offers").json() == []
    assert api.post(f"{base}/rules/{rid}/graduate", json={"choice": "auto_silent"}).status_code == 409
    await mock_db[RULES].update_one({"_id": rid}, {"$set": {"stats.streak": 10}})
    offers = api.get(f"{base}/offers").json()
    assert offers[0]["id"] == f"graduation:{rid}" and offers[0]["rule_kind"] == "revive_winner"
    assert api.post(f"{base}/rules/{rid}/graduate", json={"choice": "nope"}).status_code == 422
    r = api.post(f"{base}/rules/{rid}/graduate", json={"choice": "auto_silent"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "auto" and r.json()["notify"] is False
    api.state["user"] = OTHER
    assert api.get(f"{base}/offers").status_code == 404
    assert api.post(f"{base}/rules/{rid}/graduate", json={"choice": "keep_asking"}).status_code == 404


# ---------------------------------------------------------------------------
# Memory from notes
# ---------------------------------------------------------------------------

async def test_note_falls_back_to_fact_on_llm_error(mock_db, monkeypatch):
    agent, rules = await seed_agent(mock_db)
    doc = await seed_suggestion(mock_db, agent, rules["pause_over_target"], "s1")

    # The autouse provider raises.
    mem = await learning.memory_from_note(mock_db, agent, "Acme likes video ads", doc)
    assert mem["kind"] == "fact" and mem["text"] == "Acme likes video ads" and mem.get("filter") is None
    assert mem["source"] == "denial"

    for reply in ("not json at all", '{"kind": "exception", "filter": {"bogus_key": 1}}', None):
        use_provider(monkeypatch, FakeProvider(reply=reply))
        assert (await learning.memory_from_note(mock_db, agent, "note", doc))["kind"] == "fact"

    async def no_provider(user_id, db):
        raise RuntimeError("no key")

    monkeypatch.setattr("ai.provider_factory.get_provider_for_user", no_provider)
    assert (await learning.memory_from_note(mock_db, agent, "note", doc))["kind"] == "fact"

    use_provider(monkeypatch, FakeProvider(
        reply='```json\n{"kind": "exception", "filter": {"weekdays": [5, 6]}}\n```'))
    mem = await learning.memory_from_note(mock_db, agent, "not at weekends", doc)
    assert mem["kind"] == "exception" and mem["filter"] == {"weekdays": [5, 6]}
    assert len(await list_memory(mock_db, agent["_id"])) == 6


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------

async def test_creative_stale_proposal_values(mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    rule = rules["revive_winner"]
    for sid, age in (("d1", 64), ("d2", 71), ("d3", 83)):
        await deny(mock_db, agent, rule, sid, "creative_stale", raw={"creative_age_days": age})
    fake_replay["before"] = {("g1", "ad_d1"), ("g1", "ad_d2"), ("g1", "ad_x")}

    out = await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)
    assert len(out) == 1
    p = out[0]
    assert p["rule_id"] == rule["_id"] and p["kind"] == "param_change" and p["status"] == "open"
    assert p["before"] == {"max_creative_age_days": 90} and p["after"] == {"max_creative_age_days": 60}
    assert p["evidence"]["reason_code"] == "creative_stale" and p["evidence"]["count"] == 3
    assert sorted(p["evidence"]["item_ids"]) == ["d1", "d2", "d3"]
    assert p["replay_effect"] == {"removed": 3, "of": 3, "removed_were_denied": 2}
    assert fake_replay["calls"][0][1]["max_creative_age_days"] == 60


async def test_still_learning_and_target_wrong_values(mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db, gids=("g1", "g2"))
    rule = rules["pause_over_target"]
    for i in range(3):
        await deny(mock_db, agent, rule, f"sl{i}", "still_learning")
    # g2 has the most target_wrong denials; its worst multiple is 64/30 = 2.133 -> 2.2.
    for sid, gid, cpl in (("t1", "g2", 50), ("t2", "g2", 64), ("t3", "g2", 45), ("t4", "g1", 90)):
        await deny(mock_db, agent, rule, sid, "target_wrong", gid=gid, raw={"cpl": cpl, "target_cpl": 30})
    await deny(mock_db, agent, rule, "o1", "other")
    await deny(mock_db, agent, rule, "o2", "other")
    await deny(mock_db, agent, rule, "o3", "other")

    out = {p["reason_code"]: p for p in await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)}
    assert set(out) == {"still_learning", "target_wrong"}
    assert out["still_learning"]["before"] == {"patience_spend": 60, "patience_days": 3}
    assert out["still_learning"]["after"] == {"patience_spend": 90, "patience_days": 5}
    tw = out["target_wrong"]
    assert tw["kind"] == "client_override" and tw["client_group_id"] == "g2"
    assert tw["before"] == {"cpl_multiple": 1.5} and tw["after"] == {"cpl_multiple": 2.2}


async def test_old_denials_do_not_count(mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    rule = rules["revive_winner"]
    await deny(mock_db, agent, rule, "d1", "creative_stale", raw={"creative_age_days": 64},
               at=NOW - timedelta(days=15))
    await deny(mock_db, agent, rule, "d2", "creative_stale", raw={"creative_age_days": 71})
    await deny(mock_db, agent, rule, "d3", "creative_stale", raw={"creative_age_days": 83})
    assert await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW) == []


async def test_proposal_cooldown_and_doubling(mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    rule = rules["revive_winner"]
    for i in range(3):
        await deny(mock_db, agent, rule, f"d{i}", "creative_stale", raw={"creative_age_days": 64 + i})
    first = await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)
    assert len(first) == 1
    # Open proposal: nothing new.
    assert await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW) == []

    rejected = await learning.reject_proposal(mock_db, agent, first[0]["_id"], now=NOW)
    assert rejected["status"] == "rejected" and rejected["count_at_rejection"] == 3
    assert await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW) == []

    for i in range(3, 5):  # 5 denials: under double
        await deny(mock_db, agent, rule, f"d{i}", "creative_stale", raw={"creative_age_days": 64 + i})
    assert await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW) == []

    await deny(mock_db, agent, rule, "d5", "creative_stale", raw={"creative_age_days": 70})
    again = await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)
    assert len(again) == 1 and again[0]["evidence"]["count"] == 6
    assert await mock_db[PROPOSALS].count_documents({"agent_id": agent["_id"]}) == 2


async def test_rejected_proposal_returns_after_30_days(mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    rule = rules["revive_winner"]
    for i in range(3):
        await deny(mock_db, agent, rule, f"d{i}", "creative_stale", raw={"creative_age_days": 64 + i})
    first = await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)
    await learning.reject_proposal(mock_db, agent, first[0]["_id"], now=NOW - timedelta(days=31))
    later = NOW + timedelta(days=1)
    await mock_db[store.ACTIVITY].update_many({}, {"$set": {"created_at": later}})
    assert len(await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=later)) == 1


async def test_accept_applies_and_versions_rule(api, mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    rule = rules["revive_winner"]
    for sid, age in (("d1", 64), ("d2", 71), ("d3", 83)):
        await deny(mock_db, agent, rule, sid, "creative_stale", raw={"creative_age_days": age})
    [p] = await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)
    base = f"/api/agents/{agent['_id']}/proposals"

    listed = api.get(base).json()
    assert [x["id"] for x in listed] == [p["_id"]]
    assert listed[0]["rule_kind"] == "revive_winner" and listed[0]["after"] == {"max_creative_age_days": 60}

    r = api.post(f"{base}/{p['_id']}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["status"] == "accepted"
    updated = await rule_doc(mock_db, rule["_id"])
    assert updated["version"] == 2 and updated["params"]["max_creative_age_days"] == 60
    assert updated["params"]["min_leads"] == 5
    versions = await mock_db[RULE_VERSIONS].find({"rule_id": rule["_id"]}).to_list(length=None)
    assert len(versions) == 1 and versions[0]["params"]["max_creative_age_days"] == 90
    logged = await mock_db[store.ACTIVITY].find_one({"kind": learning.KIND_PROPOSAL_ACCEPTED})
    assert logged and logged["rule_id"] == rule["_id"] and logged["agent_id"] == agent["_id"]

    assert api.post(f"{base}/{p['_id']}/accept").status_code == 409
    assert api.post(f"{base}/{p['_id']}/reject").status_code == 409
    assert api.get(base).json() == []
    assert len(api.get(f"{base}?status=all").json()) == 1
    api.state["user"] = OTHER
    assert api.get(base).status_code == 404
    assert api.post(f"{base}/{p['_id']}/reject").status_code == 404


async def test_accept_client_override_and_reject_route(api, mock_db, mock_mongo_client, fake_replay):
    agent, rules = await seed_agent(mock_db)
    await mock_db.client_groups.insert_one({"id": "g1", "user_id": U, "name": "g1"})
    rule = rules["pause_over_target"]
    for i, cpl in enumerate((50, 64, 45)):
        await deny(mock_db, agent, rule, f"t{i}", "target_wrong", raw={"cpl": cpl, "target_cpl": 30})
    for i in range(3):
        await deny(mock_db, agent, rule, f"s{i}", "still_learning")
    props = {p["reason_code"]: p for p in await learning.scan_proposals(mock_db, mock_mongo_client, agent, now=NOW)}
    base = f"/api/agents/{agent['_id']}/proposals"
    assert api.post(f"{base}/{props['target_wrong']['_id']}/accept").status_code == 200
    updated = await rule_doc(mock_db, rule["_id"])
    assert updated["client_overrides"] == {"g1": {"cpl_multiple": 2.2}} and updated["version"] == 2

    r = api.post(f"{base}/{props['still_learning']['_id']}/reject")
    assert r.status_code == 200 and r.json()["proposal"]["status"] == "rejected"
    assert (await rule_doc(mock_db, rule["_id"]))["params"]["patience_spend"] == 60


# ---------------------------------------------------------------------------
# Replay effect (real replay over the shared fixture account)
# ---------------------------------------------------------------------------

async def test_replay_effect_counts_removed_targets(mock_db, mock_mongo_client):
    from tests.test_agent_replay import END, _agent, _fixture_account

    await _fixture_account(mock_db)
    agent = await _agent(mock_db, ["g1"])
    rule = await mock_db[RULES].find_one({"agent_id": agent["_id"], "kind": "revive_winner"})
    changed = {**rule, "params": {**rule["params"], "max_creative_age_days": 60}}
    denied = [{"client_group_id": "g1", "action": {"targets": [{"object_id": "R1"}]}}]
    eff = await learning.replay_effect(mock_db, mock_mongo_client, agent, rule, changed, denied, end=END)
    # R1's creative is 61 days old when it would revive: a 60-day cap removes it.
    assert eff == {"removed": 1, "of": 1, "removed_were_denied": 1}


# ---------------------------------------------------------------------------
# agents-tick runs scan_proposals, and a failure there never fails the run
# ---------------------------------------------------------------------------

async def test_tick_scans_proposals_and_survives_failure(mock_db, mock_mongo_client, monkeypatch):
    from ai.agents import runner

    agent, _ = await seed_agent(mock_db, user="u1")
    await mock_db.client_groups.insert_one({"id": "g1", "user_id": "u1", "meta_ad_account_id": "act_1",
                                            "last_meta_refresh": datetime(2026, 10, 26, 11, 0)})

    async def fake_run(db, mongo_client, agent, **kw):
        return runner.RunResult()

    scanned = []

    async def boom(db, mongo_client, agent, *, now):
        scanned.append(agent["_id"])
        raise RuntimeError("scan failed")

    monkeypatch.setattr(agent_jobs, "run_agent", fake_run)
    monkeypatch.setattr(learning, "scan_proposals", boom)
    from datetime import timezone
    res = await agent_jobs.run_due_agents(mock_mongo_client, now_utc=datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
    assert res["ran"] == 1 and res["errors"] == 0 and scanned == [agent["_id"]]
    assert (await mock_db.agents.find_one({"_id": agent["_id"]}))["last_run"]["status"] == "ok"
