"""
tests/test_agent_confidence.py
------------------------------
Confidence score (coverage + Wilson-bounded practice), practice cases, the
user_right re-score after a rule edit, the post-launch rolling score, and the
activation gate. All data is built through mock_db inserts; no network.
"""

import contextlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
from ai.agents import confidence
from ai.agents.confidence import compute_confidence, wilson_lower
from ai.agents.store import PRACTICE, create_agent, list_rules, update_agent
from ai.suggestions import store as sstore
from dependencies import get_current_user
from main import app
from tests.test_agent_replay import END, USER, _fixture_account

FULL_TOPICS = {"targets": True, "pause": True, "revive": True, "flags": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _group(db, gid, *, cpl=20, name=None, user=USER):
    doc = {"id": gid, "user_id": user, "name": name or f"Client {gid}", "meta_ad_account_id": f"act_{gid}"}
    if cpl is not None:
        doc["targets"] = {"cpl": cpl}
    await db.client_groups.insert_one(doc)


async def _agent(db, gids, *, topics=None, user=USER):
    agent = await create_agent(db, user)
    return await update_agent(db, user, agent["_id"], {
        "scope": {"client_group_ids": list(gids), "schedule": {"time": "07:00", "tz": "Europe/London"}},
        "training": {"topics": {**{"targets": False, "pause": False, "revive": False, "flags": False},
                                **(topics or {})}},
    })


async def _practice(db, agent_id, agreed: int, answered: int):
    for i in range(answered):
        await db[PRACTICE].insert_one({
            "_id": f"{agent_id}:c{i}", "case_id": f"c{i}", "agent_id": agent_id,
            "case": {"rule_kind": "pause_over_target", "birdy_answer": "Pause", "options": ["Pause", "Leave it"]},
            "rule_id": None, "rule_version_at_answer": 1,
            "user_answer": "Pause" if i < agreed else "Leave it",
            "resolution": "agreed" if i < agreed else "pending",
            "served_at": datetime.utcnow(),
        })


async def _ready_agent(db):
    await _group(db, "g1")
    agent = await _agent(db, ["g1"], topics=FULL_TOPICS)
    await _practice(db, agent["_id"], 6, 6)
    return agent


@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    # Practice cases replay up to local yesterday: make that the fixture's END.
    monkeypatch.setattr(agents_router, "_local_today", lambda agent: END + timedelta(days=1))
    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Formula
# ---------------------------------------------------------------------------

def test_wilson_values():
    assert wilson_lower(6, 6) == pytest.approx(0.7855, abs=0.001)
    assert wilson_lower(4, 6) == pytest.approx(0.4097, abs=0.001)
    assert wilson_lower(9, 10) == pytest.approx(0.7178, abs=0.001)
    assert wilson_lower(0, 0) == 0


@pytest.mark.parametrize("agreed,answered,score", [(6, 6, 87), (9, 10, 83), (5, 6, 75), (4, 6, 65)])
async def test_scores_match_spec(mock_db, agreed, answered, score):
    await _group(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"], topics=FULL_TOPICS)
    await _practice(mock_db, agent["_id"], agreed, answered)

    c = await compute_confidence(mock_db, agent)

    assert c["coverage"] == 40
    assert c["score"] == score
    assert c["parts"] == {"targets": True, "pause": True, "revive": True, "flags": True,
                          "practice_agreed": agreed, "practice_answered": answered}
    assert c["ready"] is (score >= 80)
    assert c["missing"] == ([] if score >= 80 else ["Get the score to 80"])


async def test_practice_zero_below_6_answers(mock_db):
    await _group(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"], topics=FULL_TOPICS)
    await _practice(mock_db, agent["_id"], 5, 5)
    # Served but unanswered cases don't count as answered.
    await mock_db[PRACTICE].insert_one({"_id": "x", "case_id": "x", "agent_id": agent["_id"], "case": {},
                                        "user_answer": None, "resolution": "unanswered"})

    c = await compute_confidence(mock_db, agent)

    assert c["practice"] == 0
    assert c["score"] == 40
    assert c["parts"]["practice_agreed"] == 5 and c["parts"]["practice_answered"] == 5
    assert c["ready"] is False
    assert c["missing"] == ["Answer at least 6 practice cases", "Get the score to 80"]


async def test_targets_part_and_missing_order(mock_db):
    await _group(mock_db, "g1", name="Acme")
    await _group(mock_db, "g2", name="Bravo", cpl=None)
    agent = await _agent(mock_db, ["g1", "g2"], topics={"targets": True, "revive": True})

    c = await compute_confidence(mock_db, agent)

    assert c["parts"]["targets"] is False  # Bravo has no CPL target
    assert c["coverage"] == 10  # only revive is covered
    assert c["missing"] == [
        "Tell Birdy when to pause ads",
        "Tell Birdy what to flag",
        "Set a CPL target for Bravo",
        "Answer at least 6 practice cases",
        "Get the score to 80",
    ]

    empty = await _agent(mock_db, [], topics=FULL_TOPICS, user="someone-else")
    c2 = await compute_confidence(mock_db, empty)
    assert c2["parts"]["targets"] is False
    assert "Choose at least one client to watch" in c2["missing"]


# ---------------------------------------------------------------------------
# Practice routes and the user_right re-score
# ---------------------------------------------------------------------------

async def test_practice_routes_flow(api, mock_db):
    await _fixture_account(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"], topics=FULL_TOPICS)
    aid = agent["_id"]

    r = api.get(f"/api/agents/{aid}/practice?k=2")
    assert r.status_code == 200, r.text
    cases = r.json()
    assert len(cases) == 2
    assert all("snapshot" not in c for c in cases)
    assert set(cases[0]) == {"case_id", "rule_kind", "client_name", "title", "facts", "options",
                             "birdy_answer", "why"}
    assert await mock_db[PRACTICE].count_documents({"agent_id": aid}) == 2

    # Unanswered cases are served again first; nothing is duplicated.
    again = api.get(f"/api/agents/{aid}/practice?k=2").json()
    assert [c["case_id"] for c in again] == [c["case_id"] for c in cases]
    assert await mock_db[PRACTICE].count_documents({"agent_id": aid}) == 2

    c0 = cases[0]
    assert api.post(f"/api/agents/{aid}/practice/{c0['case_id']}", json={"answer": "Nope"}).status_code == 400
    assert api.post(f"/api/agents/{aid}/practice/unknown", json={"answer": "Flag"}).status_code == 404
    r = api.post(f"/api/agents/{aid}/practice/{c0['case_id']}", json={"answer": c0["birdy_answer"]})
    assert r.status_code == 200
    assert r.json() == {"agreed": True, "birdy_answer": c0["birdy_answer"], "why": c0["why"]}
    assert api.post(f"/api/agents/{aid}/practice/{c0['case_id']}",
                    json={"answer": c0["birdy_answer"]}).status_code == 409
    # Only a pending (disagreed) case can be resolved.
    assert api.post(f"/api/agents/{aid}/practice/{c0['case_id']}/resolve",
                    json={"resolution": "birdy_right"}).status_code == 409

    c1 = cases[1]
    other = next(o for o in c1["options"] if o != c1["birdy_answer"])
    r = api.post(f"/api/agents/{aid}/practice/{c1['case_id']}", json={"answer": other})
    assert r.json()["agreed"] is False
    assert api.post(f"/api/agents/{aid}/practice/{c1['case_id']}/resolve",
                    json={"resolution": "maybe"}).status_code == 422
    r = api.post(f"/api/agents/{aid}/practice/{c1['case_id']}/resolve", json={"resolution": "birdy_right"})
    assert r.status_code == 200 and r.json() == {"resolution": "birdy_right"}

    conf = api.get(f"/api/agents/{aid}/confidence").json()
    assert conf["parts"]["practice_agreed"] == 2 and conf["parts"]["practice_answered"] == 2

    # The next batch excludes ads already served and differs from the first.
    nxt = api.get(f"/api/agents/{aid}/practice?k=2").json()
    assert not {c["case_id"] for c in nxt} & {c0["case_id"], c1["case_id"]}

    # Another user's agent is a 404.
    app.dependency_overrides[get_current_user] = lambda: "intruder"
    assert api.get(f"/api/agents/{aid}/practice").status_code == 404
    assert api.get(f"/api/agents/{aid}/confidence").status_code == 404


async def test_user_right_rescored_after_rule_update(api, mock_db):
    await _fixture_account(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"], topics=FULL_TOPICS)
    aid = agent["_id"]

    cases = api.get(f"/api/agents/{aid}/practice?k=20").json()
    a2 = next(c for c in cases if c["title"] == "Pause “Ad A2”")  # £70 in 7 days, 0 leads: near miss
    assert a2["birdy_answer"] == "Leave it"

    assert api.post(f"/api/agents/{aid}/practice/{a2['case_id']}", json={"answer": "Pause"}).json()["agreed"] is False
    assert api.post(f"/api/agents/{aid}/practice/{a2['case_id']}/resolve",
                    json={"resolution": "user_right"}).status_code == 200

    conf = api.get(f"/api/agents/{aid}/confidence").json()
    assert conf["parts"] == {**FULL_TOPICS, "practice_agreed": 0, "practice_answered": 1}

    # The user teaches Birdy: pause after £60 with no leads. A2 now fires.
    rule = next(r for r in await list_rules(mock_db, aid) if r["kind"] == "pause_over_target")
    r = api.patch(f"/api/agents/{aid}/rules/{rule['_id']}", json={"params": {"zero_lead_spend": 60}})
    assert r.status_code == 200

    conf = api.get(f"/api/agents/{aid}/confidence").json()
    assert conf["parts"]["practice_agreed"] == 1 and conf["parts"]["practice_answered"] == 1
    doc = await mock_db[PRACTICE].find_one({"agent_id": aid, "case_id": a2["case_id"]})
    assert doc["resolution"] == "agreed"
    # The score is persisted on the agent every time it is read.
    stored = await mock_db.agents.find_one({"_id": aid})
    assert stored["confidence"]["score"] == conf["score"]
    assert stored["confidence"]["parts"] == conf["parts"]


async def test_user_right_not_rescored_when_rule_unchanged_or_still_differs(mock_db, mock_mongo_client):
    await _fixture_account(mock_db, "g1")
    agent = await _agent(mock_db, ["g1"], topics=FULL_TOPICS)
    aid = agent["_id"]
    from ai.agents import replay
    cases = await replay.sample_cases(mock_db, mock_mongo_client, agent, end=END, k=50, seed=1,
                                      exclude_ad_ids=set())
    a3 = next(c for c in cases if c["title"] == "Pause “Ad A3”")  # fires: Birdy says Pause
    rule = next(r for r in await list_rules(mock_db, aid) if r["kind"] == "pause_over_target")
    await mock_db[PRACTICE].insert_one({
        "_id": f"{aid}:{a3['case_id']}", "case_id": a3["case_id"], "agent_id": aid, "case": a3,
        "rule_id": rule["_id"], "rule_version_at_answer": rule["version"], "user_answer": "Leave it",
        "resolution": "user_right"})

    assert (await compute_confidence(mock_db, agent))["parts"]["practice_agreed"] == 0

    # A rule edit that still pauses A3 (£50 CPL vs £20 × 2 = £40) doesn't flip it.
    from ai.agents.store import update_rule
    await update_rule(mock_db, aid, rule["_id"], params={**rule["params"], "cpl_multiple": 2})
    assert (await compute_confidence(mock_db, agent))["parts"]["practice_agreed"] == 0
    # Raising the bar to 3× target (£60) leaves A3 alone: now it agrees.
    await update_rule(mock_db, aid, rule["_id"], params={**rule["params"], "cpl_multiple": 3})
    assert (await compute_confidence(mock_db, agent))["parts"]["practice_agreed"] == 1


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------

async def test_activate_409_lists_missing(api, mock_db):
    await _group(mock_db, "g1", name="Acme", cpl=None)
    agent = await _agent(mock_db, ["g1"], topics={"pause": True})

    r = api.post(f"/api/agents/{agent['_id']}/activate")

    assert r.status_code == 409
    assert r.json()["missing"] == [
        "Tell Birdy when to revive old winners",
        "Tell Birdy what to flag",
        "Set a CPL target for Acme",
        "Answer at least 6 practice cases",
        "Get the score to 80",
    ]
    assert (await mock_db.agents.find_one({"_id": agent["_id"]}))["status"] == "draft"


async def test_activate_sets_modes_and_snapshot_score(api, mock_db):
    agent = await _ready_agent(mock_db)
    aid = agent["_id"]
    rules = {r["kind"]: r for r in await list_rules(mock_db, aid)}
    # Leave the rules in the wrong modes, with an open item carrying the old mode.
    await mock_db.agent_rules.update_one({"_id": rules["pause_over_target"]["_id"]}, {"$set": {"mode": "flag"}})
    await mock_db.agent_rules.update_one({"_id": rules["flag_no_spend"]["_id"]}, {"$set": {"mode": "ask"}})
    await mock_db[sstore.SUGGESTIONS].insert_one({"_id": "s1", "user_id": USER, "status": "open",
                                                  "rule_id": rules["pause_over_target"]["_id"],
                                                  "rule_mode": "flag"})

    r = api.post(f"/api/agents/{aid}/activate")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "active"
    assert body["confidence"]["score"] == 87
    assert body["confidence"]["at_activation"] == 87
    modes = {r["kind"]: r["mode"] for r in await list_rules(mock_db, aid)}
    assert modes == {"pause_over_target": "ask", "revive_winner": "ask",
                     "flag_no_spend": "flag", "flag_declining": "flag"}
    assert (await mock_db[sstore.SUGGESTIONS].find_one({"_id": "s1"}))["rule_mode"] == "ask"

    # Idempotent: a second activate changes nothing.
    pause_id = rules["pause_over_target"]["_id"]
    await mock_db.agent_rules.update_one({"_id": pause_id}, {"$set": {"mode": "auto"}})
    r2 = api.post(f"/api/agents/{aid}/activate")
    assert r2.status_code == 200
    assert r2.json()["confidence"]["at_activation"] == 87
    assert (await mock_db.agent_rules.find_one({"_id": pause_id}))["mode"] == "auto"


async def test_activation_keeps_practice_score_until_6_real_decisions(mock_db):
    agent = await _ready_agent(mock_db)
    aid = agent["_id"]
    before = await compute_confidence(mock_db, agent)
    assert before["practice_source"] == "practice"
    await mock_db.agents.update_one({"_id": aid}, {"$set": {"status": "active"}})
    agent = await mock_db.agents.find_one({"_id": aid})

    after = await compute_confidence(mock_db, agent)  # 0 decisions
    assert after["score"] == before["score"] and after["practice"] == before["practice"]
    assert after["practice_source"] == "practice"
    assert after["parts"]["practice_answered"] == 6 and after["parts"]["practice_agreed"] == 6

    ask_rule = next(r for r in await list_rules(mock_db, aid) if r["kind"] == "pause_over_target")["_id"]
    rows = [{"_id": f"d{i}", "user_id": USER, "agent_id": aid, "rule_id": ask_rule,
             "kind": sstore.KIND_SUGGESTION_DISMISSED if i < 3 else sstore.KIND_ACTION_APPLIED,
             "actor": "user", "ref_id": f"s{i}", "created_at": datetime(2026, 10, 1) + timedelta(hours=i)}
            for i in range(6)]
    await mock_db[sstore.ACTIVITY].insert_many(rows)
    switched = await compute_confidence(mock_db, agent)
    assert switched["practice_source"] == "decisions"
    assert switched["parts"]["practice_answered"] == 6 and switched["parts"]["practice_agreed"] == 3
    assert switched["practice"] == round(60 * wilson_lower(3, 6))


async def test_post_launch_uses_rolling_decisions(mock_db):
    agent = await _ready_agent(mock_db)  # practice cases say 6/6; they only count until 6 decisions
    aid = agent["_id"]
    rules = {r["kind"]: r for r in await list_rules(mock_db, aid)}
    ask_rule, flag_rule = rules["pause_over_target"]["_id"], rules["flag_no_spend"]["_id"]
    await mock_db.agents.update_one({"_id": aid}, {"$set": {"status": "active"}})
    agent = await mock_db.agents.find_one({"_id": aid})

    t0 = datetime(2026, 10, 1)
    rows = []

    def row(i, kind, *, rule_id=ask_rule, actor="user", ref=None):
        rows.append({"_id": f"act{i}", "user_id": USER, "agent_id": aid, "rule_id": rule_id, "kind": kind,
                     "actor": actor, "ref_id": ref or f"s{i}", "created_at": t0 + timedelta(hours=i)})

    for i in range(5):  # oldest: outside the last 30 decisions
        row(i, sstore.KIND_SUGGESTION_DISMISSED)
    for i in range(5, 32):  # 27 approvals
        row(i, sstore.KIND_ACTION_APPLIED)
    for i in range(32, 35):  # 3 denials
        row(i, sstore.KIND_SUGGESTION_DISMISSED)
    # One approval is later undone (the undo row carries no rule_id): a failure.
    rows.append({"_id": "undo", "user_id": USER, "kind": sstore.KIND_ACTION_UNDONE, "actor": "user",
                 "ref_id": "s10", "created_at": t0 + timedelta(hours=40)})
    # Not decisions on ask rules: a flag-rule dismissal, an auto-run apply, another agent.
    row(50, sstore.KIND_SUGGESTION_DISMISSED, rule_id=flag_rule)
    row(51, sstore.KIND_ACTION_APPLIED, actor="birdy")
    row(52, sstore.KIND_SUGGESTION_CREATED)
    rows.append({**rows[-1], "_id": "foreign", "agent_id": "other-agent", "kind": sstore.KIND_SUGGESTION_DISMISSED})
    await mock_db[sstore.ACTIVITY].insert_many(rows)

    c = await compute_confidence(mock_db, agent)

    assert c["parts"]["practice_answered"] == 30
    assert c["parts"]["practice_agreed"] == 26
    assert c["practice"] == round(60 * wilson_lower(26, 30))
    assert c["score"] == round(40 + 60 * wilson_lower(26, 30))

    assert c["practice_source"] == "decisions"

    # Fewer than 6 decisions: the practice cases (6/6) keep counting.
    await mock_db[sstore.ACTIVITY].delete_many({"_id": {"$nin": ["act30", "act31"]}})
    c = await compute_confidence(mock_db, agent)
    assert c["practice_source"] == "practice"
    assert c["parts"]["practice_answered"] == 6 and c["parts"]["practice_agreed"] == 6
    assert c["practice"] == round(60 * wilson_lower(6, 6))


def test_answer_label_shared_with_sampler():
    from ai.agents import replay
    assert confidence.answer_label is replay.answer_label
