"""Sub-agents (ai/agents/sub_agents, routers/sub_agents.py): validation, lifecycle, ownership, lessons,
scheduled-run plumbing, and that today's custom and stock agents are unaffected. mongomock only."""

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import routers.agents as agents_router
import routers.sub_agents as sub_router
from ai.agents import store as agent_store
from ai.agents.sub_agents import core as sub
from ai.agents.sub_agents import propose_tool
from ai.agents.sub_agents import schedule as sub_schedule
from ai.agents.sub_agents.tools import parent_tool_names, tool_label
from dependencies import get_current_user
from main import app

U = "owner@example.com"
OTHER = "other@example.com"


async def groups(db, *ids, user=U):
    for gid in ids:
        await db["client_groups"].insert_one({"id": gid, "user_id": user, "name": f"Client {gid}"})


def args(**over):
    base = dict(parent="media_buyer", name="Fatigue watcher", purpose="Spots tired ads",
                instructions="Look at frequency and CTR over 7 days.",
                tools=["get_ad_insights", "compare_periods"], clients="all", schedule=None)
    base.update(over)
    return base


async def propose(db, user=U, **over):
    return await sub.propose_sub_agent(db, user, **args(**over))


# ---------------------------------------------------------------------------
# The tool lists (merge point)
# ---------------------------------------------------------------------------

def test_parent_tool_lists_are_the_main_agents_read_tools():
    mb, sc = parent_tool_names("media_buyer"), parent_tool_names("sales_coach")
    assert {"get_ad_insights", "get_campaign_insights", "compare_periods"} <= mb
    assert {"get_call_center_stats", "get_call_recordings_summary"} <= sc
    assert "get_call_center_stats" not in mb and "get_ad_insights" not in sc
    # a sub-agent only reads and proposes, so no write tool is on offer
    assert "analyze_call_recordings" not in sc and "draft_creative_test" not in mb
    assert "render_ui" not in mb | sc  # browser-only tools are left out
    with pytest.raises(ValueError):
        parent_tool_names("birdy")


def test_tool_labels():
    assert tool_label("get_ad_insights") == "Ad results"
    assert tool_label("get_something_new") == "Something new"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

async def test_propose_stores_an_inert_proposal(mock_db):
    await groups(mock_db, "g1")
    s = await propose(mock_db, clients=["g1", "g1"], tools=["get_ad_insights", "get_ad_insights"],
                      schedule={"time": "08:30", "tz": "Europe/London", "days": ["fri", "mon"]})
    assert s["status"] == "proposed" and s["kind"] == "specialist" and s["parent"] == "media_buyer"
    assert s["created_by"] == "media_buyer" and s["approved_at"] is None
    assert s["clients"] == ["g1"] and s["tools"] == ["get_ad_insights"]
    assert s["schedule"] == {"time": "08:30", "timezone": "Europe/London", "days": ["mon", "fri"]}
    assert await mock_db[agent_store.AGENTS].count_documents({}) == 0  # nothing in `agents`


@pytest.mark.parametrize("over,code", [
    (dict(name="   "), "bad_name"),
    (dict(name="x" * 61), "bad_name"),
    (dict(name=None), "bad_name"),
    (dict(purpose=""), "bad_purpose"),
    (dict(purpose="x" * 141), "bad_purpose"),
    (dict(instructions="  "), "bad_instructions"),
    (dict(tools=[]), "bad_tools"),
    (dict(tools="get_ad_insights"), "bad_tools"),
    (dict(tools=["made_up_tool"]), "unknown_tool"),
    (dict(tools=["analyze_call_recordings"]), "tool_not_allowed"),       # Sales Coach's, not Media Buyer's
    (dict(tools=["create_alert"]), "tool_not_allowed"),                  # a write tool Media Buyer lacks
    (dict(parent="sales_coach", tools=["pause_creative_ads"]), "tool_not_allowed"),
    (dict(parent="birdy"), "bad_parent"),
    (dict(created_by="sales_coach"), "bad_creator"),
    (dict(clients=[]), "bad_clients"),
    (dict(clients="some"), "bad_clients"),
    (dict(schedule={"time": "25:00", "timezone": "Europe/London"}), "bad_schedule"),
    (dict(schedule={"time": "08:00", "timezone": "Mars/Base"}), "bad_schedule"),
    (dict(schedule={"time": "08:00", "timezone": "UTC", "days": ["someday"]}), "bad_schedule"),
    (dict(schedule={"time": "08:00", "timezone": "UTC", "every": 2}), "bad_schedule"),
])
async def test_propose_refuses(mock_db, over, code):
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, **over)
    assert e.value.status == 400 and e.value.code == code
    assert await mock_db[sub.SUB_AGENTS].count_documents({}) == 0


async def test_write_tool_refusal_says_the_parent_cant(mock_db):
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, tools=["create_alert"])
    assert "can't make the change" in e.value.body["detail"]


async def test_a_write_tool_is_refused_even_if_the_parent_has_it(mock_db):
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, tools=["draft_creative_test"])
    assert e.value.code == "tool_not_allowed"


async def test_clients_must_be_the_users(mock_db):
    await groups(mock_db, "mine")
    await groups(mock_db, "theirs", user=OTHER)
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, clients=["mine", "theirs"])
    assert e.value.code == "unknown_client"


async def test_duplicate_names_per_parent(mock_db):
    await propose(mock_db, name="Fatigue Watcher")
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, name="  fatigue   watcher ")
    assert e.value.status == 409 and e.value.code == "duplicate_name"
    assert "waiting for approval" in e.value.body["detail"]
    # Another parent, or another user, may use the name.
    await propose(mock_db, parent="sales_coach", tools=["get_call_center_stats"], name="Fatigue watcher")
    await propose(mock_db, user=OTHER, name="Fatigue watcher")


async def test_a_custom_agents_name_is_taken_under_media_buyer(mock_db):
    await agent_store.create_agent(mock_db, U, name="Pause rules")
    with pytest.raises(sub.SubAgentError) as e:
        await propose(mock_db, name="pause rules")
    assert e.value.code == "duplicate_name"
    await propose(mock_db, parent="sales_coach", tools=["get_call_center_stats"], name="Pause rules")


async def test_unique_index_backs_the_name_check(mock_mongo_client, mock_db):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    await propose(mock_db)
    doc = await mock_db[sub.SUB_AGENTS].find_one({})
    clash = {**doc, "_id": "x2"}
    with pytest.raises(Exception, match="(?i)duplicate"):
        await mock_db[sub.SUB_AGENTS].insert_one(clash)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_lifecycle(mock_db):
    s = await propose(mock_db)
    with pytest.raises(sub.SubAgentError) as e:
        await sub.pause_sub_agent(mock_db, U, s["id"])
    assert e.value.status == 409 and e.value.code == "wrong_status"
    with pytest.raises(sub.SubAgentError):
        await sub.resume_sub_agent(mock_db, U, s["id"])

    a = await sub.approve_sub_agent(mock_db, U, s["id"])
    assert a["status"] == "active" and a["approved_at"] is not None
    assert (await sub.approve_sub_agent(mock_db, U, s["id"]))["status"] == "active"  # twice is fine
    with pytest.raises(sub.SubAgentError):
        await sub.reject_sub_agent(mock_db, U, s["id"])  # too late to reject

    p = await sub.pause_sub_agent(mock_db, U, s["id"])
    assert p["status"] == "paused" and p["paused_at"] is not None
    assert p["instructions"] == a["instructions"] and p["tools"] == a["tools"]  # pause keeps everything
    with pytest.raises(sub.SubAgentError):
        await sub.approve_sub_agent(mock_db, U, s["id"])
    assert (await sub.resume_sub_agent(mock_db, U, s["id"]))["status"] == "active"


async def test_reject_deletes_the_proposal(mock_db):
    s = await propose(mock_db)
    assert await sub.reject_sub_agent(mock_db, U, s["id"]) == {"deleted": s["id"]}
    assert await sub.get_sub_agent(mock_db, U, s["id"]) is None
    await propose(mock_db)  # the name is free again


async def test_update(mock_db):
    await groups(mock_db, "g1", "g2")
    s = await propose(mock_db, schedule={"time": "07:00", "timezone": "UTC"})
    u = await sub.update_sub_agent(mock_db, U, s["id"], {
        "name": "CTR watcher", "tools": ["get_campaign_insights"], "clients": ["g2"], "schedule": None})
    assert u["name"] == "CTR watcher" and u["tools"] == ["get_campaign_insights"]
    assert u["clients"] == ["g2"] and u["schedule"] is None and u["status"] == "proposed"
    assert u["updated_at"] >= s["updated_at"]
    for patch, code in [({"tools": ["analyze_call_recordings"]}, "tool_not_allowed"),
                        ({"clients": ["nope"]}, "unknown_client"),
                        ({"parent": "sales_coach"}, "bad_field"),
                        ({"status": "active"}, "bad_field"),
                        ({"name": ""}, "bad_name")]:
        with pytest.raises(sub.SubAgentError) as e:
            await sub.update_sub_agent(mock_db, U, s["id"], patch)
        assert e.value.code == code
    other = await propose(mock_db, name="Second")
    with pytest.raises(sub.SubAgentError) as e:
        await sub.update_sub_agent(mock_db, U, other["id"], {"name": "ctr WATCHER"})
    assert e.value.code == "duplicate_name"
    same = await sub.update_sub_agent(mock_db, U, s["id"], {"name": "CTR watcher"})  # its own name
    assert same["name"] == "CTR watcher"


async def test_delete_removes_lessons_and_runs(mock_db):
    s = await propose(mock_db, schedule={"time": "07:00", "timezone": "UTC"})
    keep = await propose(mock_db, name="Keeper")
    await sub.approve_sub_agent(mock_db, U, s["id"])
    await sub.add_sub_agent_lesson(mock_db, U, s["id"], "Weekends are slow for this client")
    await sub.add_sub_agent_lesson(mock_db, U, keep["id"], "Keep me")
    await mock_db[sub_schedule.RUNS].insert_one({"_id": "r1", "sub_agent_id": s["id"], "local_date": "2026-11-20"})
    assert await sub.delete_sub_agent(mock_db, U, s["id"]) == {"deleted": s["id"]}
    assert await mock_db[sub.SUB_AGENTS].count_documents({"_id": s["id"]}) == 0
    assert await mock_db[agent_store.MEMORY].count_documents({"agent_id": s["id"]}) == 0
    assert await mock_db[sub_schedule.RUNS].count_documents({"sub_agent_id": s["id"]}) == 0
    assert len(await sub.list_sub_agent_lessons(mock_db, U, keep["id"])) == 1


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------

async def test_another_users_sub_agent_is_not_found(mock_db):
    s = await propose(mock_db)
    assert await sub.get_sub_agent(mock_db, OTHER, s["id"]) is None
    assert await sub.list_sub_agents(mock_db, OTHER) == []
    for fn in (sub.approve_sub_agent, sub.reject_sub_agent, sub.pause_sub_agent, sub.resume_sub_agent,
               sub.delete_sub_agent, sub.list_sub_agent_lessons):
        with pytest.raises(sub.SubAgentError) as e:
            await fn(mock_db, OTHER, s["id"])
        assert e.value.status == 404
    with pytest.raises(sub.SubAgentError) as e:
        await sub.update_sub_agent(mock_db, OTHER, s["id"], {"name": "Mine now"})
    assert e.value.status == 404
    with pytest.raises(sub.SubAgentError) as e:
        await sub.add_sub_agent_lesson(mock_db, OTHER, s["id"], "x")
    assert e.value.status == 404
    assert (await sub.get_sub_agent(mock_db, U, s["id"]))["status"] == "proposed"


# ---------------------------------------------------------------------------
# Listing, and today's custom / stock agents
# ---------------------------------------------------------------------------

async def test_list_filters_and_custom_agents_backfill(mock_db):
    custom = await agent_store.create_agent(mock_db, U, name="Pause rules")
    await agent_store.update_agent(mock_db, U, custom["_id"], {"status": "active", "description": "Pauses losers"})
    await agent_store.create_agent(mock_db, U, type=agent_store.TYPE_STOCK)
    mb = await propose(mock_db)
    sc = await propose(mock_db, parent="sales_coach", tools=["get_call_center_stats"], name="Call QA")
    await sub.approve_sub_agent(mock_db, U, sc["id"])

    rows = await sub.list_sub_agents(mock_db, U, "media_buyer")
    assert [r["id"] for r in rows] == [custom["_id"], mb["id"]]  # oldest first; the stock agent is not one
    c = rows[0]
    assert c["kind"] == "custom" and c["parent"] == "media_buyer" and c["status"] == "active"
    assert c["purpose"] == "Pauses losers" and c["tools"] == []
    assert c["schedule"] == {"time": "07:00", "timezone": "Europe/London", "days": list(sub.DAYS)}

    assert [r["id"] for r in await sub.list_sub_agents(mock_db, U, "sales_coach")] == [sc["id"]]
    assert [r["id"] for r in await sub.list_sub_agents(mock_db, U, "media_buyer", kind="specialist")] == [mb["id"]]
    assert [r["id"] for r in await sub.list_sub_agents(mock_db, U, status="active", kind="specialist")] == [sc["id"]]
    assert [r["id"] for r in await sub.list_sub_agents(mock_db, U, "media_buyer", status="proposed")] == [mb["id"]]
    assert len(await sub.list_sub_agents(mock_db, U)) == 3
    assert (await sub.get_sub_agent(mock_db, U, custom["_id"]))["kind"] == "custom"
    with pytest.raises(sub.SubAgentError):
        await sub.list_sub_agents(mock_db, U, status="draft")


async def test_custom_agents_are_read_only_here(mock_db):
    custom = await agent_store.create_agent(mock_db, U, name="Pause rules")
    before = await mock_db[agent_store.AGENTS].find_one({"_id": custom["_id"]})
    for fn in (sub.approve_sub_agent, sub.reject_sub_agent, sub.pause_sub_agent, sub.resume_sub_agent,
               sub.delete_sub_agent):
        with pytest.raises(sub.SubAgentError) as e:
            await fn(mock_db, U, custom["_id"])
        assert e.value.status == 409 and e.value.code == "managed_elsewhere"
    with pytest.raises(sub.SubAgentError):
        await sub.update_sub_agent(mock_db, U, custom["_id"], {"name": "New"})
    assert await mock_db[agent_store.AGENTS].find_one({"_id": custom["_id"]}) == before
    assert (await sub.get_sub_agent(mock_db, U, custom["_id"]))["status"] == "proposed"  # a draft in setup
    await agent_store.update_agent(mock_db, U, custom["_id"], {"turned_off_at": datetime.utcnow()})
    assert (await sub.get_sub_agent(mock_db, U, custom["_id"]))["status"] == "paused"


async def test_specialists_never_reach_the_agent_paths(mock_db):
    """The tick, claims and GET /api/agents read `agents`; an active specialist is not there."""
    s = await propose(mock_db)
    await sub.approve_sub_agent(mock_db, U, s["id"])
    assert await agent_store.list_agents(mock_db, U) == []
    assert await mock_db[agent_store.AGENTS].count_documents({"status": "active"}) == 0


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------

async def test_lessons_private_and_readable_by_parent(mock_db):
    await groups(mock_db, "g1", "g2")
    a = await propose(mock_db, clients=["g1"])
    b = await propose(mock_db, name="Other")
    c = await propose(mock_db, parent="sales_coach", tools=["get_call_center_stats"], name="Calls")
    await sub.add_sub_agent_lesson(mock_db, U, a["id"], "Client g1 hates broad audiences", client_group_id="g1")
    await sub.add_sub_agent_lesson(mock_db, U, b["id"], "Check frequency first")
    await sub.add_sub_agent_lesson(mock_db, U, c["id"], "Ask about budget early")
    with pytest.raises(sub.SubAgentError) as e:
        await sub.add_sub_agent_lesson(mock_db, U, a["id"], "x", client_group_id="g2")  # not a client it covers
    assert e.value.code == "unknown_client"
    with pytest.raises(sub.SubAgentError):
        await sub.add_sub_agent_lesson(mock_db, U, a["id"], "x" * 281)

    assert [l["text"] for l in await sub.list_sub_agent_lessons(mock_db, U, a["id"])] == [
        "Client g1 hates broad audiences"]
    mb = await sub.list_parent_lessons(mock_db, U, "media_buyer")
    assert {(l["sub_agent_name"], l["text"]) for l in mb} == {
        ("Fatigue watcher", "Client g1 hates broad audiences"), ("Other", "Check frequency first")}
    assert [l["text"] for l in await sub.list_parent_lessons(mock_db, U, "sales_coach")] == ["Ask about budget early"]
    assert await sub.list_parent_lessons(mock_db, OTHER, "media_buyer") == []

    lesson = (await sub.list_sub_agent_lessons(mock_db, U, b["id"]))[0]
    await sub.delete_sub_agent_lesson(mock_db, U, b["id"], lesson["id"])
    assert await sub.list_sub_agent_lessons(mock_db, U, b["id"]) == []


# ---------------------------------------------------------------------------
# The proposal tool (for the delegation branch)
# ---------------------------------------------------------------------------

async def test_propose_tool(mock_db):
    schema = propose_tool.tool_schema("sales_coach")
    enum = schema["function"]["parameters"]["properties"]["tools"]["items"]["enum"]
    assert "get_call_center_stats" in enum and "get_ad_insights" not in enum
    ok = await propose_tool.execute(mock_db, U, "media_buyer", name="Spend pacer", purpose="Watches pacing",
                                    instructions="Compare spend to budget.", tools=["get_daily_metrics"],
                                    clients="all", session_id="s1", parent="sales_coach")  # parent from args is ignored
    assert ok["ok"] and ok["sub_agent"]["status"] == "proposed" and ok["sub_agent"]["parent"] == "media_buyer"
    assert ok["sub_agent"]["tools"] == [{"name": "get_daily_metrics", "label": "Daily numbers"}]
    assert (await mock_db[sub.SUB_AGENTS].find_one({}))["source"] == {"session_id": "s1"}
    bad = await propose_tool.execute(mock_db, U, "media_buyer", name="X", purpose="p", instructions="i",
                                     tools=["analyze_call_recordings"])
    assert bad == {"ok": False, "error": bad["error"], "code": "tool_not_allowed"}


# ---------------------------------------------------------------------------
# Scheduled runs (plumbing; not wired into the tick)
# ---------------------------------------------------------------------------

def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_is_due():
    s = {"status": "active", "kind": "specialist",
         "schedule": {"time": "08:00", "timezone": "Europe/London", "days": ["fri"]}}
    assert not sub_schedule.is_due(s, _utc(2026, 11, 20, 7, 59))      # Friday, before 08:00 London (UTC+0)
    assert sub_schedule.is_due(s, _utc(2026, 11, 20, 8, 0))
    assert not sub_schedule.is_due(s, _utc(2026, 11, 20, 9, 0), done_today=True)
    assert not sub_schedule.is_due(s, _utc(2026, 11, 21, 9, 0))       # Saturday is not one of its days
    assert not sub_schedule.is_due({**s, "status": "paused"}, _utc(2026, 11, 20, 9, 0))
    assert not sub_schedule.is_due({**s, "schedule": None}, _utc(2026, 11, 20, 9, 0))


async def test_run_due_sub_agents_runs_once_a_day(mock_mongo_client, mock_db):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    s = await propose(mock_db, schedule={"time": "08:00", "timezone": "UTC"})
    paused = await propose(mock_db, name="Paused", schedule={"time": "08:00", "timezone": "UTC"})
    await propose(mock_db, name="Unapproved", schedule={"time": "08:00", "timezone": "UTC"})
    await sub.approve_sub_agent(mock_db, U, s["id"])
    await sub.approve_sub_agent(mock_db, U, paused["id"])
    await sub.pause_sub_agent(mock_db, U, paused["id"])

    calls = []

    async def run_one(db, mongo_client, sub_agent, *, local_date, deadline):
        calls.append((sub_agent["id"], local_date, sub_agent["instructions"]))
        return {"status": "ok", "summary": "done"}

    now = _utc(2026, 11, 20, 8, 15)
    out = await sub_schedule.run_due_sub_agents(mock_mongo_client, now_utc=now, run_one=run_one)
    assert out["ran"] == 1 and calls == [(s["id"], "2026-11-20", s["instructions"])]
    again = await sub_schedule.run_due_sub_agents(mock_mongo_client, now_utc=now + timedelta(minutes=15), run_one=run_one)
    assert again["ran"] == 0 and len(calls) == 1
    stored = await sub.get_sub_agent(mock_db, U, s["id"])
    assert stored["last_run_at"] is not None and stored["last_run"]["status"] == "ok"
    await sub_schedule.run_due_sub_agents(mock_mongo_client, now_utc=_utc(2026, 11, 21, 8, 1), run_one=run_one)
    assert [c[1] for c in calls] == ["2026-11-20", "2026-11-21"]


async def test_claim_retries_a_stale_run_then_gives_up(mock_mongo_client, mock_db):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    s = await propose(mock_db, schedule={"time": "08:00", "timezone": "UTC"})
    doc = await mock_db[sub.SUB_AGENTS].find_one({"_id": s["id"]})
    t = _utc(2026, 11, 20, 8, 0)
    assert await sub_schedule.claim_run(mock_db, doc, "2026-11-20", t) is not None
    assert await sub_schedule.claim_run(mock_db, doc, "2026-11-20", t + timedelta(minutes=5)) is None  # held
    t2 = t + timedelta(minutes=31)
    assert (await sub_schedule.claim_run(mock_db, doc, "2026-11-20", t2))["attempts"] == 2
    t3 = t2 + timedelta(minutes=31)
    assert (await sub_schedule.claim_run(mock_db, doc, "2026-11-20", t3))["attempts"] == 3
    assert await sub_schedule.claim_run(mock_db, doc, "2026-11-20", t3 + timedelta(minutes=31)) is None
    row = await mock_db[sub_schedule.RUNS].find_one({"sub_agent_id": s["id"]})
    assert row["status"] == "error" and row["result"]["reason"] == "timed_out"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def api(mock_mongo_client, monkeypatch):
    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    monkeypatch.setattr(sub_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(agents_router, "get_mongo_client", fake_client)
    state = {"user": U}
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    client = TestClient(app)
    client.state = state
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_routes(api, mock_db):
    await groups(mock_db, "g1")
    s = await propose(mock_db, clients=["g1"], schedule={"time": "08:00", "timezone": "UTC"})

    r = api.get("/api/agents/sub", params={"parent": "media_buyer"})
    assert r.status_code == 200, r.text
    rec = r.json()[0]
    assert set(rec) >= {"id", "parent", "kind", "name", "purpose", "instructions", "tools", "clients",
                        "schedule", "status", "created_at", "last_run_at"}
    assert rec["clients"] == [{"id": "g1", "name": "Client g1"}]
    assert rec["tools"][0] == {"name": "get_ad_insights", "label": "Ad results"}
    assert rec["schedule"] == {"time": "08:00", "timezone": "UTC", "days": list(sub.DAYS)}
    assert api.get("/api/agents/sub", params={"parent": "sales_coach"}).json() == []
    assert api.get("/api/agents/sub", params={"parent": "birdy"}).status_code == 422
    assert api.get("/api/agents/sub", params={"status": "active"}).json() == []
    assert api.get(f"/api/agents/sub/{s['id']}").json()["status"] == "proposed"

    assert api.post(f"/api/agents/sub/{s['id']}/pause").status_code == 409
    assert api.post(f"/api/agents/sub/{s['id']}/approve").json()["status"] == "active"
    assert api.post(f"/api/agents/sub/{s['id']}/pause").json()["status"] == "paused"
    assert api.post(f"/api/agents/sub/{s['id']}/resume").json()["status"] == "active"

    r = api.patch(f"/api/agents/sub/{s['id']}", json={"purpose": "New purpose", "schedule": None, "clients": "all"})
    assert r.status_code == 200, r.text
    assert r.json()["purpose"] == "New purpose" and r.json()["schedule"] is None and r.json()["clients"] == "all"
    r = api.patch(f"/api/agents/sub/{s['id']}", json={"tools": ["analyze_call_recordings"]})
    assert r.status_code == 400 and r.json()["code"] == "tool_not_allowed"
    assert api.patch(f"/api/agents/sub/{s['id']}", json={"status": "proposed"}).status_code == 422

    await sub.add_sub_agent_lesson(mock_db, U, s["id"], "A lesson")
    lessons = api.get(f"/api/agents/sub/{s['id']}/lessons").json()
    assert [l["text"] for l in lessons] == ["A lesson"]

    assert api.delete(f"/api/agents/sub/{s['id']}").json() == {"deleted": s["id"]}
    assert api.get(f"/api/agents/sub/{s['id']}").status_code == 404


async def test_reject_route(api, mock_db):
    s = await propose(mock_db)
    assert api.post(f"/api/agents/sub/{s['id']}/reject").json() == {"deleted": s["id"]}
    assert api.post(f"/api/agents/sub/{s['id']}/reject").status_code == 404


async def test_routes_other_user_404(api, mock_db):
    s = await propose(mock_db)
    api.state["user"] = OTHER
    assert api.get("/api/agents/sub").json() == []
    for method, path in [("get", ""), ("post", "/approve"), ("post", "/reject"), ("post", "/pause"),
                         ("post", "/resume"), ("delete", ""), ("get", "/lessons")]:
        r = getattr(api, method)(f"/api/agents/sub/{s['id']}{path}")
        assert r.status_code == 404, (method, path, r.text)
    assert api.patch(f"/api/agents/sub/{s['id']}", json={"name": "Mine"}).status_code == 404
    api.state["user"] = U
    assert api.get(f"/api/agents/sub/{s['id']}").json()["name"] == "Fatigue watcher"


async def test_agents_routes_unaffected(api, mock_db):
    """GET /api/agents still lists only agents, and an agent id still resolves under /api/agents."""
    await propose(mock_db)
    r = api.post("/api/agents", json={"type": "custom", "name": "Rules"})
    assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert [a["id"] for a in api.get("/api/agents").json()] == [aid]
    assert api.get(f"/api/agents/{aid}").json()["name"] == "Rules"
    listed = api.get("/api/agents/sub", params={"parent": "media_buyer"}).json()
    assert {x["kind"] for x in listed} == {"custom", "specialist"}
    assert api.post(f"/api/agents/sub/{aid}/pause").json()["code"] == "managed_elsewhere"
    assert api.delete(f"/api/agents/{aid}").json() == {"deleted": aid}  # its own delete still works


# ---------------------------------------------------------------------------
# The editor's tool list, and proposals that lapse
# ---------------------------------------------------------------------------

def test_tools_route_lists_what_validation_allows(api):
    for parent in ("media_buyer", "sales_coach"):
        r = api.get("/api/agents/sub/tools", params={"parent": parent})
        assert r.status_code == 200, r.text
        tools = r.json()["tools"]
        assert {t["name"] for t in tools} == parent_tool_names(parent)
        assert all(set(t) == {"name", "label"} and t["label"] for t in tools)
    assert {"name": "get_ad_insights", "label": "Ad results"} in api.get(
        "/api/agents/sub/tools", params={"parent": "media_buyer"}).json()["tools"]
    for params in ({}, {"parent": "birdy"}):
        r = api.get("/api/agents/sub/tools", params=params)
        assert r.status_code == 400 and r.json()["code"] == "bad_parent"


async def _age(db, sub_id, days):
    await db[sub.SUB_AGENTS].update_one({"_id": sub_id},
                                        {"$set": {"created_at": datetime.utcnow() - timedelta(days=days)}})


async def test_a_lapsed_proposal_frees_its_name_and_is_hidden(mock_mongo_client, mock_db):
    await sub.create_sub_agent_indexes(mock_mongo_client)
    old = await propose(mock_db)
    fresh = await propose(mock_db, name="Fresh one")
    await _age(mock_db, old["id"], 31)
    await _age(mock_db, fresh["id"], 29)
    assert [s["id"] for s in await sub.list_sub_agents(mock_db, U, "media_buyer")] == [fresh["id"]]
    with pytest.raises(sub.SubAgentError) as e:
        await sub.approve_sub_agent(mock_db, U, old["id"])
    assert e.value.code == "expired"
    with pytest.raises(sub.SubAgentError) as e:      # a fresh proposal still holds its name
        await propose(mock_db, name="Fresh one")
    assert e.value.code == "duplicate_name"
    again = await propose(mock_db)                     # the lapsed one no longer does
    assert again["id"] != old["id"] and again["status"] == "proposed"
    assert await mock_db[sub.SUB_AGENTS].find_one({"_id": old["id"]}) is None
    # an approved sub-agent never lapses
    await sub.approve_sub_agent(mock_db, U, again["id"])
    await _age(mock_db, again["id"], 90)
    assert again["id"] in [s["id"] for s in await sub.list_sub_agents(mock_db, U, "media_buyer")]
