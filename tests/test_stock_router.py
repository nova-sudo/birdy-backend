"""The stock agent's API (spec section 12): preview, runs, cards, outcomes, lessons, activate."""

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import routers.agent_stock as stock_router
import routers.agents as agents_router
from ai.agents import claims
from ai.agents.stock import runner, templates
from ai.agents.stock.runner import RUNS
from ai.agents.stock.scoreboard import OUTCOMES
from ai.agents.store import (AGENTS, CARDS, MEMORY, TYPE_STOCK, create_agent, create_agent_indexes,
                             delete_draft_agent)
from dependencies import get_current_user
from jobs import agent_jobs
from main import app
from test_stock_runner import NOW, USER, ZONES, _client, _setup, fakes  # noqa: F401 - fakes is a fixture

OTHER = "u2"


@pytest.fixture
def api(mock_mongo_client, monkeypatch, fakes):  # noqa: F811
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(stock_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(stock_router, "_utcnow", lambda: NOW)
    state = {"user": USER}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    client.fakes = fakes
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def _routes(aid):
    base = f"/api/agents/{aid}"
    return [("post", f"{base}/preview"), ("get", f"{base}/runs"), ("get", f"{base}/runs/latest"),
            ("get", f"{base}/runs/r1"), ("patch", f"{base}/cards/c1"), ("get", f"{base}/outcomes"),
            ("get", f"{base}/lessons")]


def _call(api, method, path):
    return getattr(api, method)(path, **({"json": {}} if method == "patch" else {}))


async def test_routes_404_for_another_users_agent_and_400_for_custom(api, mock_db):
    agent = await _setup(mock_db, "g1")
    api.state["user"] = OTHER
    for method, path in _routes(agent["_id"]):
        assert _call(api, method, path).status_code == 404, path
    api.state["user"] = USER
    custom = await create_agent(mock_db, USER, type="custom", name="Rules")
    for method, path in _routes(custom["_id"]):
        assert _call(api, method, path).status_code == 400, path


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

async def test_preview_409_while_history_is_not_ready(api, mock_db):
    agent = await _setup(mock_db, "g1", history_status="pending")
    r = api.post(f"/api/agents/{agent['_id']}/preview")
    assert r.status_code == 409 and r.json()["not_ready"] == ["g1"]
    assert await mock_db[RUNS].count_documents({}) == 0


async def test_preview_needs_a_client(api, mock_db):
    agent = await create_agent(mock_db, USER, type=TYPE_STOCK)
    assert api.post(f"/api/agents/{agent['_id']}/preview").status_code == 409


async def test_preview_runs_and_writes_nothing_live(api, mock_db, monkeypatch):
    agent = await _setup(mock_db, "g1", claim=False)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "draft"}})
    r = api.post(f"/api/agents/{agent['_id']}/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "preview" and body["phase"] == "done"
    f = api.fakes
    assert f.published == [] and f.autos == [] and f.urgent == []
    [card] = await mock_db[CARDS].find({"run_id": body["run_id"]}).to_list(None)
    assert card["preview"] is True
    shown = api.get(f"/api/agents/{agent['_id']}/runs/{body['run_id']}").json()
    assert [c["can_apply"] for c in shown["cards"]] == [False]


async def test_a_stale_open_preview_is_ended_and_a_fresh_one_starts(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    old = await runner.get_or_create_run(mock_db, agent, NOW - timedelta(days=2), kind="preview")
    r = api.post(f"/api/agents/{agent['_id']}/preview")
    assert r.status_code == 200 and r.json()["run_id"] != old["_id"]
    ended = await mock_db[RUNS].find_one({"_id": old["_id"]})
    assert ended["phase"] == "done" and ended["ended_early"] is True


async def test_the_tick_finishes_an_unfinished_preview(api, mock_db, mock_mongo_client):
    agent = await _setup(mock_db, "g1", claim=False)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "draft"}})  # not ticked as a day's run
    run = await runner.get_or_create_run(mock_db, agent, NOW, kind="preview")
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    for n in range(1, 8):
        if (await mock_db[RUNS].find_one({"_id": run["_id"]}))["phase"] == "done":
            break
        await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW + timedelta(minutes=15 * n))
    assert (await mock_db[RUNS].find_one({"_id": run["_id"]}))["phase"] == "done"
    assert api.fakes.urgent == [] and api.fakes.published == []


# ---------------------------------------------------------------------------
# Activate
# ---------------------------------------------------------------------------

async def test_activate_lists_what_is_missing_until_ready(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False, history_status="ready")
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft", "scope.client_group_ids": []}})
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and len(r.json()["missing"]) >= 2
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"scope.client_group_ids": ["g1"]}})
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and any("preview" in m.lower() for m in r.json()["missing"])

    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"settings.zones": {
        "currency": None, "green_under": None, "red_over": None}, "settings.clients.g1.zones": None}})
    r = api.post(f"/api/agents/{aid}/activate")
    assert any("zone" in m.lower() for m in r.json()["missing"])

    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"settings.zones": ZONES}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 200 and r.json()["status"] == "active"
    assert await claims.holder(mock_db, USER, "g1") == aid


async def test_activate_claim_conflict_is_409_with_conflicts(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and r.json()["conflicts"][0]["client_group_id"] == "g1"


# ---------------------------------------------------------------------------
# Deactivate (turn off)
# ---------------------------------------------------------------------------

async def test_deactivate_turns_a_live_agent_off_and_keeps_what_setup_chose(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"scope.flag_only_client_group_ids": ["g1"]}})
    before = await mock_db[AGENTS].find_one({"_id": aid})
    assert await claims.holder(mock_db, USER, "g1") == aid

    r = api.post(f"/api/agents/{aid}/deactivate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft" and r.json()["turned_off_at"]
    assert await claims.holder(mock_db, USER, "g1") is None
    after = await mock_db[AGENTS].find_one({"_id": aid})
    assert after["scope"] == before["scope"] and after["settings"] == before["settings"]

    again = api.post(f"/api/agents/{aid}/deactivate")  # nothing to do the second time
    assert again.status_code == 200 and again.json()["turned_off_at"] == r.json()["turned_off_at"]
    assert api.get(f"/api/agents/{aid}").json()["turned_off_at"] == r.json()["turned_off_at"]


async def test_deactivate_a_draft_agent_changes_nothing(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"status": "draft"}})
    r = api.post(f"/api/agents/{agent['_id']}/deactivate")
    assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["turned_off_at"] is None


async def test_deactivate_is_for_the_stock_agent_and_its_owner(api, mock_db):
    custom = await create_agent(mock_db, USER, type="custom", name="Rules")
    await mock_db[AGENTS].update_one({"_id": custom["_id"]}, {"$set": {"status": "active"}})
    r = api.post(f"/api/agents/{custom['_id']}/deactivate")
    assert r.status_code == 409 and "Birdy Media Buying Agent" in r.json()["detail"]
    assert (await mock_db[AGENTS].find_one({"_id": custom["_id"]}))["status"] == "active"
    stock = await _setup(mock_db, "g1")
    api.state["user"] = OTHER
    assert api.post(f"/api/agents/{stock['_id']}/deactivate").status_code == 404


async def test_a_turned_off_agent_is_not_ticked_and_turning_it_on_needs_no_new_preview(api, mock_db, mock_mongo_client):
    agent = await _setup(mock_db, "g1", claim=False)
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    assert api.post(f"/api/agents/{aid}/activate").status_code == 200
    previews = await mock_db[RUNS].count_documents({"agent_id": aid, "kind": "preview"})

    assert api.post(f"/api/agents/{aid}/deactivate").status_code == 200
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert await mock_db[RUNS].count_documents({"agent_id": aid, "kind": {"$ne": "preview"}}) == 0

    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["turned_off_at"] is None
    assert "turned_off_at" not in await mock_db[AGENTS].find_one({"_id": aid})
    assert await claims.holder(mock_db, USER, "g1") == aid
    assert await mock_db[RUNS].count_documents({"agent_id": aid, "kind": "preview"}) == previews
    await agent_jobs.run_due_stock_agents(mock_mongo_client, now_utc=NOW)
    assert await mock_db[RUNS].count_documents({"agent_id": aid, "kind": {"$ne": "preview"}}) == 1


async def test_a_turned_off_agent_cannot_be_started_over(api, mock_db):
    agent = await _setup(mock_db, "g1")
    api.post(f"/api/agents/{agent['_id']}/deactivate")
    r = api.delete(f"/api/agents/{agent['_id']}")
    assert r.status_code == 409 and "turned off" in r.json()["detail"].lower()
    assert await mock_db[AGENTS].count_documents({"_id": agent["_id"]}) == 1


# ---------------------------------------------------------------------------
# Runs, cards, outcomes, lessons
# ---------------------------------------------------------------------------

async def _seed_run(db, agent, **kw):
    run = {"_id": kw.pop("rid"), "agent_id": agent["_id"], "user_id": USER, "kind": "batch", "phase": "done",
           "local_date": "2026-11-03", "as_of": datetime(2026, 11, 3, 7), "summary": {"cards": 1},
           "clients": {"g1": {"state": "done", "facts": {"big": 1}, "draft": {"x": 1}}}, "alerts": [],
           "scheduled": True, **kw}
    await db[RUNS].insert_one(run)
    return run


async def _seed_card(db, agent, cid, rid, **kw):
    card = {"_id": cid, "agent_id": agent["_id"], "user_id": USER, "run_id": rid, "client_group_id": "g1",
            "tag": "changes", "status": "open", "lines": [{"id": "l1", "kind": "remove", "ad_name": "A", "meta_ids": ["a1"],
                                         "mode": "ask", "state": "proposed"},
                                        {"id": "l2", "kind": "remove", "ad_name": "B", "meta_ids": ["a2"],
                                         "mode": "ask", "state": "proposed"}],
            "additional_changes": "", "thought_process": "t", "next_steps": "n",
            "original": {"lines": [], "additional_changes": "", "thought_process": "t", "next_steps": "n"},
            "follow_ups": [{"line_id": "l1", "text": "A: off", "ok": True}],
            "created_at": datetime(2026, 11, 3, 8), **kw}
    await db[CARDS].insert_one(card)


async def test_runs_list_latest_and_detail(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r1", as_of=datetime(2026, 11, 1, 7))
    await _seed_run(mock_db, agent, rid="r2", as_of=datetime(2026, 11, 3, 7))
    await _seed_run(mock_db, agent, rid="rp", kind="preview", as_of=datetime(2026, 11, 4, 7))
    await _seed_card(mock_db, agent, "c1", "r2")
    await _seed_card(mock_db, agent, "c2", "r2", client_group_id="g2", status="closing")
    listed = api.get(f"/api/agents/{aid}/runs?limit=1").json()
    assert [r["id"] for r in listed["runs"]] == ["r2"] and listed["next_before"]
    older = api.get(f"/api/agents/{aid}/runs", params={"before": listed["next_before"]}).json()
    assert [r["id"] for r in older["runs"]] == ["r1"] and older["next_before"] is None
    assert api.get(f"/api/agents/{aid}/runs/latest").json()["run"]["id"] == "r2"
    assert api.get(f"/api/agents/{aid}/runs/latest?kind=preview").json()["run"]["id"] == "rp"
    d = api.get(f"/api/agents/{aid}/runs/r2").json()
    assert set(d) == {"run", "cards", "follow_ups", "scoreboard", "learned", "proposals", "weekly"}
    assert [c["id"] for c in d["cards"]] == ["c1"] and d["cards"][0]["can_apply"] is True
    assert d["follow_ups"] == [{"client_group_id": "g1", "line_id": "l1", "text": "A: off", "ok": True}]
    assert "facts" not in d["run"]["clients"]["g1"] and "_id" not in d["run"]
    assert (d["learned"], d["proposals"], d["weekly"]) == ([], [], None)
    assert d["scoreboard"] == {"won": 0, "lost": 0, "moves": 0, "rows": []}
    assert api.get(f"/api/agents/{aid}/runs/nope").status_code == 404


async def test_latest_with_no_runs_is_null(api, mock_db):
    agent = await _setup(mock_db, "g1")
    assert api.get(f"/api/agents/{agent['_id']}/runs/latest").json() == {"run": None, "open_cards": 0}


async def test_patch_card_keeps_original_and_refuses_a_settled_card(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    r = api.patch(f"/api/agents/{aid}/cards/c1", json={
        "thought_process": "mine", "lines": [{"id": "l2", "kind": "add", "meta_ids": ["evil"]}]})
    assert r.status_code == 200, r.text
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert card["thought_process"] == "mine" and card["original"]["thought_process"] == "t"
    assert [ln["id"] for ln in card["lines"]] == ["l2"]
    assert card["lines"][0]["kind"] == "remove" and card["lines"][0]["meta_ids"] == ["a2"]
    assert api.patch(f"/api/agents/{aid}/cards/c1", json={"lines": [{"id": "zz"}]}).status_code == 400
    assert api.patch(f"/api/agents/{aid}/cards/c1", json={"status": "applied"}).status_code == 422
    for status in ("applied", "partly_applied", "dismissed", "superseded"):
        await _seed_card(mock_db, agent, f"s-{status}", "r2", client_group_id=status, status=status)
        assert api.patch(f"/api/agents/{aid}/cards/s-{status}", json={"next_steps": "x"}).status_code == 409
    await _seed_card(mock_db, agent, "cl", "r2", client_group_id="x", status="closing")
    assert api.patch(f"/api/agents/{aid}/cards/cl", json={"next_steps": "x"}).status_code == 404


async def test_outcomes_and_lessons(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    assert api.get(f"/api/agents/{aid}/outcomes").json() == {"won": 0, "lost": 0, "moves": 0, "rows": []}
    await mock_db[OUTCOMES].insert_many([
        {"_id": "o1", "agent_id": aid, "creative_key": "k", "result": "won", "created_at": datetime(2026, 11, 1)},
        {"_id": "o2", "agent_id": aid, "creative_key": "k", "result": None, "created_at": datetime(2026, 11, 2)},
        {"_id": "o3", "agent_id": "other", "creative_key": "k", "result": "lost"}])
    out = api.get(f"/api/agents/{aid}/outcomes").json()
    assert (out["won"], out["lost"], out["moves"]) == (1, 0, 2)
    assert [r["id"] for r in out["rows"]] == ["o2", "o1"]
    assert api.get(f"/api/agents/{aid}/lessons").json() == []
    await mock_db[MEMORY].insert_one({"_id": "m1", "agent_id": aid, "text": "no blue", "active": True,
                                      "created_at": datetime(2026, 11, 1)})
    assert [m["id"] for m in api.get(f"/api/agents/{aid}/lessons").json()] == ["m1"]


# ---------------------------------------------------------------------------
# Fix round 1
# ---------------------------------------------------------------------------

def test_preview_budget_leaves_room_for_the_last_read():
    assert stock_router.PREVIEW_BUDGET_SECONDS <= 170


async def test_read_timeout_shrinks_near_the_deadline():
    assert runner._read_timeout(None) == runner.CLIENT_READ_TIMEOUT_S
    assert runner._read_timeout(time.monotonic() + 170) == runner.CLIENT_READ_TIMEOUT_S
    near = runner._read_timeout(time.monotonic() + 5)
    assert near <= 5 + runner.DEADLINE_MARGIN_S < runner.CLIENT_READ_TIMEOUT_S
    assert runner._read_timeout(time.monotonic() - 50) >= 1  # never zero or negative


async def test_a_racing_preview_insert_returns_the_existing_run(mock_db, mock_mongo_client):
    """Mongomock runs requests one after another, so the race is forced: the second caller's
    open-preview check misses (as it would between the first caller's check and insert), and the
    unique open_preview index sends it to the run the first caller made. Real concurrent
    behaviour is a Task 21 real-Mongo check."""
    await create_agent_indexes(mock_mongo_client)
    agent = await _setup(mock_db, "g1", claim=False)
    first = await runner.get_or_create_run(mock_db, agent, NOW, kind="preview")

    class Miss:
        def __init__(self, coll):
            self.coll, self.missed = coll, False

        async def find_one(self, *a, **kw):
            if not self.missed:
                self.missed = True
                return None
            return await self.coll.find_one(*a, **kw)

        def __getattr__(self, name):
            return getattr(self.coll, name)

    miss = Miss(mock_db[RUNS])

    class Db:
        def __getitem__(self, name):
            return miss if name == RUNS else mock_db[name]

    second = await runner.get_or_create_run(Db(), agent, NOW, kind="preview")
    assert second["_id"] == first["_id"] and await mock_db[RUNS].count_documents({"kind": "preview"}) == 1


async def test_open_preview_flag_clears_when_done(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    r = api.post(f"/api/agents/{agent['_id']}/preview").json()
    assert (await mock_db[RUNS].find_one({"_id": r["run_id"]}))["open_preview"] is False


async def test_card_edit_is_versioned_and_refuses_a_stale_version(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    api.patch(f"/api/agents/{aid}/cards/c1", json={"next_steps": "x"})
    assert (await mock_db[CARDS].find_one({"_id": "c1"}))["version"] == 1
    from ai.agents.stock.cards import guarded_update
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert await guarded_update(mock_db, card, {"next_steps": "a"}) is True
    assert await guarded_update(mock_db, card, {"next_steps": "b"}) is False  # card was read before that write
    assert (await mock_db[CARDS].find_one({"_id": "c1"}))["next_steps"] == "a"


async def test_dropping_a_line_that_was_applied_is_refused(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    await mock_db[CARDS].update_one({"_id": "c1", "lines.id": "l1"}, {"$set": {"lines.$.state": "applied"}})
    r = api.patch(f"/api/agents/{aid}/cards/c1", json={"lines": [{"id": "l2"}]})
    assert r.status_code == 409
    assert len((await mock_db[CARDS].find_one({"_id": "c1"}))["lines"]) == 2
    assert api.patch(f"/api/agents/{aid}/cards/c1", json={"lines": [{"id": "l1"}, {"id": "l2"}]}).status_code == 200


async def test_preview_cards_cannot_be_patched(api, mock_db):
    agent = await _setup(mock_db, "g1")
    await _seed_run(mock_db, agent, rid="rp", kind="preview")
    await _seed_card(mock_db, agent, "cp", "rp", preview=True)
    assert api.patch(f"/api/agents/{agent['_id']}/cards/cp", json={"next_steps": "x"}).status_code == 409


async def test_deleting_a_draft_agent_removes_its_outcomes(mock_db):
    agent = await create_agent(mock_db, USER, type=TYPE_STOCK)
    await mock_db[OUTCOMES].insert_many([{"_id": "o1", "agent_id": agent["_id"]}, {"_id": "o2", "agent_id": "x"}])
    assert await delete_draft_agent(mock_db, USER, agent["_id"])
    assert [o["_id"] async for o in mock_db[OUTCOMES].find()] == ["o2"]


async def test_activate_needs_a_preview_covering_the_current_clients(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    await _client(mock_db, "g2")
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {
        "scope.client_group_ids": ["g1", "g2"], "settings.clients.g2": {"batch_id": "a", "treatment_ids": []}}})
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and any("preview" in m.lower() for m in r.json()["missing"])


async def test_run_view_trims_internals_and_outcome_rows_name_their_fields(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2", slack={"channel_id": "C1", "ts": "1.2"})
    run = api.get(f"/api/agents/{aid}/runs/r2").json()["run"]
    assert "user_id" not in run and "channel_id" not in run["slack"]
    await mock_db[OUTCOMES].insert_one({"_id": "o1", "agent_id": aid, "creative_key": "k", "result": "won",
                                        "secret": "x", "created_at": datetime(2026, 11, 1)})
    [row] = api.get(f"/api/agents/{aid}/outcomes").json()["rows"]
    assert "secret" not in row and row["id"] == "o1" and row["result"] == "won"


async def test_other_agents_and_users_runs_and_cards_are_not_reachable(api, mock_db):
    mine = await _setup(mock_db, "g1")
    await _seed_run(mock_db, {"_id": "foreign-agent"}, rid="rf")
    await _seed_card(mock_db, {"_id": "foreign-agent"}, "cf", "rf")
    await _seed_run(mock_db, mine, rid="rm")
    await _seed_card(mock_db, mine, "cm", "rm")
    aid = mine["_id"]
    assert api.get(f"/api/agents/{aid}/runs/rf").status_code == 404
    assert api.patch(f"/api/agents/{aid}/cards/cf", json={"next_steps": "x"}).status_code == 404
    assert (await mock_db[CARDS].find_one({"_id": "cf"}))["thought_process"] == "t"
    # a card of my own agent, asked for through another run id, is not listed there
    await mock_db[CARDS].insert_one({"_id": "cx", "agent_id": "foreign-agent", "run_id": "rm", "client_group_id": "z",
                                     "status": "open", "lines": [], "created_at": datetime(2026, 11, 3)})
    assert [c["id"] for c in api.get(f"/api/agents/{aid}/runs/rm").json()["cards"]] == ["cm"]


async def test_bad_before_and_limit_bounds(api, mock_db):
    agent = await _setup(mock_db, "g1")
    base = f"/api/agents/{agent['_id']}/runs"
    assert api.get(base, params={"before": "yesterday"}).status_code == 422
    assert api.get(base, params={"limit": 0}).status_code == 422
    assert api.get(base, params={"limit": 101}).status_code == 422
    assert api.get(base, params={"limit": 100}).status_code == 200


async def test_flagged_cards_are_returned(api, mock_db):
    agent = await _setup(mock_db, "g1")
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "cf", "r2", status="flagged")
    cards = api.get(f"/api/agents/{agent['_id']}/runs/r2").json()["cards"]
    assert [(c["id"], c["can_apply"]) for c in cards] == [("cf", False)]


async def test_a_claim_conflict_on_activate_changes_nothing(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    await claims.claim_clients(mock_db, USER, "other-agent", ["g1"])
    assert api.post(f"/api/agents/{aid}/activate").status_code == 409
    assert await claims.holder(mock_db, USER, "g1") == "other-agent"
    doc = await mock_db[AGENTS].find_one({"_id": aid})
    assert doc["status"] == "draft"


async def test_latest_doesnt_count_green_or_no_changes_cards(api, mock_db):
    """They are `open` and `can_apply` but have no lines, so nothing on them waits for a person."""
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    await _seed_card(mock_db, agent, "c2", "r2", client_group_id="g2", tag="green", lines=[])
    await _seed_card(mock_db, agent, "c3", "r2", client_group_id="g3", tag="no_changes", lines=[])
    await _seed_card(mock_db, agent, "c4", "r2", client_group_id="g4", lines=[], additional_changes="Raise the budget")
    assert api.get(f"/api/agents/{aid}/runs/latest").json()["open_cards"] == 2
    shown = {c["id"]: c for c in api.get(f"/api/agents/{aid}/runs/r2").json()["cards"]}
    assert shown["c2"]["can_apply"] is True  # the route's own rule for the card itself doesn't change

    # Slack counts proposed lines, so it agrees: the additional-changes card has none to apply.
    cards = await mock_db[CARDS].find({"run_id": "r2"}).to_list(length=None)
    run = await mock_db[RUNS].find_one({"_id": "r2"})
    waiting = templates.summary_values(run, cards)["cards_waiting"]
    assert waiting == "1"
    assert "Apply all" in str(templates.summary_buttons(run, "https://x", waiting=int(waiting)))
    assert "Apply all" not in str(templates.summary_buttons(run, "https://x", waiting=0))


async def test_a_preview_card_is_never_replaced_by_a_later_live_card(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="rp", kind="preview")
    await _seed_card(mock_db, agent, "cp", "rp", preview=True)
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2", created_at=datetime(2026, 11, 4, 8))
    [card] = api.get(f"/api/agents/{aid}/runs/rp").json()["cards"]
    assert card["has_newer"] is False and card["can_apply"] is False


async def test_latest_counts_the_cards_a_person_can_still_apply(api, mock_db):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    await _seed_card(mock_db, agent, "c2", "r2", client_group_id="g2", status="applied")
    await _seed_card(mock_db, agent, "c3", "r2", client_group_id="g3", status="closing")
    await _seed_run(mock_db, agent, rid="rp", kind="preview", as_of=datetime(2026, 11, 4, 7))
    await _seed_card(mock_db, agent, "c4", "rp", client_group_id="g4")
    latest = api.get(f"/api/agents/{aid}/runs/latest").json()
    assert latest["open_cards"] == 1
    shown = api.get(f"/api/agents/{aid}/runs/r2").json()["cards"]
    assert latest["open_cards"] == sum(1 for c in shown if c["can_apply"])
    assert api.get(f"/api/agents/{aid}/runs/latest?kind=preview").json()["open_cards"] == 0


# ---------------------------------------------------------------------------
# Setup support: the add-writes switch in the agent view, and a preview that read nothing
# ---------------------------------------------------------------------------

async def test_agent_view_tells_the_stock_agent_whether_add_writes_are_on(api, mock_db, monkeypatch):
    agent = await _setup(mock_db, "g1")
    aid = agent["_id"]
    monkeypatch.delenv("AGENT_ADD_WRITES_ENABLED", raising=False)
    assert api.get(f"/api/agents/{aid}").json()["add_writes_enabled"] is False
    monkeypatch.setenv("AGENT_ADD_WRITES_ENABLED", "true")
    assert api.get(f"/api/agents/{aid}").json()["add_writes_enabled"] is True
    assert api.get("/api/agents").json()[0]["add_writes_enabled"] is True


async def test_custom_agent_view_has_no_add_writes_field(api, mock_db):
    custom = await create_agent(mock_db, USER, type="custom", name="Rules")
    assert "add_writes_enabled" not in api.get(f"/api/agents/{custom['_id']}").json()


async def test_a_preview_where_every_client_failed_does_not_count(api, mock_db):
    agent = await _setup(mock_db, "g1", claim=False)
    aid = agent["_id"]
    await mock_db[AGENTS].update_one({"_id": aid}, {"$set": {"status": "draft"}})
    assert api.post(f"/api/agents/{aid}/preview").status_code == 200
    await mock_db[RUNS].update_many({"agent_id": aid, "kind": "preview"}, {"$set": {
        "clients.g1": {"state": "failed", "error": "Meta needs reconnecting. Reconnect to check this client."}}})
    r = api.post(f"/api/agents/{aid}/activate")
    assert r.status_code == 409 and any("preview" in m.lower() for m in r.json()["missing"])
