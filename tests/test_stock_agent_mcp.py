from datetime import datetime

import pytest
from fastmcp.exceptions import ToolError

from ai.agents import store as agent_store
from ai.agents.stock.scoreboard import OUTCOMES
from ai.mcp import stock_agent_mcp as t

ALICE, BOB = "alice@example.com", "bob@example.com"
RUNS, CARDS, MEMORY = agent_store.RUNS, agent_store.CARDS, agent_store.MEMORY

pytestmark = pytest.mark.asyncio


async def _stock(db, user=ALICE):
    return await agent_store.create_agent(db, user, type=agent_store.TYPE_STOCK)


async def _run(db, agent, rid, **kw):
    await db[RUNS].insert_one({"_id": rid, "agent_id": agent["_id"], "user_id": agent["user_id"], "kind": "batch",
                               "phase": "done", "as_of": datetime(2026, 11, 3, 7), "summary": {"cards": 1},
                               "clients": {"g1": {"state": "done", "draft": {"x": 1}}}, "alerts": [], **kw})


async def _card(db, agent, cid, rid, **kw):
    await db[CARDS].insert_one({
        "_id": cid, "agent_id": agent["_id"], "user_id": agent["user_id"], "run_id": rid, "client_group_id": "g1",
        "client_name": "Acme", "tag": "changes", "status": "open",
        "lines": [{"id": "l1", "kind": "remove", "ad_name": "A", "mode": "ask", "state": "proposed"}],
        "thought_process": "t", "next_steps": "n", "follow_ups": [], "created_at": datetime(2026, 11, 3, 8), **kw})


@pytest.fixture
async def seeded(mock_db, set_current_user):
    set_current_user(ALICE)
    agent = await _stock(mock_db)
    await _run(mock_db, agent, "r1")
    await _card(mock_db, agent, "c1", "r1")
    await _card(mock_db, agent, "c2", "r1", status="dismissed", client_name="Zed")
    return agent


async def test_overview(mock_db, seeded):
    out = await t.get_agent_overview.fn()
    assert out["agent"]["id"] == seeded["_id"] and out["latest_run"]["id"] == "r1"
    assert "clients" not in out["latest_run"] and out["open_cards"] == 1


async def test_runs(mock_db, seeded):
    await _run(mock_db, seeded, "rp", kind="preview")
    out = await t.get_agent_runs.fn(limit=5)
    assert [r["id"] for r in out["runs"]] == ["r1"] and out["more"] is False


async def test_run(mock_db, seeded):
    out = await t.get_agent_run.fn(run_id="r1")
    assert out["run"]["id"] == "r1" and {c["id"] for c in out["cards"]} == {"c1", "c2"}
    assert out["clients"]["g1"]["state"] == "done" and out["scoreboard"]["moves"] == 0
    with pytest.raises(ToolError, match="Run not found"):
        await t.get_agent_run.fn(run_id="nope")


async def test_month(mock_db, seeded):
    out = await t.get_agent_month.fn(month="2026-11")
    assert out
    with pytest.raises(ToolError, match="2026-10"):
        await t.get_agent_month.fn(month="nonsense")
    with pytest.raises(ToolError, match="hasn't happened"):
        await t.get_agent_month.fn(month="2999-01")


async def test_cards(mock_db, seeded):
    assert [c["id"] for c in (await t.get_agent_cards.fn())["cards"]] == ["c1"]
    assert [c["id"] for c in (await t.get_agent_cards.fn(status="dismissed"))["cards"]] == ["c2"]
    assert (await t.get_agent_cards.fn(status="all"))["count"] == 2


async def test_card(mock_db, seeded):
    out = await t.get_agent_card.fn(card_id="c1")
    assert out["id"] == "c1" and out["lines"][0]["id"] == "l1" and out["thought_process"] == "t"
    with pytest.raises(ToolError, match="Card not found"):
        await t.get_agent_card.fn(card_id="zzz")


async def test_lessons(mock_db, seeded):
    await mock_db[MEMORY].insert_one({"_id": "m1", "agent_id": seeded["_id"], "text": "no blue", "active": True,
                                      "created_at": datetime(2026, 11, 1)})
    assert [m["id"] for m in await t.get_agent_lessons.fn()] == ["m1"]


async def test_outcomes(mock_db, seeded):
    await mock_db[OUTCOMES].insert_many([
        {"_id": "o1", "agent_id": seeded["_id"], "result": "won", "created_at": datetime(2026, 11, 1)},
        {"_id": "o2", "agent_id": seeded["_id"], "result": "lost", "created_at": datetime(2026, 11, 2)}])
    out = await t.get_agent_outcomes.fn()
    assert (out["won"], out["lost"], out["moves"]) == (1, 1, 2) and [r["id"] for r in out["rows"]] == ["o2", "o1"]


async def test_slack_template(mock_db, seeded):
    out = await t.get_agent_slack_template.fn(kind="card")
    assert out["kind"] == "card" and out["customised"] is False and out["template"]
    with pytest.raises(ToolError, match="No template called"):
        await t.get_agent_slack_template.fn(kind="bogus")


async def test_stock_tools_reject_custom_agent_with_hint(mock_db, set_current_user):
    set_current_user(ALICE)
    custom = await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_CUSTOM, name="Side")
    with pytest.raises(ToolError, match="custom agent"):
        await t.get_agent_overview.fn(agent=custom["_id"])
    with pytest.raises(ToolError, match="custom agent"):
        await t.get_agent_cards.fn(agent="Side")


async def test_cards_of_other_user_invisible(mock_db, set_current_user):
    alice = await _stock(mock_db, ALICE)
    await _run(mock_db, alice, "r1")
    await _card(mock_db, alice, "c1", "r1")
    set_current_user(BOB)
    bob = await _stock(mock_db, BOB)
    assert (await t.get_agent_cards.fn(status="all"))["cards"] == []
    with pytest.raises(ToolError, match="Card not found"):
        await t.get_agent_card.fn(card_id="c1")
    with pytest.raises(ToolError):
        await t.get_agent_card.fn(card_id="c1", agent=alice["_id"])
    with pytest.raises(ToolError, match="Run not found"):
        await t.get_agent_run.fn(run_id="r1")
    assert (await t.get_agent_overview.fn())["agent"]["id"] == bob["_id"]
