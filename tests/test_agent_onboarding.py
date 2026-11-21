"""
tests/test_agent_onboarding.py
------------------------------
Agent onboarding (ai/team/setup.py, routers/team_setup.py, ai/team/setup_chat.py, ai/team/sales_coach.py):
the order and the lock, each step's answer landing where that setting lives, the lock at every entry point,
the setup chat's block guarantee, Sales Coach's numbers with and without cached data, and the credit gate.
Everything runs on mongomock with scripted providers: no model calls, no live DB, no external calls.
"""

import contextlib
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError

import credits
from ai.agents.store import AGENTS, RUNS, TYPE_STOCK
from ai.orchestrator import run_chat
from ai.team import definitions as defs
from ai.team import delegation
from ai.team import route as rt
from ai.team import sales_coach as sc
from ai.team import setup as s
from ai.team import setup_chat
from ai.team.memory import TEAM_AGENTS
from ai.tools.registry import ToolRegistry
from core.models import ChatRequest
from tests.test_agent_team import TeamProvider, call, registry, say  # noqa: F401 (fixture)

pytestmark = pytest.mark.agent_onboarding

U = "owner@example.com"
G1, G2, G3 = "grp-aura", "grp-beaulea", "grp-calm"


@pytest.fixture(autouse=True)
def isolate(monkeypatch, mock_db):
    monkeypatch.setattr("ai.orchestrator.mcp_client.needs_mcp", lambda allowed: False)
    credits._settings_cache["data"] = None
    yield
    credits._settings_cache["data"] = None


async def _account(db, *, onboarded=True):
    await db["users"].insert_one({"user_id": U, "timezone": "Europe/London",
                                  "onboarding": {"completed": onboarded},
                                  "credits": {**credits._effective_credits({})[0], "topup_balance": 1000.0}})
    await db["client_groups"].insert_many([
        {"user_id": U, "id": G1, "name": "Aura", "ad_account_currency": "GBP", "meta_ad_account_id": "act_1",
         "ghl_location_id": "loc-1", "targets": {"cpl": 12}},
        {"user_id": U, "id": G2, "name": "Beaulea", "ad_account_currency": "GBP", "meta_ad_account_id": "act_2",
         "ghl_location_id": "loc-2"},
        {"user_id": U, "id": G3, "name": "Calm", "ad_account_currency": "USD"},
        {"user_id": "someone@else.com", "id": "grp-x", "name": "Other"},
    ])


async def _stock(db):
    return await db[AGENTS].find_one({"user_id": U, "type": TYPE_STOCK})


async def _finished_preview(db, clients=(G1, G2), state="done"):
    agent = await _stock(db)
    await db[RUNS].insert_one({"_id": "run-p", "agent_id": agent["_id"], "kind": "preview", "phase": "done",
                               "as_of": datetime.utcnow(), "clients": {g: {"state": state} for g in clients}})


MB_ANSWERS = [
    ("clients", {"client_group_ids": [G1, G2]}),
    ("cpl", {"default": 10, "clients": {G2: 8}}),
    ("playbook", {"confirmed": True}),
    ("permissions", {"ads": True, "adsets": False, "campaigns": False}),
    ("schedule", {"time": "08:30", "tz": "Europe/London", "days": "weekdays"}),
    ("slack", {"channel_id": "C123"}),
]
SC_ANSWERS = [
    ("clients", {"client_group_ids": "all"}),
    ("outcomes", {"booked": {"kind": "tag", "value": "Booked Call", "label": "Booked Call"},
                  "closed": {"kind": "stage", "value": "st-won", "label": "Sales / Won"}}),
    ("speed", {"minutes": 5}),
    ("schedule", {"time": "09:00", "tz": "Europe/London", "days": "daily"}),
    ("slack", {"channel_id": None}),
]


async def _media_buyer_done(db):
    for step, body in MB_ANSWERS:
        last = await s.save_step(db, U, s.MEDIA_BUYER, step, body)
        if step == "clients":
            await _finished_preview(db)   # a practice run the owner asked for: lets it switch on
    return last


async def _sales_coach_done(db):
    for step, body in SC_ANSWERS:
        out = await s.save_step(db, U, s.SALES_COACH, step, body)
    return out


# ---------------------------------------------------------------------------
# Order and status
# ---------------------------------------------------------------------------

async def test_a_new_account_is_locked_behind_birdy(mock_db):
    await _account(mock_db, onboarded=False)
    out = await s.state(mock_db, U)
    assert out["order"] == ["birdy", "media_buyer", "sales_coach", "sub_agents"]
    a = out["agents"]
    assert a["birdy"] == {"status": "open"}
    assert (a["media_buyer"]["status"], a["media_buyer"]["waits_on"]) == ("locked", "birdy")
    assert (a["sales_coach"]["status"], a["sales_coach"]["waits_on"]) == ("locked", "media_buyer")
    assert a["sub_agents"] == {"status": "locked", "waits_on": "sales_coach", "skipped": False, "completed_at": None}
    with pytest.raises(s.SetupError) as e:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1]})
    assert e.value.status == 423 and e.value.detail == {"code": "agent_locked", "agent": "media_buyer",
                                                        "waits_on": "birdy"}


async def test_after_onboarding_media_buyer_opens_on_its_first_step(mock_db):
    await _account(mock_db)
    mb = (await s.state(mock_db, U))["agents"]["media_buyer"]
    assert mb["status"] == "open" and mb["waits_on"] is None
    assert mb["order"] == ["clients", "cpl", "playbook", "permissions", "schedule", "slack"]
    assert mb["current"] == "clients" and mb["steps"] == {k: False for k in mb["order"]}
    assert [k["step"] for k in mb["known"]] == mb["order"] and all(k["value"] is None for k in mb["known"])
    assert mb["values"] == {} and mb["completed_at"] is None


async def test_steps_go_in_order_the_first_time(mock_db):
    await _account(mock_db)
    with pytest.raises(s.SetupError) as e:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, "cpl", {"default": 10, "clients": {}})
    assert e.value.status == 409
    assert e.value.detail == {"code": "step_out_of_order", "agent": "media_buyer", "step": "cpl", "expected": "clients"}
    for bad in (("media_buyer", "budgets"), ("birdy", "clients"), ("sales_coach", "cpl")):
        with pytest.raises(s.SetupError) as e:
            await s.save_step(mock_db, U, *bad, {})
        assert e.value.status in (404, 423)


async def test_a_done_step_can_be_saved_again(mock_db):
    await _account(mock_db)
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1]})
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "cpl", {"default": 10, "clients": {}})
    out = await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1, G2]})
    mb = out["agents"]["media_buyer"]
    assert mb["values"]["clients"] == {"client_group_ids": [G1, G2]} and mb["current"] == "playbook"


async def test_media_buyer_finishes_turns_on_and_unlocks_sales_coach(mock_db):
    await _account(mock_db)
    out = await _media_buyer_done(mock_db)
    mb = out["agents"]["media_buyer"]
    assert mb["status"] == "done" and mb["current"] is None and mb["completed_at"]
    assert mb["activation"] == {"status": "active", "missing": []}
    assert (await _stock(mock_db))["status"] == "active"
    assert out["agents"]["sales_coach"]["status"] == "open"
    assert out["agents"]["sub_agents"]["status"] == "locked"
    assert {k["step"]: k["value"] for k in mb["known"]} == {
        "clients": "2 clients: Aura, Beaulea", "cpl": "£10 for every client; Beaulea £8",
        "playbook": "Asks first before pausing, adding or reviving ads",
        "permissions": "Can change ads; never budgets", "schedule": "08:30 Europe/London, weekdays",
        "slack": "C123"}
    # editing later is allowed and keeps it done
    again = await s.save_step(mock_db, U, s.MEDIA_BUYER, "slack", {"channel_id": None})
    assert again["agents"]["media_buyer"]["status"] == "done"
    assert again["agents"]["media_buyer"]["values"]["slack"] == {"channel_id": None}


async def test_finishing_does_not_force_an_agent_that_is_not_ready(mock_db):
    await _account(mock_db)
    for step, body in MB_ANSWERS:
        out = await s.save_step(mock_db, U, s.MEDIA_BUYER, step, body)
        if step == "clients":
            await _finished_preview(mock_db, clients=(G1,))   # the preview didn't cover Beaulea
    mb = out["agents"]["media_buyer"]
    assert mb["status"] == "done"
    assert mb["activation"]["status"] == "not_ready" and mb["activation"]["missing"]
    assert (await _stock(mock_db))["status"] != "active"


async def test_practice_run_is_optional_and_not_a_step(mock_db):
    await _account(mock_db)
    for step, body in MB_ANSWERS:
        out = await s.save_step(mock_db, U, s.MEDIA_BUYER, step, body)
    mb = out["agents"]["media_buyer"]
    assert mb["status"] == "done" and "practice" not in mb["order"]     # done with no practice run
    assert mb["activation"]["status"] == "not_ready"                    # daily runs still wait for one
    assert out["agents"]["sales_coach"]["status"] == "open"
    assert (await s.practice_status(mock_db, U))["status"] == "none"
    with pytest.raises(s.SetupError) as e:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, "practice", {"confirmed": True})
    assert e.value.status == 404
    agent = await _stock(mock_db)
    await mock_db[RUNS].insert_one({"_id": "r1", "agent_id": agent["_id"], "kind": "preview", "phase": "clients",
                                    "as_of": datetime.utcnow(), "clients": {}})
    assert (await s.practice_status(mock_db, U)) == {"status": "running", "summary": None, "job_id": "r1"}
    await mock_db[RUNS].update_one({"_id": "r1"}, {"$set": {"phase": "done", "clients": {G1: {"state": "done"}}}})
    done = await s.practice_status(mock_db, U)
    assert done["status"] == "done" and done["summary"]["clients"] == 1


async def test_sales_coach_then_sub_agents(mock_db):
    await _account(mock_db)
    await _media_buyer_done(mock_db)
    out = await _sales_coach_done(mock_db)
    sc_state = out["agents"]["sales_coach"]
    assert sc_state["status"] == "done" and sc_state["completed_at"]
    assert sc_state["values"]["clients"] == {"client_group_ids": "all"}
    assert sc_state["values"]["speed"] == {"minutes": 5}
    assert {k["step"]: k["value"] for k in sc_state["known"]}["outcomes"] == "Booked: Booked Call; closed: Sales / Won"
    row = await mock_db[TEAM_AGENTS].find_one({"_id": f"sales_coach:{U}"})
    assert row["settings"]["outcomes"]["closed"] == {"kind": "stage", "value": "st-won", "label": "Sales / Won"}
    assert row["settings"]["schedule"] == {"time": "09:00", "tz": "Europe/London", "days": "daily"}
    assert out["agents"]["sub_agents"]["status"] == "open"
    done = await s.finish_sub_agents(mock_db, U, True)
    assert done["agents"]["sub_agents"]["status"] == "done" and done["agents"]["sub_agents"]["skipped"] is True


async def test_sub_agents_cannot_finish_before_sales_coach(mock_db):
    await _account(mock_db)
    with pytest.raises(s.SetupError) as e:
        await s.finish_sub_agents(mock_db, U, False)
    assert e.value.status == 423 and e.value.detail["agent"] == "sub_agents"


# ---------------------------------------------------------------------------
# Each answer lands where the setting lives
# ---------------------------------------------------------------------------

async def test_clients_create_the_stock_agent_and_set_its_scope(mock_db):
    await _account(mock_db)
    assert await _stock(mock_db) is None
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": "all"})
    agent = await _stock(mock_db)
    assert agent["status"] == "draft" and agent["scope"]["schedule"]["tz"] == "Europe/London"
    assert set(agent["scope"]["client_group_ids"]) == {G1, G2, G3}
    mb = (await s.state(mock_db, U))["agents"]["media_buyer"]
    assert mb["values"]["clients"] == {"client_group_ids": "all"} and mb["known"][0]["value"] == "All clients"
    with pytest.raises(s.SetupError) as e:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": ["grp-x"]})
    assert e.value.status == 400


async def test_cpl_sets_zones_and_client_targets(mock_db):
    await _account(mock_db)
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1, G2]})
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "cpl", {"default": 10, "clients": {G2: 8}})
    settings = (await _stock(mock_db))["settings"]
    assert settings["zones"] == {"currency": "GBP", "green_under": 10, "red_over": 13.0}
    assert settings["clients"][G2]["zones"] == {"green_under": 8, "red_over": 10.4}
    assert (await mock_db["client_groups"].find_one({"id": G2}))["targets"]["cpl"] == 8
    assert (await mock_db["client_groups"].find_one({"id": G1}))["targets"]["cpl"] == 12   # untouched
    # saved again without Beaulea: it goes back to the default
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "cpl", {"default": 9, "clients": {}})
    settings = (await _stock(mock_db))["settings"]
    assert settings["zones"]["green_under"] == 9 and settings["clients"][G2]["zones"] is None
    for bad in ({"default": -1, "clients": {}}, {"default": "ten", "clients": {}}, {"default": 1, "clients": {G1: "x"}},
                {"default": 1, "clients": {"grp-x": 3}}):
        with pytest.raises(s.SetupError) as e:
            await s.save_step(mock_db, U, s.MEDIA_BUYER, "cpl", bad)
        assert e.value.status == 400, bad


async def test_permissions_schedule_and_slack_land_on_the_stock_agent(mock_db):
    await _account(mock_db)
    for step, body in MB_ANSWERS:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, step, body)
    agent = await _stock(mock_db)
    assert agent["permissions"] == {"ads": True, "adsets": False, "campaigns": False, "budgets": False}
    assert agent["scope"]["schedule"] == {"time": "08:30", "tz": "Europe/London", "days": "weekdays"}
    assert agent["settings"]["slack"]["channel_id"] == "C123"
    for step, bad in (("permissions", {"ads": "yes", "adsets": True, "campaigns": False}),
                      ("schedule", {"time": "8am", "tz": "Europe/London"}),
                      ("schedule", {"time": "08:00", "tz": "Mars/Base"}),
                      ("schedule", {"time": "08:00", "tz": "UTC", "days": "sundays"}),
                      ("slack", {"channel_id": 5}), ("playbook", {"confirmed": False})):
        with pytest.raises(s.SetupError) as e:
            await s.save_step(mock_db, U, s.MEDIA_BUYER, step, bad)
        assert e.value.status == 400, (step, bad)


async def test_sales_coach_bad_answers_are_refused(mock_db):
    await _account(mock_db)
    await _media_buyer_done(mock_db)
    await s.save_step(mock_db, U, s.SALES_COACH, "clients", {"client_group_ids": [G1]})
    for bad in ({"booked": {"kind": "vibe", "value": "x", "label": "x"}, "closed": SC_ANSWERS[1][1]["closed"]},
                {"booked": {"kind": "tag", "value": "", "label": "x"}, "closed": SC_ANSWERS[1][1]["closed"]},
                {"booked": SC_ANSWERS[1][1]["booked"]}):
        with pytest.raises(s.SetupError) as e:
            await s.save_step(mock_db, U, s.SALES_COACH, "outcomes", bad)
        assert e.value.status == 400
    await s.save_step(mock_db, U, s.SALES_COACH, "outcomes", SC_ANSWERS[1][1])
    for bad in ({"minutes": 0}, {"minutes": 2.5}, {"minutes": True}, {}):
        with pytest.raises(s.SetupError) as e:
            await s.save_step(mock_db, U, s.SALES_COACH, "speed", bad)
        assert e.value.status == 400


def test_weekdays_schedule_skips_the_weekend():
    from jobs.agent_jobs import _is_due
    agent = {"scope": {"schedule": {"time": "07:00", "tz": "UTC", "days": "weekdays"}}, "last_run_at": None}
    saturday = datetime(2026, 11, 21, 9, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    assert _is_due(agent, saturday) is False and _is_due(agent, monday) is True
    daily = {"scope": {"schedule": {"time": "07:00", "tz": "UTC"}}, "last_run_at": None}
    assert _is_due(daily, saturday) is True


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

@pytest.fixture
def client(mock_mongo_client, monkeypatch):
    import routers.team_setup as router_mod

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(router_mod, "get_mongo_client", fake_client)
    app = FastAPI()
    app.include_router(router_mod.router)
    app.dependency_overrides[router_mod.get_current_user] = lambda: U
    return TestClient(app)


async def test_router_endpoints(mock_db, client):
    await _account(mock_db, onboarded=False)
    r = client.get("/api/team/setup")
    assert r.status_code == 200 and r.json()["agents"]["media_buyer"]["status"] == "locked"
    r = client.put("/api/team/setup/media_buyer/clients", json={"client_group_ids": [G1]})
    assert r.status_code == 423
    assert r.json() == {"detail": {"code": "agent_locked", "agent": "media_buyer", "waits_on": "birdy"}}
    await mock_db["users"].update_one({"user_id": U}, {"$set": {"onboarding.completed": True}})
    r = client.put("/api/team/setup/media_buyer/cpl", json={"default": 1, "clients": {}})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "step_out_of_order"
    r = client.put("/api/team/setup/media_buyer/clients", json={"client_group_ids": [G1]})
    assert r.status_code == 200 and r.json()["agents"]["media_buyer"]["current"] == "cpl"
    assert client.put("/api/team/setup/media_buyer/cpl", json={"default": "x"}).status_code == 400
    assert client.put("/api/team/setup/media_buyer/nope", json={}).status_code == 404
    assert client.get("/api/team/setup/media_buyer/practice").json() == {"status": "none", "summary": None,
                                                                         "job_id": None}
    assert client.get("/api/team/setup/sales_coach/outcome-options").json() == {"tags": [], "pipelines": []}
    r = client.post("/api/team/setup/sub_agents/finish", json={"skipped": True})
    assert r.status_code == 423 and r.json()["detail"]["agent"] == "sub_agents"


async def test_practice_post_refuses_without_clients_and_with_history_not_ready(mock_db, client):
    await _account(mock_db)
    r = client.post("/api/team/setup/media_buyer/practice")
    assert r.status_code == 409
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1]})
    r = client.post("/api/team/setup/media_buyer/practice")
    detail = r.json()["detail"]
    assert r.status_code == 409 and detail["code"] == "ad_history_loading" and detail["clients"] == ["Aura"]
    assert "still loading the ad history for 1 client (Aura)" in detail["message"]


async def test_practice_post_runs_the_stock_preview(mock_db, mock_mongo_client, monkeypatch):
    import routers.team_setup as router_mod
    from ai.agents.stock import runner as stock_runner
    await _account(mock_db)
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1]})
    seen = {}

    async def no_block(db, user_id, agent):
        return None

    async def fake_advance(db, mongo_client, run, agent, *, deadline, now_utc):
        seen["kind"] = run["kind"]
        await db[stock_runner.RUNS].update_one({"_id": run["_id"]}, {"$set": {"phase": stock_runner.DONE}})
        return {**run, "phase": stock_runner.DONE}

    monkeypatch.setattr("routers.agent_stock.preview_blocker", no_block)
    monkeypatch.setattr(stock_runner, "advance_run", fake_advance)
    out = await router_mod.start_practice_run(mock_db, mock_mongo_client, U)
    assert out["status"] == "done" and out["job_id"] and seen["kind"] == stock_runner.PREVIEW
    assert (await s.practice_status(mock_db, U))["job_id"] == out["job_id"]


# ---------------------------------------------------------------------------
# Locks at every entry point
# ---------------------------------------------------------------------------

@pytest.fixture
def chat_checks(monkeypatch):
    """routers.chat._checks with the credit gate and the provider recorded instead of run."""
    import routers.chat as chat_router
    seen = {"credits": 0}

    async def gate(user, client):
        seen["credits"] += 1

    async def provider(user, db):
        return "provider"

    monkeypatch.setattr(chat_router, "check_credits", gate)
    monkeypatch.setattr(chat_router, "get_provider_for_user", provider)

    async def run(db, **kw):
        return await chat_router._checks(db, U, ChatRequest(message="hi", **kw), None)
    run.seen = seen
    return run


async def test_direct_chat_is_refused_for_a_locked_agent(mock_db, chat_checks):
    await _account(mock_db)
    with pytest.raises(HTTPException) as e:
        await chat_checks(mock_db, agent="media_buyer")
    assert e.value.status_code == 423 and e.value.detail == {"code": "agent_locked", "agent": "media_buyer",
                                                             "waits_on": None}
    with pytest.raises(HTTPException) as e:
        await chat_checks(mock_db, agent="sales_coach", page="team_setup")
    assert e.value.status_code == 423 and e.value.detail["waits_on"] == "media_buyer"
    # its own setup chat, once open, is allowed and skips the credit gate
    await chat_checks(mock_db, agent="media_buyer", page="team_setup")
    assert chat_checks.seen["credits"] == 0
    # Birdy is never locked; an ordinary chat still meets the credit gate
    await chat_checks(mock_db, agent="birdy")
    assert chat_checks.seen["credits"] == 1
    with pytest.raises(HTTPException) as e:
        await chat_checks(mock_db, agent="birdy", page="team_setup")
    assert e.value.status_code == 422


async def test_a_set_up_agent_takes_chats_and_its_setup_chat_edits(mock_db, chat_checks):
    await _account(mock_db)
    await _media_buyer_done(mock_db)
    await chat_checks(mock_db, agent="media_buyer")
    await chat_checks(mock_db, agent="media_buyer", page="team_setup")
    assert chat_checks.seen["credits"] == 1


async def test_chat_endpoint_answers_423_json(mock_db, mock_mongo_client, monkeypatch):
    import routers.chat as chat_router
    await _account(mock_db)

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def no_op(*a, **kw):
        return None

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "check_credits", no_op)
    monkeypatch.setattr(chat_router, "get_provider_for_user", no_op)
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[chat_router.get_current_user] = lambda: U
    r = TestClient(app).post("/api/chat", json={"message": "hi", "agent": "sales_coach"})
    assert r.status_code == 423
    assert r.json() == {"detail": {"code": "agent_locked", "agent": "sales_coach", "waits_on": "media_buyer"}}
    r = TestClient(app).post("/api/chat/stream", json={"message": "hi", "agent": "media_buyer"})
    assert r.status_code == 423


async def test_birdy_has_no_ask_for_a_locked_agent(mock_db, registry):
    await _account(mock_db)
    await _media_buyer_done(mock_db)
    p = TeamProvider({"birdy": [say("Done.")]})
    await run_chat(provider=p, tool_registry=registry, db=mock_db, user_id=U, message="hi", agent="birdy")
    offered = p.offered("birdy")
    assert "ask_media_buyer" in offered and "ask_sales_coach" not in offered
    assert "Sales Coach is not set up yet" in p.calls[0][2][0]["content"]


async def test_an_ask_to_a_locked_agent_is_refused_in_plain_words(mock_db):
    await _account(mock_db)
    turn = rt.Turn(root_agent=defs.BIRDY)
    frame = rt.Frame(turn=turn, agent=defs.BIRDY, depth=0, chain=(defs.BIRDY,), step={}, usage=rt.Usage(agent="birdy", depth=0))
    ctx = {"db": mock_db, "user_id": U, "locked": await s.locked_agents(mock_db, U)}
    out = await delegation.handle("ask_sales_coach", {"question": "calls?", "client": "agency"}, frame, ctx)
    assert '"agent_locked"' in out and "Sales Coach isn't set up yet" in out and turn.asks == 0
    assert delegation.tool_schemas(frame, [], locked=ctx["locked"]) == [delegation._RECALL_SCHEMA]


async def test_sub_agents_are_hidden_while_locked(mock_db):
    await _account(mock_db)
    locked = await s.locked_agents(mock_db, U)
    assert locked == {"media_buyer", "sales_coach", "sub_agents"}
    turn = rt.Turn(root_agent=defs.MEDIA_BUYER)
    frame = rt.Frame(turn=turn, agent=defs.MEDIA_BUYER, depth=0, chain=(defs.MEDIA_BUYER,), step={},
                     usage=rt.Usage(agent="media_buyer", depth=0))
    assert await delegation.subs_for(mock_db, frame, U, locked=locked) == []


async def test_mcp_ask_refuses_a_locked_agent(mock_db, set_current_user):
    from ai.mcp import team_mcp
    await _account(mock_db)
    set_current_user(U)
    with pytest.raises(ToolError) as e:
        await team_mcp.ask_media_buyer.fn(question="How is Aura doing?")
    assert "Media Buyer isn't set up yet" in str(e.value) and "Nothing was asked" in str(e.value)
    assert await mock_db["ai_conversation_log"].count_documents({}) == 0


async def test_scheduled_sub_agent_run_is_skipped_while_locked(mock_db, mock_mongo_client):
    from ai.team import scheduled
    await _account(mock_db)
    out = await scheduled.run_one(mock_db, mock_mongo_client, {"user_id": U, "parent": "media_buyer", "id": "s1"},
                                  local_date="2026-11-21", deadline=None, provider=object(), tool_registry=object())
    assert out["status"] == "skipped" and out["reason"] == "agent_locked"
    assert "Sub-agents aren't available yet" in out["answer"] and len(out["summary"]) <= 60


async def test_lock_read_failure_locks_nothing(mock_db, monkeypatch):
    async def broken(db, user_id):
        raise RuntimeError("db down")
    monkeypatch.setattr(s, "statuses", broken)
    assert await s.locked_agents(mock_db, U) == frozenset()


# ---------------------------------------------------------------------------
# The setup chat
# ---------------------------------------------------------------------------

def _setup_registry(base):
    setup_chat.register_setup_chat_tools(base)
    return base


async def test_setup_chat_adds_the_current_step_block_when_the_model_forgets(mock_db, registry):
    await _account(mock_db)
    reg = _setup_registry(registry)
    p = TeamProvider({"default": [say("Hi, I'm Media Buyer. Which clients should I run?")]})
    out = await run_chat(provider=p, tool_registry=reg, db=mock_db, user_id=U, message="hi",
                         agent="media_buyer", page="team_setup")
    blocks = [c for c in out["agent_changes"] if c.get("ui_block")]
    assert len(blocks) == 1 and blocks[0]["tool"] == "setup_guard"
    block = blocks[0]["ui_block"]
    assert block["type"] == "clients" and block["agent"] == "media_buyer" and block["value"] is None
    assert {o["id"] for o in block["options"]} == {G1, G2, G3}
    offered = p.calls[0][1]
    assert set(offered) == {"get_client_groups", "get_setup_state", "show_setup_step", "save_setup_step"}   # no asks
    # setup chats stay out of the history list; the state names the latest one so the app can reopen it
    assert (await s.state(mock_db, U))["agents"]["media_buyer"]["chat_session_id"] == out["session_id"]
    assert (await s.state(mock_db, U))["agents"]["sales_coach"]["chat_session_id"] is None
    system = p.calls[0][2][0]["content"]
    assert system.startswith("You are Media Buyer") and "<birdy_knows>" in system and "ONE step at a time" in system
    assert '"current": "clients"' in system


async def test_setup_chat_tool_block_is_not_doubled(mock_db, registry):
    await _account(mock_db)
    await s.save_step(mock_db, U, s.MEDIA_BUYER, "clients", {"client_group_ids": [G1, G2]})
    reg = _setup_registry(registry)
    p = TeamProvider({"default": [call("show_setup_step", step="cpl", agent="sales_coach"),
                                  say("What cost per lead should I aim for? Aura's target is £12.")]})
    out = await run_chat(provider=p, tool_registry=reg, db=mock_db, user_id=U, message="Saved clients",
                         agent="media_buyer", page="team_setup")
    blocks = [c for c in out["agent_changes"] if c.get("ui_block")]
    assert [b["tool"] for b in blocks] == ["show_setup_step"]
    block = blocks[0]["ui_block"]
    assert block["type"] == "cpl" and block["agent"] == "media_buyer" and block["currency"] == "GBP"
    assert {c["id"]: c["target"] for c in block["clients"]} == {G1: 12, G2: None}


async def test_setup_chat_step_blocks_carry_their_form_data(mock_db):
    await _account(mock_db)
    for step, body in MB_ANSWERS[:3]:
        await s.save_step(mock_db, U, s.MEDIA_BUYER, step, body)
    playbook = await setup_chat.block(mock_db, U, "media_buyer", "playbook")
    assert playbook["modes"] == {"remove": "ask", "add": "ask", "revive": "ask"} and playbook["creative"]["max_ads_live"]
    perms = await setup_chat.block(mock_db, U, "media_buyer", "permissions")
    assert perms["value"] is None and perms["budgets"] is False    # offered, not chosen
    assert perms["suggestion"] == {"ads": True, "adsets": True, "campaigns": False}
    sched = await setup_chat.block(mock_db, U, "media_buyer", "schedule")
    assert sched["value"] is None and sched["default"] == {"time": "07:00", "tz": "Europe/London", "days": "daily"}
    slack = await setup_chat.block(mock_db, U, "media_buyer", "slack")
    assert slack["connected"] is False
    practice = await setup_chat.block(mock_db, U, "media_buyer", "practice")
    assert practice["status"] == "none"
    bad = await setup_chat.show_setup_step(mock_db, U, agent="media_buyer", step="outcomes")
    assert bad["ok"] is False


async def test_setup_chat_after_both_agents_offers_sub_agents(mock_db, registry):
    await _account(mock_db)
    await _media_buyer_done(mock_db)
    await _sales_coach_done(mock_db)
    reg = _setup_registry(registry)
    p = TeamProvider({"default": [say("I'm set up. Want a sub-agent?")]})
    out = await run_chat(provider=p, tool_registry=reg, db=mock_db, user_id=U, message="Saved slack",
                         agent="sales_coach", page="team_setup")
    assert [c["ui_block"]["type"] for c in out["agent_changes"] if c.get("ui_block")] == ["sub_agents"]
    assert "propose_sub_agent" in p.calls[0][1] and "ask_birdy" not in p.calls[0][1]
    assert "Sub-agents (the last, optional step)" in p.calls[0][2][0]["content"]


async def test_setup_chat_before_sub_agents_open_has_no_propose(mock_db, registry):
    await _account(mock_db)
    reg = _setup_registry(registry)
    p = TeamProvider({"default": [say("Hi.")]})
    await run_chat(provider=p, tool_registry=reg, db=mock_db, user_id=U, message="hi",
                   agent="media_buyer", page="team_setup")
    assert "propose_sub_agent" not in p.calls[0][1]


async def test_setup_tools_only_for_the_setup_chat():
    from ai.orchestrator import _PAGE_ONLY_TOOLS
    assert set(setup_chat.TOOL_NAMES) <= _PAGE_ONLY_TOOLS
    assert (await setup_chat.get_setup_state(None, U, agent=None))["ok"] is False


# ---------------------------------------------------------------------------
# Sales Coach's settings in use
# ---------------------------------------------------------------------------

async def _coached(db, clients=(G1,)):
    await db[TEAM_AGENTS].insert_one({"_id": f"sales_coach:{U}", "user_id": U, "key": "sales_coach", "settings": {
        "client_group_ids": list(clients), "outcomes": SC_ANSWERS[1][1], "speed_minutes": 5}})


async def test_outcome_options_read_the_caches_only(mock_db):
    await _account(mock_db)
    await mock_db["client_groups"].update_one({"id": G1}, {"$set": {"gohighlevel_cache.metrics.tag_breakdown": {
        "booked call": 40, "new lead": 90}}})
    await mock_db[sc.TAG_CACHE].insert_one({"user_id": U, "group_id": G2, "preset": "maximum", "tags": {"won": 3}})
    await mock_db["ghl_pipelines"].insert_one({"user_id": U, "location_id": "loc-1", "pipelines": [
        {"id": "p1", "name": "Sales", "stages": [{"id": "st-new", "name": "New"}, {"id": "st-won", "name": "Won"}]}]})
    out = await sc.outcome_options(mock_db, U)
    assert [t["value"] for t in out["tags"]] == ["new lead", "booked call", "won"]
    assert out["pipelines"] == [{"id": "p1", "name": "Sales", "stages": [{"id": "st-new", "name": "New"},
                                                                        {"id": "st-won", "name": "Won"}]}]


async def test_outcome_options_degrade_to_empty_lists(mock_db, monkeypatch):
    await _account(mock_db)
    assert await sc.outcome_options(mock_db, U) == {"tags": [], "pipelines": []}

    async def broken(*a, **kw):
        raise RuntimeError("down")
    monkeypatch.setattr(s, "coached_ids", broken)
    assert await sc.outcome_options(mock_db, U) == {"tags": [], "pipelines": []}


async def test_sales_outcomes_say_no_data_when_nothing_is_cached(mock_db):
    await _account(mock_db)
    assert "error" in await sc.get_sales_outcomes(mock_db, U)        # nothing set up: never a guess
    await _coached(mock_db)
    out = await sc.get_sales_outcomes(mock_db, U)
    row = out["clients"][0]
    assert row["client_group_id"] == G1 and row["coached"] is True
    assert row["booked"]["count"] is None and row["booked"]["note"].startswith("no data")
    assert row["closed"]["count"] is None and row["closed"]["note"].startswith("no data")
    assert row["speed_to_lead"]["calls"] == 0 and row["speed_to_lead"]["note"].startswith("no data")
    assert "error" in await sc.get_sales_outcomes(mock_db, U, preset="forever")


async def test_sales_outcomes_count_from_the_caches(mock_db):
    await _account(mock_db)
    await _coached(mock_db)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    await mock_db[sc.TAG_CACHE].insert_one({"user_id": U, "group_id": G1, "preset": "last_30d",
                                            "tags": {"booked call": 7, "new lead": 30}})
    await mock_db["ghl_contacts"].insert_many([
        {"user_id": U, "client_group_id": G1, "contact_data": {"dateAdded": today + "T10:00:00Z", "opportunities": [
            {"pipelineStageId": "st-won", "status": "won"}]}},
        {"user_id": U, "client_group_id": G1, "contact_data": {"dateAdded": today + "T11:00:00Z", "opportunities": [
            {"pipeline_stage_id": "st-won"}, {"pipeline_stage_id": "st-new"}]}},
        {"user_id": U, "client_group_id": G1, "contact_data": {"dateAdded": "2020-01-01T10:00:00Z", "opportunities": [
            {"pipelineStageId": "st-won"}]}},   # outside the period
        {"user_id": U, "client_group_id": G1, "contact_data": {"dateAdded": today, "opportunities": [
            {"pipelineStageId": "st-new"}]}},
    ])
    await mock_db["hotprospector_leads"].insert_one({"user_id": U, "ghl_location_id": "loc-1", "lead_data": {
        "call_logs": [{"call_time_iso": today + "T10:05:00", "speed_to_lead": 120},
                      {"call_time_iso": today + "T11:30:00", "speed_to_lead": 600},
                      {"call_time_iso": today + "T12:00:00", "speed_to_lead": 0},        # not measured
                      {"call_time_iso": "2020-01-01T10:00:00", "speed_to_lead": 30}]}})   # outside the period
    out = await sc.get_sales_outcomes(mock_db, U, preset="last_30d")
    row = out["clients"][0]
    assert row["booked"] == {"count": 7}         # the tag, matched without case
    assert row["closed"] == {"count": 2}
    assert row["speed_to_lead"] == {"calls": 2, "average_minutes": 6.0, "target_minutes": 5, "within_target": 1,
                                    "within_target_pct": 50.0, "meets_target": False}
    assert out["definitions"]["booked"]["label"] == "Booked Call" and "never estimate" in out["basis"]


async def test_sales_coach_prompt_carries_its_setup(mock_db):
    await _account(mock_db)
    await _coached(mock_db)
    prompt = await defs.build_instructions(defs.SALES_COACH, mock_db, U, client_group_id=None, client_name=None,
                                           asked_by=None)
    assert "## Your setup" in prompt and '"booked_means"' in prompt and '"Aura"' in prompt
    assert '"speed_to_lead_target_minutes": 5' in prompt and sc.TOOL_NAME in prompt
    assert sc.TOOL_NAME in defs.tool_names(defs.SALES_COACH, nested=True)
    assert sc.TOOL_NAME not in defs.tool_names(defs.MEDIA_BUYER, nested=False)


async def test_sales_outcomes_tool_is_pinned_in_a_one_client_run():
    from ai.orchestrator import _AGENT_PINNED_GROUP_IDS_TOOLS
    assert sc.TOOL_NAME in _AGENT_PINNED_GROUP_IDS_TOOLS
    reg = ToolRegistry()
    sc.register_sales_coach_tools(reg)
    assert sc.TOOL_NAME in {s_["function"]["name"] for s_ in reg.get_schemas()}


async def test_the_agent_saves_and_changes_answers_from_the_chat(mock_db):
    """What the owner says in words is saved by the agent, and an earlier answer can be changed mid-setup."""
    await _account(mock_db)
    out = await setup_chat.save_setup_step(mock_db, U, agent="media_buyer", step="clients",
                                           answer={"client_group_ids": [G1, G2]})
    assert out["ok"] and out["saved"] == "clients" and out["current"] == "cpl"
    # a later step can't be saved before its turn, and the refusal says which step is expected
    early = await setup_chat.save_setup_step(mock_db, U, agent="media_buyer", step="schedule",
                                             answer={"time": "09:00", "tz": "Europe/London", "days": "daily"})
    assert early["ok"] is False and early["error"]["expected"] == "cpl"
    # changing the first answer while still on the second step
    changed = await setup_chat.save_setup_step(mock_db, U, agent="media_buyer", step="clients",
                                               answer={"client_group_ids": [G1]})
    assert changed["ok"] and changed["current"] == "cpl"
    assert (await _stock(mock_db))["scope"]["client_group_ids"] == [G1]
    bad = await setup_chat.save_setup_step(mock_db, U, agent="media_buyer", step="practice", answer={"confirmed": True})
    assert bad["ok"] is False
    assert (await setup_chat.save_setup_step(mock_db, U, agent="media_buyer", step="cpl", answer="10"))["ok"] is False


async def test_the_practice_form_is_shown_only_on_request(mock_db):
    await _account(mock_db)
    shown = await setup_chat.show_setup_step(mock_db, U, agent="media_buyer", step="practice")
    assert shown["ok"] and shown["ui_block"]["type"] == "practice"
    assert (await setup_chat.show_setup_step(mock_db, U, agent="sales_coach", step="practice"))["ok"] is False
    prompt = await setup_chat.system_prompt(mock_db, U, "media_buyer")
    assert "NOT a step" in prompt and "only a suggestion" in prompt
