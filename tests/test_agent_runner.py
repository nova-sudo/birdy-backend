"""
tests/test_agent_runner.py
--------------------------
run_agent: evaluates every enabled rule for every watched client, applies
memory filters, upserts work-list items (live only), auto-applies auto-mode
rules as Birdy, closes stale items per rule + client, and never crashes on one
bad client. Also: memory_filters.apply and the purger skipping agent clients.
"""

from datetime import date, datetime, timedelta

import pytest

from ai.agents import memory_filters, runner
from ai.agents.meta_state import MetaTokenError
from ai.agents.store import add_memory, create_agent, list_rules, update_agent, update_rule
from ai.suggestions import actions
from ai.suggestions import store as sstore
from ai.suggestions.contracts import Evidence, Finding, Action, ACTION_PAUSE_ADS

USER = "u1"
AS_OF = date(2026, 10, 25)  # a Sunday


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _insight(gid, ad_id, adset_id, day, spend, leads, ad_name=None):
    return {
        "user_id": USER, "client_group_id": gid, "ad_id": ad_id, "adset_id": adset_id,
        "campaign_id": f"c_{gid}", "ad_name": ad_name or f"Ad {ad_id}", "campaign_name": "Camp", "date_start": day.isoformat(),
        "updated_at": datetime(2026, 10, 25),
        "insight_data": {"spend": str(spend), "actions": [{"action_type": "lead", "value": str(leads)}]},
    }


async def _seed_client(db, gid, *, target=20, meta=True, last_day=AS_OF, extra_near=False, name=None):
    group = {"id": gid, "user_id": USER, "name": name or f"Client {gid}", "ad_account_currency": "GBP"}
    if target is not None:
        group["targets"] = {"cpl": target}
    if meta:
        group["meta_ad_account_id"] = f"act_{gid}"
    await db.client_groups.insert_one(group)

    ads = [(f"{gid}_bad", f"{gid}_s1", "Bad ad"), (f"{gid}_good", f"{gid}_s2", "Good ad")]
    if extra_near:
        ads.append((f"{gid}_near", f"{gid}_s3", "Near ad"))
    for ad_id, adset_id, ad_name in ads:
        await db.meta_ad_state.insert_one({
            "client_group_id": gid, "ad_id": ad_id, "adset_id": adset_id, "campaign_id": f"c_{gid}",
            "name": ad_name, "effective_status": "ACTIVE", "created_time": datetime(2026, 10, 1),
        })
    rows = []
    for i in range(10):
        d = last_day - timedelta(days=i)
        rows.append(_insight(gid, f"{gid}_bad", f"{gid}_s1", d, 30, 0, "Bad ad"))   # £210/7d, 0 leads
        rows.append(_insight(gid, f"{gid}_good", f"{gid}_s2", d, 10, 2, "Good ad"))  # CPL £5
        if extra_near:
            rows.append(_insight(gid, f"{gid}_near", f"{gid}_s3", d, 10, 0, "Near ad"))  # £70/7d, 0 leads
    await db.facebook_ad_insights.insert_many(rows)


async def _agent(db, gids):
    agent = await create_agent(db, USER)
    agent = await update_agent(db, USER, agent["_id"], {
        "status": "active",
        "scope": {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "Europe/London"}},
    })
    return agent


async def _rule(db, agent, kind):
    return next(r for r in await list_rules(db, agent["_id"]) if r["kind"] == kind)


@pytest.fixture
def meta_calls(monkeypatch):
    calls = []

    async def fake_refresh(db, mongo_client, user_id, gid, ad_account_id, *, today):
        calls.append(("refresh", gid, ad_account_id, today))
        return {"ads": 0, "adsets": 0}

    async def fake_backfill(db, gid):
        calls.append(("backfill", gid))
        return 0

    monkeypatch.setattr(runner, "refresh_meta_state", fake_refresh)
    monkeypatch.setattr(runner, "backfill_paused_at", fake_backfill)
    return calls


@pytest.fixture
def no_slack(monkeypatch):
    posted = []

    async def fake_post(db, user_id, docs):
        posted.extend(docs)
        return len(docs)

    monkeypatch.setattr("services.slack_suggestion_notifier.post_new_suggestions", fake_post)
    return posted


@pytest.fixture
def fake_apply(monkeypatch):
    state = {"calls": [], "outcome": "applied", "raise": False}

    async def _apply(db, mongo_client, user_id, suggestion_id, *, actor="user", source=None, by=None):
        state["calls"].append({"user_id": user_id, "id": suggestion_id, "actor": actor, "source": source})
        if state["raise"]:
            raise RuntimeError("boom")
        ok = state["outcome"] in ("applied", "already_in_meta", "partial", "already")
        if ok and state["outcome"] != "already":
            await db[sstore.SUGGESTIONS].update_one({"_id": suggestion_id}, {"$set": {"status": "applied"}})
        return {"ok": ok, "outcome": state["outcome"], "doc": None}

    monkeypatch.setattr(actions, "apply_suggestion", _apply)
    return state


def _docs(db, **q):
    return db[sstore.SUGGESTIONS].find(q).to_list(length=None)


# ---------------------------------------------------------------------------
# Brief tests
# ---------------------------------------------------------------------------

async def test_live_run_creates_items_with_agent_fields(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    docs = await _docs(mock_db, agent="media_buying:pause_over_target")
    assert len(docs) == 1
    d = docs[0]
    assert d["agent_id"] == agent["_id"] and d["rule_id"] == rule["_id"] and d["rule_version"] == 1
    assert d["window"] == "agent" and d["origin_window"] == "agent"
    assert d["status"] == "open" and d["client_group_id"] == "cg1"
    assert d["action"]["targets"][0]["object_id"] == "cg1_bad"
    assert d["rule_mode"] == "ask"
    assert res.created == 1 and res.auto_applied == 0 and res.stale is False
    assert [c[0] for c in meta_calls] == ["refresh", "backfill"]
    assert meta_calls[0][2] == "act_cg1"
    assert [p["_id"] for p in no_slack] == [d["_id"]]
    saved = await mock_db.agents.find_one({"_id": agent["_id"]})
    assert isinstance(saved["last_run_at"], datetime)
    # The activity row is an agent row.
    act = await mock_db[sstore.ACTIVITY].find_one({"kind": sstore.KIND_SUGGESTION_CREATED})
    assert act["agent_id"] == agent["_id"] and act["rule_id"] == rule["_id"]


async def test_auto_mode_rule_applies_as_birdy(mock_db, mock_mongo_client, meta_calls, no_slack, fake_apply):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert len(fake_apply["calls"]) == 1
    call = fake_apply["calls"][0]
    assert call["actor"] == "birdy" and call["source"] == "agent" and call["user_id"] == USER
    assert res.auto_applied == 1 and res.blocked == 0
    doc = await mock_db[sstore.SUGGESTIONS].find_one({"_id": call["id"]})
    assert doc["status"] == "applied" and not doc.get("auto_failed")


async def test_memory_filter_weekend_skip(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    await add_memory(mock_db, agent["_id"], text="Don't touch ads at weekends", kind="rule",
                     source="user", filter={"weekdays": [5, 6]})

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)  # Sunday
    assert not [f for f in res.findings if f.agent == "media_buying:pause_over_target"]
    assert await _docs(mock_db, agent="media_buying:pause_over_target") == []

    # Monday: the filter no longer matches.
    await mock_db.facebook_ad_insights.insert_one(
        _insight("cg1", "cg1_bad", "cg1_s1", AS_OF + timedelta(days=1), 30, 0, "Bad ad"))
    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF + timedelta(days=1))
    assert [f for f in res.findings if f.agent == "media_buying:pause_over_target"]


async def test_replay_mode_writes_nothing(mock_db, mock_mongo_client, meta_calls, no_slack, fake_apply):
    await _seed_client(mock_db, "cg1", extra_near=True)
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF, mode="replay", near_miss=True)

    assert any(f.agent == "media_buying:pause_over_target" and not f.near_miss for f in res.findings)
    assert any(f.near_miss for f in res.findings)  # near misses come back in replay
    assert await _docs(mock_db) == []
    assert await mock_db[sstore.ACTIVITY].count_documents({}) == 0
    assert meta_calls == [] and fake_apply["calls"] == [] and no_slack == []
    assert (await mock_db.agents.find_one({"_id": agent["_id"]}))["last_run_at"] is None
    assert res.created == 0 and res.auto_applied == 0


async def test_missing_target_flags_once_and_skips_target_rules(mock_db, mock_mongo_client, meta_calls,
                                                               no_slack, monkeypatch):
    await _seed_client(mock_db, "cg1", target=None, name="Acme")
    agent = await _agent(mock_db, ["cg1"])

    revive_calls = []

    def fake_revive(*a, **kw):
        revive_calls.append(a)
        return []

    monkeypatch.setitem(runner.REGISTRY, "revive_winner", fake_revive)

    for _ in range(2):
        res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
        adv = [f for f in res.findings if f.agent == "media_buying:missing_target"]
        assert len(adv) == 1
        assert adv[0].title == "Set a CPL target for Acme" and adv[0].action is None
        # The zero-lead branch still runs without a target.
        assert any(f.agent == "media_buying:pause_over_target" for f in res.findings)

    assert revive_calls == []
    docs = await _docs(mock_db, agent="media_buying:missing_target")
    assert len(docs) == 1 and docs[0]["status"] == "open" and docs[0]["agent_id"] == agent["_id"]


async def test_purger_skips_agent_clients(mock_db):
    from ai.suggestions.orchestrator import run_pass_for_user

    for gid in ("cg1", "cg2", "cg3", "cg4"):
        await mock_db.client_groups.insert_one({
            "id": gid, "user_id": USER, "name": gid,
            "facebook_cache": {"last_7d": {"ads": [{"ad_id": "x"}]}},
        })
    await mock_db.agents.insert_many([
        {"_id": "a1", "user_id": USER, "type": "media_buying", "status": "active",
         "scope": {"client_group_ids": ["cg1"]}},
        {"_id": "a2", "user_id": USER, "type": "other", "status": "dry_run",
         "scope": {"client_group_ids": ["cg2"]}},
        {"_id": "a3", "user_id": USER, "type": "third", "status": "draft",
         "scope": {"client_group_ids": ["cg3"]}},
        {"_id": "a4", "user_id": "someone-else", "type": "media_buying", "status": "active",
         "scope": {"client_group_ids": ["cg4"]}},
    ])
    seen = []

    class FakeAgent:
        name = "fake"

        async def analyze(self, ctx, group, window):
            seen.append(group["id"])
            return []

    await run_pass_for_user(mock_db, USER, "weekly", agents=[FakeAgent()], notify=False)
    assert sorted(seen) == ["cg3", "cg4"]


async def test_removed_meta_account_skipped_not_crash(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1", meta=False)
    await _seed_client(mock_db, "cg2")
    agent = await _agent(mock_db, ["cg1", "cg2"])

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert any(s.startswith("no_meta") for s in res.skipped)
    assert "no_meta:cg1" in res.skipped
    assert {f.client_group_id for f in res.findings} == {"cg2"}
    assert [c[1] for c in meta_calls if c[0] == "refresh"] == ["cg2"]


# ---------------------------------------------------------------------------
# Rulings 3, 5, 6, 7, 10
# ---------------------------------------------------------------------------

async def test_meta_token_and_other_errors_are_per_client(mock_db, mock_mongo_client, no_slack, monkeypatch):
    for gid in ("cg1", "cg2", "cg3"):
        await _seed_client(mock_db, gid)
    agent = await _agent(mock_db, ["cg1", "cg2", "cg3"])

    async def refresh(db, mongo_client, user_id, gid, acct, *, today):
        if gid == "cg1":
            raise MetaTokenError("expired")
        if gid == "cg2":
            raise RuntimeError("Meta Graph error")
        return {}

    async def backfill(db, gid):
        return 0

    monkeypatch.setattr(runner, "refresh_meta_state", refresh)
    monkeypatch.setattr(runner, "backfill_paused_at", backfill)

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert "meta_token:cg1" in res.skipped and "error:cg2" in res.skipped
    assert {f.client_group_id for f in res.findings} == {"cg3"}
    assert {d["client_group_id"] for d in await _docs(mock_db)} == {"cg3"}


async def test_live_never_upserts_near_misses(mock_db, mock_mongo_client, meta_calls, no_slack, monkeypatch):
    await _seed_client(mock_db, "cg1", extra_near=True)
    agent = await _agent(mock_db, ["cg1"])
    seen = []
    real = runner.REGISTRY["pause_over_target"]

    def spy(*a, near_miss=False):
        seen.append(near_miss)
        return real(*a, near_miss=near_miss)

    monkeypatch.setitem(runner.REGISTRY, "pause_over_target", spy)

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF, near_miss=True)

    assert seen == [False]
    assert not any(f.near_miss for f in res.findings)
    targets = [d["action"]["targets"][0]["object_id"] for d in await _docs(mock_db) if d.get("action")]
    assert "cg1_near" not in targets and "cg1_bad" in targets


async def test_stale_data_creates_items_but_never_auto_applies(mock_db, mock_mongo_client, meta_calls,
                                                              no_slack, fake_apply):
    await _seed_client(mock_db, "cg1", last_day=AS_OF - timedelta(days=3))
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert res.stale is True
    assert fake_apply["calls"] == [] and res.auto_applied == 0
    docs = await _docs(mock_db, agent="media_buying:pause_over_target")
    assert len(docs) == 1 and docs[0]["status"] == "open"


async def test_force_stale_blocks_auto_apply_on_fresh_data(mock_db, mock_mongo_client, meta_calls,
                                                          no_slack, fake_apply):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF, force_stale=True)

    assert res.stale is True and res.created >= 1
    assert fake_apply["calls"] == [] and res.auto_applied == 0


async def test_fresh_data_is_not_stale(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1", last_day=AS_OF - timedelta(days=1))
    agent = await _agent(mock_db, ["cg1"])
    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
    assert res.stale is False


@pytest.mark.parametrize("outcome,raises,blocked,applied,failed", [
    ("blocked", False, 1, 0, False),
    ("already_in_meta", False, 0, 1, False),
    ("failed", False, 0, 0, True),
    ("busy", False, 0, 0, True),
    (None, True, 0, 0, True),
])
async def test_auto_apply_outcomes(mock_db, mock_mongo_client, meta_calls, no_slack, fake_apply,
                                   outcome, raises, blocked, applied, failed):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")
    fake_apply["outcome"] = outcome
    fake_apply["raise"] = raises

    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert res.blocked == blocked and res.auto_applied == applied
    doc = (await _docs(mock_db, agent="media_buying:pause_over_target"))[0]
    assert bool(doc.get("auto_failed")) is failed
    if failed:
        assert doc["status"] == "open"


async def test_auto_failed_item_not_retried(mock_db, mock_mongo_client, meta_calls, no_slack, fake_apply):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")
    fake_apply["outcome"] = "failed"
    await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
    await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
    assert len(fake_apply["calls"]) == 1


async def test_disabled_rule_skipped(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], enabled=False)
    res = await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
    assert not [f for f in res.findings if f.agent == "media_buying:pause_over_target"]


async def test_close_stale_scoped_per_rule_and_client(mock_db, mock_mongo_client, meta_calls, no_slack):
    await _seed_client(mock_db, "cg1")
    await _seed_client(mock_db, "cg2", meta=False)  # skipped this run
    agent = await _agent(mock_db, ["cg1", "cg2"])
    base = {"user_id": USER, "status": "open", "origin_window": "agent", "window": "agent",
            "agent_id": agent["_id"]}
    await mock_db[sstore.SUGGESTIONS].insert_many([
        {**base, "_id": "old_cg1", "client_group_id": "cg1", "agent": "media_buying:pause_over_target"},
        {**base, "_id": "old_cg2", "client_group_id": "cg2", "agent": "media_buying:pause_over_target"},
        {**base, "_id": "user_cg1", "client_group_id": "cg1", "agent": "useless_ad_purger",
         "origin_window": "weekly"},
        {**base, "_id": "other_agent", "client_group_id": "cg1", "agent": "media_buying:pause_over_target",
         "agent_id": "someone-else"},
    ])

    await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    status = {d["_id"]: d["status"] for d in await _docs(mock_db)}
    assert status["old_cg1"] == "resolved"
    assert status["old_cg2"] == "open"       # client was skipped: don't close its items
    assert status["user_cg1"] == "open"      # non-agent item
    assert status["other_agent"] == "open"   # another agent's item


# ---------------------------------------------------------------------------
# memory_filters.apply (pure)
# ---------------------------------------------------------------------------

def _f(gid="cg1", name="Spring Video", title=None):
    return Finding(
        agent="media_buying:pause_over_target", client_group_id=gid, client_name="C",
        severity="HIGH", title=title or f"Pause “{name}”", description="", evidence=Evidence(window="agent"),
        action=Action(type=ACTION_PAUSE_ADS, targets=[{"object_id": "a1", "object_type": "ad", "name": name}]),
    )


def test_memory_filters_and_semantics():
    sunday = AS_OF
    fs = [_f("cg1", "Spring VIDEO"), _f("cg2", "Spring video"), _f("cg1", "Static")]
    mems = [{"filter": {"client_group_id": "cg1", "ad_name_contains": "video"}}]
    out = memory_filters.apply(fs, mems, as_of=sunday)
    assert [(f.client_group_id, f.action.targets[0]["name"]) for f in out] == [
        ("cg2", "Spring video"), ("cg1", "Static")]

    # weekdays ANDed with client
    mems = [{"filter": {"client_group_id": "cg2", "weekdays": [6]}}]
    assert len(memory_filters.apply(fs, mems, as_of=sunday)) == 2
    assert len(memory_filters.apply(fs, mems, as_of=sunday - timedelta(days=1))) == 3

    # Memories without a filter (or an empty one) exclude nothing.
    assert len(memory_filters.apply(fs, [{"filter": None}, {"filter": {}}, {}], as_of=sunday)) == 3


def test_memory_filter_name_falls_back_to_title_without_targets():
    f = Finding(agent="media_buying:flag_no_spend", client_group_id="cg1", client_name="C",
                severity="MEDIUM", title="“Evergreen set” spent £0 for 24 hrs", description="",
                evidence=Evidence(window="agent"))
    assert memory_filters.apply([f], [{"filter": {"ad_name_contains": "EVERGREEN"}}], as_of=AS_OF) == []


# ---------------------------------------------------------------------------
# Fix round 1: undone items are never auto-applied again (I2); M2
# ---------------------------------------------------------------------------

async def test_undone_auto_item_not_reapplied(mock_db, mock_mongo_client, meta_calls, no_slack, fake_apply):
    await _seed_client(mock_db, "cg1")
    agent = await _agent(mock_db, ["cg1"])
    rule = await _rule(mock_db, agent, "pause_over_target")
    await update_rule(mock_db, agent["_id"], rule["_id"], mode="auto")

    await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)
    assert len(fake_apply["calls"]) == 1
    sid = fake_apply["calls"][0]["id"]
    assert await sstore.mark_undone(mock_db, USER, sid, undone_by={"actor": "user"})

    await runner.run_agent(mock_db, mock_mongo_client, agent, as_of=AS_OF)

    assert len(fake_apply["calls"]) == 1
    doc = await mock_db[sstore.SUGGESTIONS].find_one({"_id": sid})
    assert doc["status"] == "open" and doc.get("undone_at")


async def test_reupsert_keeps_undone_at_and_refreshes_rule_version(mock_db):
    f = _f()
    f.agent_id, f.rule_id, f.rule_version = "ag", "r1", 1
    doc, created, _ = await sstore.upsert_finding(mock_db, USER, f)
    assert created
    await mock_db[sstore.SUGGESTIONS].update_one({"_id": doc["_id"]}, {"$set": {"status": "applied"}})
    assert await sstore.mark_undone(mock_db, USER, doc["_id"])

    f2 = _f()
    f2.agent_id, f2.rule_id, f2.rule_version = "ag", "r1", 2
    doc2, created2, changed2 = await sstore.upsert_finding(mock_db, USER, f2)
    assert not created2 and changed2
    saved = await mock_db[sstore.SUGGESTIONS].find_one({"_id": doc["_id"]})
    assert saved["undone_at"] and doc2["undone_at"] == saved["undone_at"]
    assert saved["rule_version"] == 2
