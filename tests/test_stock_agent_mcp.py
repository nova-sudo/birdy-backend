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


async def test_other_agent_of_same_user_card_not_returned(mock_db, set_current_user):
    set_current_user(ALICE)
    stock = await _stock(mock_db)
    custom = await agent_store.create_agent(mock_db, ALICE, type=agent_store.TYPE_CUSTOM, name="Side")
    await _card(mock_db, custom, "cx", "rx")
    assert (await t.get_agent_cards.fn(status="all"))["cards"] == []
    with pytest.raises(ToolError, match="Card not found"):
        await t.get_agent_card.fn(card_id="cx")
    assert stock["_id"]


async def test_lists_say_when_truncated(mock_db, set_current_user, monkeypatch):
    set_current_user(ALICE)
    agent = await _stock(mock_db)
    monkeypatch.setattr(t, "CARD_LIST_LIMIT", 2)
    monkeypatch.setattr(t, "SCOREBOARD_ROWS", 2)
    for i in range(3):
        await _card(mock_db, agent, f"c{i}", "r1", created_at=datetime(2026, 11, 3, 8, i))
        await mock_db[OUTCOMES].insert_one({"_id": f"o{i}", "agent_id": agent["_id"], "result": "won",
                                            "created_at": datetime(2026, 11, 1, 0, i)})
    out = await t.get_agent_cards.fn()
    assert out["count"] == 2 and out["more"] is True
    board = await t.get_agent_outcomes.fn()
    assert len(board["rows"]) == 2 and board["more_rows"] is True and board["won"] == 3


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock  # noqa: E402


def _two_lines():
    return [{"id": "l1", "kind": "remove", "ad_name": "A", "mode": "ask", "state": "proposed"},
            {"id": "l2", "kind": "remove", "ad_name": "B", "mode": "ask", "state": "proposed"}]


@pytest.fixture
async def writable(mock_db, set_current_user, monkeypatch):
    set_current_user(ALICE)
    agent = await _stock(mock_db)
    await _run(mock_db, agent, "r1")
    await _card(mock_db, agent, "c1", "r1", lines=_two_lines(), spend=120.0, current_cpl=15.0, currency="GBP")
    applied = AsyncMock(return_value={"ok": True, "outcome": "applied", "reason": None, "card": {"client_name": "Acme"}, "lines": []})
    monkeypatch.setattr(t.stock_apply, "apply_card", applied)
    return agent, applied


async def test_apply_card_first_call_changes_nothing(writable):
    _, applied = writable
    out = await t.apply_card.fn(card_id="c1", line_ids=["l1"])
    applied.assert_not_awaited()
    assert out["needs_confirmation"] is True and out["confirm_code"]
    assert [ln["id"] for ln in out["preview"]["card"]["lines"]] == ["l1"]
    assert out["preview"]["client"] == {"name": "Acme", "spend": 120.0, "cpl": 15.0, "currency": "GBP"}


async def test_apply_card_with_code_applies_with_source_mcp(writable):
    agent, applied = writable
    first = await t.apply_card.fn(card_id="c1", line_ids=["l2", "l1"])
    out = await t.apply_card.fn(card_id="c1", line_ids=["l1", "l2"], confirm_code=first["confirm_code"])
    applied.assert_awaited_once()
    args, kw = applied.await_args
    assert args[2:4] == (ALICE, "c1") and kw["source"] == "mcp" and kw["by"] == ALICE
    assert kw["line_ids"] == ["l1", "l2"] and kw["budget_s"] == t.APPLY_BUDGET_SECONDS == 20
    assert out["outcome"] == "applied" and out["ok"] is True and out["summary"]
    again = await t.apply_card.fn(card_id="c1", line_ids=["l1", "l2"], confirm_code=first["confirm_code"])
    assert "error" in again and applied.await_count == 1


async def test_apply_card_code_for_other_line_ids_refused(writable):
    _, applied = writable
    first = await t.apply_card.fn(card_id="c1", line_ids=["l1"])
    out = await t.apply_card.fn(card_id="c1", line_ids=["l2"], confirm_code=first["confirm_code"])
    assert "error" in out and "Nothing was changed" in out["error"]
    applied.assert_not_awaited()


async def test_apply_card_without_line_ids_previews_every_proposed_line(writable):
    out = await t.apply_card.fn(card_id="c1")
    assert [ln["id"] for ln in out["preview"]["card"]["lines"]] == ["l1", "l2"]


async def test_apply_card_unknown_line_refused(writable):
    with pytest.raises(ToolError, match="aren't lines"):
        await t.apply_card.fn(card_id="c1", line_ids=["nope"])


async def test_apply_all_previews_open_cards_then_applies(mock_db, writable, monkeypatch):
    agent, _ = writable
    await _card(mock_db, agent, "c9", "r1", status="applied")
    run = AsyncMock(return_value={"ok": True, "outcome": "queued", "reason": None})
    monkeypatch.setattr(t.stock_apply, "apply_run", run)
    first = await t.apply_all_cards.fn(run_id="r1")
    assert first["preview"]["count"] == 1 and first["preview"]["cards"][0]["card"]["id"] == "c1"
    run.assert_not_awaited()
    out = await t.apply_all_cards.fn(run_id="r1", confirm_code=first["confirm_code"])
    assert run.await_args.kwargs["source"] == "mcp" and out["outcome"] == "queued" and "queued" in out["summary"]
    run.return_value = {"ok": True, "outcome": "applied", "reason": None, "applied": 2}
    again = await t.apply_all_cards.fn(run_id="r1")
    done = await t.apply_all_cards.fn(run_id="r1", confirm_code=again["confirm_code"])
    assert "Applied 2" in done["summary"] and "queued" not in done["summary"]


async def test_undo_card_line_two_step(writable, monkeypatch):
    undo = AsyncMock(return_value={"ok": True, "outcome": "undone", "reason": None, "card": {}, "lines": []})
    monkeypatch.setattr(t.stock_apply, "undo_line", undo)
    first = await t.undo_card_line.fn(card_id="c1", line_id="l1")
    undo.assert_not_awaited()
    out = await t.undo_card_line.fn(card_id="c1", line_id="l1", confirm_code=first["confirm_code"])
    assert undo.await_args.kwargs["source"] == "mcp" and out["outcome"] == "undone"
    other = await t.undo_card_line.fn(card_id="c1", line_id="l1")
    assert (await t.undo_card_line.fn(card_id="c1", line_id="l1", confirm_code=other["confirm_code"]))["outcome"] == "undone"
    bad = await t.undo_card_line.fn(card_id="c1", line_id="l1")
    with pytest.raises(ToolError):
        await t.undo_card_line.fn(card_id="c1", line_id="zzz")
    assert bad["confirm_code"]


async def test_post_weekly_report_previews_text_then_posts(mock_db, writable, monkeypatch):
    agent, _ = writable
    await mock_db[RUNS].update_one({"_id": "r1"}, {"$set": {"weekly": {"values": {"week": "3 Nov to 9 Nov", "wins": "Acme"}}}})
    post = AsyncMock(return_value={"outcome": "posted", "ts": "1.2"})
    monkeypatch.setattr(t.reports, "post_weekly_report", post)
    first = await t.post_weekly_report.fn(run_id="r1")
    post.assert_not_awaited()
    assert "3 Nov to 9 Nov" in first["preview"]["text"] and "Acme" in first["preview"]["text"]
    out = await t.post_weekly_report.fn(run_id="r1", confirm_code=first["confirm_code"])
    assert out["outcome"] == "posted" and "Posted" in out["summary"] and post.await_args.kwargs["by"] == ALICE


async def test_post_weekly_report_without_weekly_refused(writable):
    with pytest.raises(ToolError, match="no weekly report"):
        await t.post_weekly_report.fn(run_id="r1")


async def test_read_scope_cannot_dismiss(mcp_call, writable):
    res = await mcp_call("dismiss_card", {"card_id": "c1"}, scope="birdy.read", token_type="mcp")
    assert res.is_error and "view-only" in res.text
    for tool, args in [("apply_card", {"card_id": "c1"}), ("post_weekly_report", {"run_id": "r1"}),
                       ("update_agent_settings", {})]:
        denied = await mcp_call(tool, args, scope="birdy.read", token_type="mcp")
        assert denied.is_error and "view-only" in denied.text


async def test_write_scope_live_tool_returns_preview_over_the_wire(mcp_call, writable):
    res = await mcp_call("apply_card", {"card_id": "c1", "line_ids": ["l1"]}, scope="birdy.write", token_type="mcp")
    assert not res.is_error and "confirm_code" in res.text


async def test_dismiss_card_records_source_mcp(mock_db, writable):
    out = await t.dismiss_card.fn(card_id="c1")
    assert out["outcome"] == "dismissed" and out["summary"]
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert card["status"] == "dismissed" and card["decided_by"]["source"] == "mcp"


async def test_edit_card_keeps_only_given_lines(mock_db, writable):
    out = await t.edit_card.fn(card_id="c1", keep_line_ids=["l1"])
    assert "1 line" in out["summary"]
    assert [ln["id"] for ln in out["card"]["lines"]] == ["l1"]
    stored = await mock_db[CARDS].find_one({"_id": "c1"})
    assert [ln["id"] for ln in stored["lines"]] == ["l1"]


async def test_add_agent_lesson_client_needs_a_card_and_all_does_not(mock_db, writable, monkeypatch):
    with pytest.raises(ToolError, match="card_id"):
        await t.add_agent_lesson.fn(text="never pause brand")
    lesson = AsyncMock(return_value={"_id": "m1", "text": "never pause brand"})
    monkeypatch.setattr(t.stock_lessons, "lesson_from_text", lesson)
    out = await t.add_agent_lesson.fn(text="never pause brand", scope="all")
    assert out["lesson"]["id"] == "m1" and lesson.await_args.kwargs["source"] == "mcp"
    out = await t.add_agent_lesson.fn(text="x", card_id="c1")
    assert lesson.await_args.kwargs["client_group_id"] == "g1" and out["summary"]
    assert lesson.await_args.kwargs["source"] == "mcp" and lesson.await_args.kwargs["source_ref"] == "c1"


async def test_update_agent_settings_merges_and_refuses_bad(mock_db, writable):
    agent, _ = writable
    out = await t.update_agent_settings.fn(settings={"weekly": {"auto_post": True}})
    assert out["agent"]["settings"]["weekly"]["auto_post"] is True
    stored = await mock_db[agent_store.AGENTS].find_one({"_id": agent["_id"]})
    assert stored["settings"]["weekly"]["auto_post"] is True
    with pytest.raises(ToolError, match="invalid"):
        await t.update_agent_settings.fn(settings={"zones": {"currency": 5}})
    with pytest.raises(ToolError, match="Unknown client group"):
        await t.update_agent_settings.fn(client_group_ids=["not-mine"])


async def test_slack_template_set_and_reset(writable):
    default = (await t.get_agent_slack_template.fn(kind="weekly"))["defaults"]
    out = await t.set_agent_slack_template.fn(kind="weekly", template=default)
    assert out["customised"] is True and out["summary"]
    back = await t.reset_agent_slack_template.fn(kind="weekly")
    assert back["customised"] is False
    with pytest.raises(ToolError, match="No template"):
        await t.set_agent_slack_template.fn(kind="nope", template=default)
    with pytest.raises(ToolError, match="can't be used"):
        await t.set_agent_slack_template.fn(kind="weekly", template={"blocks": "junk"})


async def test_write_tools_cannot_touch_another_users_card(mock_db, set_current_user, monkeypatch):
    alice = await _stock(mock_db, ALICE)
    await _run(mock_db, alice, "r1")
    await _card(mock_db, alice, "c1", "r1", lines=_two_lines())
    await mock_db[RUNS].update_one({"_id": "r1"}, {"$set": {"weekly": {"values": {"week": "w"}}}})
    applied = AsyncMock()
    monkeypatch.setattr(t.stock_apply, "apply_card", applied)
    monkeypatch.setattr(t.stock_apply, "apply_run", applied)
    set_current_user(BOB)
    await _stock(mock_db, BOB)
    calls = [t.apply_card.fn(card_id="c1"), t.undo_card_line.fn(card_id="c1", line_id="l1"),
             t.dismiss_card.fn(card_id="c1"), t.edit_card.fn(card_id="c1", keep_line_ids=["l1"]),
             t.add_agent_lesson.fn(text="x", card_id="c1")]
    for call in calls:
        with pytest.raises(ToolError, match="Card not found"):
            await call
    for call in (t.apply_all_cards.fn(run_id="r1"), t.post_weekly_report.fn(run_id="r1")):
        with pytest.raises(ToolError, match="not found|no weekly"):
            await call
    with pytest.raises(ToolError, match="No agent matches"):   # naming alice's agent finds nothing for bob
        await t.apply_card.fn(card_id="c1", agent=alice["_id"])
    applied.assert_not_awaited()
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    assert card["status"] == "open" and len(card["lines"]) == 2
    assert await mock_db["mcp_confirm_codes"].count_documents({}) == 0   # no code was issued for bob


async def test_new_tools_are_tagged_write_and_live():
    from ai.mcp import mcp
    tools = await mcp.get_tools()
    for name in ("apply_card", "apply_all_cards", "undo_card_line", "post_weekly_report"):
        assert tools[name].tags == {"write", "live"}
        assert "Show the preview to the user and only call again with confirm_code after they say yes." in tools[name].description
    for name in ("dismiss_card", "edit_card", "add_agent_lesson", "update_agent_settings",
                 "set_agent_slack_template", "reset_agent_slack_template"):
        assert tools[name].tags == {"write"}


async def test_bob_cannot_replay_alices_confirm_code(mock_db, set_current_user, monkeypatch):
    alice = await _stock(mock_db, ALICE)
    await _run(mock_db, alice, "r1")
    await _card(mock_db, alice, "c1", "r1", lines=_two_lines())
    applied = AsyncMock()
    monkeypatch.setattr(t.stock_apply, "apply_card", applied)
    set_current_user(ALICE)
    code = (await t.apply_card.fn(card_id="c1", line_ids=["l1"]))["confirm_code"]
    set_current_user(BOB)
    await _stock(mock_db, BOB)
    with pytest.raises(ToolError, match="Card not found"):
        await t.apply_card.fn(card_id="c1", line_ids=["l1"], confirm_code=code)
    applied.assert_not_awaited()
    # the code is still alice's, unspent
    set_current_user(ALICE)
    applied.return_value = {"ok": True, "outcome": "applied", "reason": None, "card": {}, "lines": []}
    assert (await t.apply_card.fn(card_id="c1", line_ids=["l1"], confirm_code=code))["outcome"] == "applied"
