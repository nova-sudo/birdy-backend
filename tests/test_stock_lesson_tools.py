"""Teach Birdy (page "stock_lessons", BirdyHub redesign spec section 4.1): list, add, edit and remove the stock
agent's lessons by chat, only ever on the agent the chat was opened for."""

import contextlib

import pytest
from fastapi.testclient import TestClient

import routers.chat as chat_router
from ai.agents.store import AGENTS, MEMORY, add_memory, create_agent
from ai.tools import stock_lesson_tools as t
from dependencies import get_current_user
from main import app
from test_agent_learning import FakeProvider, use_provider
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture


@pytest.fixture(autouse=True)
def calm_ai(monkeypatch):
    use_provider(monkeypatch, FakeProvider(error=RuntimeError("no network in tests")))

    async def not_blocked(db, user_id):
        return False

    monkeypatch.setattr("credits.is_blocked", not_blocked)


@pytest.fixture(autouse=True)
def _reset_agent():
    token = t.current_lessons_agent.set(None)
    yield
    t.current_lessons_agent.reset(token)


async def _agent(db):
    agent = await _setup(db, "g1", "g2")
    await db["client_groups"].update_one({"id": "g1"}, {"$set": {"name": "Glow Clinic"}})
    await db["client_groups"].update_one({"id": "g2"}, {"$set": {"name": "Glow Spa"}})
    t.current_lessons_agent.set(agent["_id"])
    return agent


async def test_add_lesson_by_client_name(mock_db):
    agent = await _agent(mock_db)
    out = await t.save_lesson(mock_db, USER, text="Never pause video ads", client_name="glow clinic")
    assert out["ok"] is True and out["changed"] == "lesson" and out["lesson"]["client"] == "Glow Clinic"
    every = await t.save_lesson(mock_db, USER, text="Keep three ads live")
    assert every["lesson"]["client"] == "All clients"
    rows = await mock_db[MEMORY].find({"agent_id": agent["_id"]}).sort("created_at", 1).to_list(None)
    assert [(r["client_group_id"] if "client_group_id" in r else None) for r in rows] == ["g1", None]
    assert {r["source"] for r in rows} == {"chat"}
    listed = await t.list_lessons(mock_db, USER)
    assert listed["ok"] and {x["client"] for x in listed["lessons"]} == {"Glow Clinic", "All clients"}
    assert all(x["uses"] == 0 for x in listed["lessons"])


async def test_add_lesson_unknown_client_candidates(mock_db):
    await _agent(mock_db)
    out = await t.save_lesson(mock_db, USER, text="x", client_name="Acme")
    assert out["ok"] is False and out["candidates"] == ["Glow Clinic", "Glow Spa"]
    vague = await t.save_lesson(mock_db, USER, text="x", client_name="glow")
    assert vague["ok"] is False and vague["candidates"] == ["Glow Clinic", "Glow Spa"]
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_edit_and_remove(mock_db):
    agent = await _agent(mock_db)
    m = await add_memory(mock_db, agent["_id"], text="no blue", kind="fact", source="edit")
    out = await t.edit_lesson(mock_db, USER, lesson_id=m["_id"], text="No blue ads on Fridays")
    assert out["ok"] is True and out["changed"] == "lesson_edited"
    doc = await mock_db[MEMORY].find_one({"_id": m["_id"]})
    assert doc["text"] == "No blue ads on Fridays" and doc["source"] == "chat_edit"
    assert doc["previous_texts"][0]["text"] == "no blue"
    assert (await t.remove_lesson(mock_db, USER, lesson_id=m["_id"])) == {"ok": True, "changed": "lesson_removed"}
    assert (await mock_db[MEMORY].find_one({"_id": m["_id"]}))["active"] is False
    assert (await t.list_lessons(mock_db, USER))["lessons"] == []


async def test_foreign_lesson_not_found(mock_db):
    await _agent(mock_db)
    other = await create_agent(mock_db, "u2", type="custom", name="Theirs")
    m = await add_memory(mock_db, other["_id"], text="theirs", kind="fact", source="edit")
    assert (await t.edit_lesson(mock_db, USER, lesson_id=m["_id"], text="mine now"))["error"] == "not_found"
    assert (await t.remove_lesson(mock_db, USER, lesson_id=m["_id"]))["error"] == "not_found"
    assert (await mock_db[MEMORY].find_one({"_id": m["_id"]}))["text"] == "theirs"
    # An agent id that isn't this user's stock agent reaches nothing at all.
    t.current_lessons_agent.set(other["_id"])
    assert await t.list_lessons(mock_db, USER) == {"ok": False, "error": "not_found"}
    assert (await t.save_lesson(mock_db, USER, text="x"))["error"] == "not_found"


async def test_chat_route_404_for_foreign_lessons_agent(mock_db, mock_mongo_client, monkeypatch):
    agent = await _agent(mock_db)
    await mock_db[AGENTS].update_one({"_id": agent["_id"]}, {"$set": {"user_id": "u2"}})

    @contextlib.asynccontextmanager
    async def fake_client():
        yield mock_mongo_client

    async def run_chat(**kw):
        raise AssertionError("must not run the chat")

    monkeypatch.setattr(chat_router, "get_mongo_client", fake_client)
    monkeypatch.setattr(chat_router, "run_chat", run_chat)
    app.dependency_overrides[get_current_user] = lambda: USER
    try:
        r = TestClient(app).post("/api/chat", json={"message": "hi", "page": "stock_lessons", "agent_id": agent["_id"]})
        assert r.status_code == 404 and r.json()["detail"] == "agent_not_found"
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_lesson_tools_registered_for_the_page():
    from ai.orchestrator import _ASYNC_PAGE_PROMPTS, _PAGE_ONLY_TOOLS, _PAGE_TOOLS
    assert _PAGE_TOOLS["stock_lessons"] == list(t.STOCK_LESSON_TOOL_NAMES)
    assert set(t.STOCK_LESSON_TOOL_NAMES) <= _PAGE_ONLY_TOOLS
    assert "stock_lessons" in _ASYNC_PAGE_PROMPTS


async def test_prompt_restates_and_confirms(mock_db):
    await _agent(mock_db)
    p = await t.build_system_prompt(mock_db, USER)
    for needle in ("one short sentence", "confirm", "list_lessons", "Glow Clinic"):
        assert needle in p, needle
