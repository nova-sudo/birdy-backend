"""Undo for every kind of change a proposal can apply (ai/team/proposals.py): lessons saved, edited and
removed; creative-test drafts and targets; a new sub-agent. Each is reversed exactly, and refused when the
thing was changed since or has no exact reverse. mongomock, scripted providers, no model or API calls."""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from ai.agents.store import MEMORY
from ai.team import memory as team_memory
from ai.team import proposals as tp
from ai.tools import creative_tools, stock_lesson_tools
from ai.tools.stock_lesson_tools import current_lessons_agent, register_stock_lesson_tools
from services.creatives import store as ct_store
from tests.test_agent_team import (G1, TeamProvider, U, _use, api, call, isolate,  # noqa: F401
                                   registry, say)

AGENT = "agent-1"


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _approved(db, changes, pid="p1"):
    await db[tp.LOG].insert_one({"_id": "row-" + pid, "user_id": U, "session_id": "s-" + pid, "role": "assistant",
                                 "created_at": datetime(2030, 1, 1),
                                 "proposals": [{"id": pid, "agent": "sales_coach", "title": "t", "kind": "save_lesson",
                                                "status": tp.APPROVED,
                                                "applied": {"type": tp.CHANGES, "changes": changes}}]})


async def _lesson(db, lid="l1", text="Call within 5 minutes.", active=True, status="active"):
    await db[MEMORY].insert_one({"_id": lid, "agent_id": AGENT, "text": text, "active": active, "status": status,
                                 "source": "chat"})


async def _status(db, pid="p1"):
    return (await tp.find(db, U, pid))[2]["status"]


# ── the journal ───────────────────────────────────────────────────────────────

def test_record_does_nothing_outside_an_apply():
    tp.record({"op": tp.LESSON_SAVED})               # no journal open: no error, nothing kept
    journal = []
    token = tp.current_journal.set(journal)
    try:
        tp.record({"op": tp.LESSON_SAVED, "lesson_id": "x"})
    finally:
        tp.current_journal.reset(token)
    assert journal == [{"op": tp.LESSON_SAVED, "lesson_id": "x"}]


async def test_lesson_tools_record_what_undo_needs(mock_db):
    sc = team_memory.sales_coach_id(U)
    journal = []
    jt, at = tp.current_journal.set(journal), current_lessons_agent.set(sc)
    try:
        saved = await stock_lesson_tools.save_lesson(mock_db, U, text="Call back within an hour.")
        lid = saved["lesson"]["id"]
        await stock_lesson_tools.edit_lesson(mock_db, U, lesson_id=lid, text="Call back within 30 minutes.")
        await stock_lesson_tools.remove_lesson(mock_db, U, lesson_id=lid)
    finally:
        tp.current_journal.reset(jt)
        current_lessons_agent.reset(at)
    assert [c["op"] for c in journal] == [tp.LESSON_SAVED, tp.LESSON_EDITED, tp.LESSON_REMOVED]
    assert journal[0] == {"op": tp.LESSON_SAVED, "agent_id": sc, "lesson_id": lid, "text": "Call back within an hour."}
    assert journal[1]["before"]["text"] == "Call back within an hour."
    assert journal[1]["after_text"] == "Call back within 30 minutes."
    assert journal[2]["text"] == "Call back within 30 minutes."
    # all three unwind, newest first, to nothing saved
    await _approved(mock_db, journal)
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 200 and body["status"] == tp.UNDONE and all(c["ok"] for c in body["changes"])
    doc = await mock_db[MEMORY].find_one({"_id": lid})
    assert doc["active"] is False and doc["text"] == "Call back within an hour."


# ── lessons ───────────────────────────────────────────────────────────────────

async def test_undo_a_saved_lesson_removes_it(mock_db):
    await _lesson(mock_db)
    await _approved(mock_db, [{"op": tp.LESSON_SAVED, "agent_id": AGENT, "lesson_id": "l1",
                               "text": "Call within 5 minutes."}])
    assert await tp.can_undo(mock_db, U, (await tp.find(mock_db, U, "p1"))[2])
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 200 and body["status"] == "undone"
    assert (await mock_db[MEMORY].find_one({"_id": "l1"}))["active"] is False
    assert await _status(mock_db) == tp.UNDONE
    assert (await tp.undo(mock_db, None, U, "p1"))[1]["code"] == "cannot_undo"


async def test_a_saved_lesson_edited_since_is_not_undone(mock_db):
    await _lesson(mock_db, text="Call within 10 minutes.")       # the owner reworded it afterwards
    await _approved(mock_db, [{"op": tp.LESSON_SAVED, "agent_id": AGENT, "lesson_id": "l1",
                               "text": "Call within 5 minutes."}])
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and body["code"] == "cannot_undo" and "edited since" in body["detail"]
    assert (await mock_db[MEMORY].find_one({"_id": "l1"}))["active"] is True
    assert await _status(mock_db) == tp.APPROVED


async def test_undo_an_edited_lesson_restores_its_words(mock_db):
    await _lesson(mock_db, text="Call within 2 minutes.")
    change = {"op": tp.LESSON_EDITED, "agent_id": AGENT, "lesson_id": "l1",
              "before": {"text": "Call within 5 minutes.", "source": "slack_mention"}, "after_text": "Call within 2 minutes."}
    await _approved(mock_db, [change])
    status, _ = await tp.undo(mock_db, None, U, "p1")
    doc = await mock_db[MEMORY].find_one({"_id": "l1"})
    assert status == 200 and doc["text"] == "Call within 5 minutes." and doc["source"] == "slack_mention"
    assert doc["previous_texts"][-1]["text"] == "Call within 2 minutes."      # nothing lost


async def test_an_edited_lesson_edited_again_is_not_undone(mock_db):
    await _lesson(mock_db, text="Call within 1 minute.")
    await _approved(mock_db, [{"op": tp.LESSON_EDITED, "agent_id": AGENT, "lesson_id": "l1",
                               "before": {"text": "Call within 5 minutes."}, "after_text": "Call within 2 minutes."}])
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and body["code"] == "cannot_undo" and "edited again" in body["detail"]
    assert (await mock_db[MEMORY].find_one({"_id": "l1"}))["text"] == "Call within 1 minute."


async def test_undo_a_removed_lesson_brings_it_back(mock_db):
    await _lesson(mock_db, active=False)
    await _approved(mock_db, [{"op": tp.LESSON_REMOVED, "agent_id": AGENT, "lesson_id": "l1",
                               "text": "Call within 5 minutes.", "status": "active"}])
    assert (await tp.undo(mock_db, None, U, "p1"))[0] == 200
    assert (await mock_db[MEMORY].find_one({"_id": "l1"}))["active"] is True


async def test_a_removed_lesson_changed_since_is_not_undone(mock_db):
    await _lesson(mock_db, active=False, status="overruled")       # overruled after it was removed
    await _approved(mock_db, [{"op": tp.LESSON_REMOVED, "agent_id": AGENT, "lesson_id": "l1",
                               "text": "Call within 5 minutes.", "status": "active"}])
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and body["code"] == "cannot_undo" and "changed since" in body["detail"]
    assert (await mock_db[MEMORY].find_one({"_id": "l1"}))["active"] is False


async def test_a_removed_lesson_already_back_is_not_undone(mock_db):
    await _lesson(mock_db, active=True)
    await _approved(mock_db, [{"op": tp.LESSON_REMOVED, "agent_id": AGENT, "lesson_id": "l1",
                               "text": "Call within 5 minutes.", "status": "active"}])
    assert (await tp.undo(mock_db, None, U, "p1"))[1]["code"] == "cannot_undo"


# ── creative tests ────────────────────────────────────────────────────────────

async def _test(db, status=ct_store.DRAFT, **fields):
    t = await ct_store.create_test(db, U, name="Spring", origin="new", creative={"destination": "website"},
                                   targets={"metrics": {"cpl": {"value": 20}}})
    return await ct_store.update_test(db, U, t["id"], {"status": status, **fields})


async def test_undo_a_new_draft_archives_it(mock_db):
    t = await _test(mock_db)
    await _approved(mock_db, [{"op": tp.DRAFT_CREATED, "test_id": t["id"], "updated_at": t["updated_at"]}])
    assert (await tp.undo(mock_db, None, U, "p1"))[0] == 200
    assert (await mock_db[ct_store.TESTS].find_one({"id": t["id"]}))["status"] == ct_store.ARCHIVED


async def test_a_draft_posted_since_is_not_undone(mock_db):
    t = await _test(mock_db)
    await _approved(mock_db, [{"op": tp.DRAFT_CREATED, "test_id": t["id"], "updated_at": t["updated_at"]}])
    await ct_store.update_test(mock_db, U, t["id"], {"status": ct_store.POSTING})
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and body["code"] == "cannot_undo" and "posted" in body["detail"]
    assert (await mock_db[ct_store.TESTS].find_one({"id": t["id"]}))["status"] == ct_store.POSTING


async def test_undo_a_draft_edit_restores_the_previous_fields(mock_db):
    t = await _test(mock_db, name="Spring v2", headline_note="new")
    await _approved(mock_db, [{"op": tp.DRAFT_EDITED, "test_id": t["id"], "updated_at": t["updated_at"],
                               "before": {"name": "Spring"}, "unset": ["headline_note"]}])
    assert (await tp.undo(mock_db, None, U, "p1"))[0] == 200
    doc = await mock_db[ct_store.TESTS].find_one({"id": t["id"]})
    assert doc["name"] == "Spring" and "headline_note" not in doc and doc["status"] == ct_store.DRAFT


async def test_draft_edit_changed_again_is_not_undone(mock_db):
    t = await _test(mock_db, name="Spring v2")
    await _approved(mock_db, [{"op": tp.DRAFT_EDITED, "test_id": t["id"], "updated_at": t["updated_at"],
                               "before": {"name": "Spring"}, "unset": []}])
    await asyncio.sleep(0.005)                      # stored times are kept to the millisecond
    await ct_store.update_test(mock_db, U, t["id"], {"name": "Spring v3"})
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and "changed since" in body["detail"]
    assert (await mock_db[ct_store.TESTS].find_one({"id": t["id"]}))["name"] == "Spring v3"


async def test_a_draft_made_then_edited_in_the_same_chat_unwinds(mock_db):
    t = await _test(mock_db)
    created = {"op": tp.DRAFT_CREATED, "test_id": t["id"], "updated_at": t["updated_at"]}
    await asyncio.sleep(0.005)
    t2 = await ct_store.update_test(mock_db, U, t["id"], {"name": "Spring v2"})
    edited = {"op": tp.DRAFT_EDITED, "test_id": t["id"], "updated_at": t2["updated_at"],
              "before_updated_at": t["updated_at"], "before": {"name": "Spring"}, "unset": []}
    await _approved(mock_db, [created, edited])
    assert (await tp.undo(mock_db, None, U, "p1"))[0] == 200
    doc = await mock_db[ct_store.TESTS].find_one({"id": t["id"]})
    assert doc["name"] == "Spring" and doc["status"] == ct_store.ARCHIVED


async def test_targets_tool_records_and_undo_restores(mock_db):
    t = await _test(mock_db, status=ct_store.TESTING)
    journal = []
    token = tp.current_journal.set(journal)
    try:
        await creative_tools.update_creative_targets(mock_db, U, test_id=t["id"],
                                                     targets={"metrics": {"cpl": {"value": 35}}})
    finally:
        tp.current_journal.reset(token)
    assert journal[0]["op"] == tp.TARGETS_CHANGED and journal[0]["before"]["targets"]["metrics"]["cpl"]["value"] == 20
    await _approved(mock_db, journal)
    assert (await tp.undo(mock_db, None, U, "p1"))[0] == 200
    doc = await mock_db[ct_store.TESTS].find_one({"id": t["id"]})
    assert doc["targets"]["metrics"]["cpl"]["value"] == 20 and doc["status"] == ct_store.TESTING


async def test_targets_changed_since_are_not_undone(mock_db):
    t = await _test(mock_db, status=ct_store.TESTING)
    await _approved(mock_db, [{"op": tp.TARGETS_CHANGED, "test_id": t["id"], "updated_at": t["updated_at"],
                               "before": {"targets": {}}, "unset": []}])
    await asyncio.sleep(0.005)
    await ct_store.update_test(mock_db, U, t["id"], {"verdict": "winner"})      # scored since
    status, body = await tp.undo(mock_db, None, U, "p1")
    assert status == 409 and body["code"] == "cannot_undo"


# ── changes with no exact reverse ─────────────────────────────────────────────

async def test_spent_or_live_changes_are_refused_and_block_the_rest(mock_db):
    for op in (tp.CALLS_ANALYZED, tp.TEST_POSTED, tp.TEST_SCALED, tp.ADS_PAUSED):
        await mock_db[tp.LOG].delete_many({})
        await _lesson(mock_db, lid="l-" + op)
        await _approved(mock_db, [{"op": tp.LESSON_SAVED, "agent_id": AGENT, "lesson_id": "l-" + op,
                                   "text": "Call within 5 minutes."}, {"op": op}])
        status, body = await tp.undo(mock_db, None, U, "p1")
        assert status == 409 and body["code"] == "cannot_undo" and body["detail"] == tp.NO_REVERSE[op]
        # nothing was half undone
        assert (await mock_db[MEMORY].find_one({"_id": "l-" + op}))["active"] is True


async def test_an_approval_that_changed_nothing_cannot_be_undone(mock_db):
    await _approved(mock_db, [])
    assert (await tp.undo(mock_db, None, U, "p1"))[1]["code"] == "cannot_undo"


# ── a new sub-agent ───────────────────────────────────────────────────────────

async def test_undo_a_new_sub_agent_pauses_it(mock_db):
    from ai.agents.sub_agents import core as sub_core
    sub = await sub_core.propose_sub_agent(mock_db, U, parent="sales_coach", name="Call QA", purpose="Checks calls",
                                           instructions="Check calls.", tools=["get_call_center_stats"], clients="all")
    prop = {"id": "sub_" + sub["id"], "agent": "sales_coach", "kind": tp.SUB_AGENT_KIND, "status": "pending",
            "payload": {"sub_agent": {"id": sub["id"]}}}
    await mock_db[tp.LOG].insert_one({"user_id": U, "session_id": "s", "role": "assistant", "proposals": [prop]})
    # still a proposal: nothing to undo
    assert (await tp.undo(mock_db, None, U, prop["id"]))[1]["code"] == "cannot_undo"
    await sub_core.approve_sub_agent(mock_db, U, sub["id"])
    [shown] = await tp.present(mock_db, U, [prop])
    assert shown["status"] == tp.APPROVED and shown["can_undo"] is True
    status, body = await tp.undo(mock_db, None, U, prop["id"])
    assert status == 200 and body["status"] == tp.UNDONE
    assert (await sub_core.get_sub_agent(mock_db, U, sub["id"]))["status"] == sub_core.PAUSED
    stored = (await tp.find(mock_db, U, prop["id"]))[2]
    [shown] = await tp.present(mock_db, U, [stored])
    assert shown["status"] == tp.UNDONE and shown["can_undo"] is False
    # resumed by the owner later: the proposal stays undone and is not undone twice
    await sub_core.resume_sub_agent(mock_db, U, sub["id"])
    assert (await tp.undo(mock_db, None, U, prop["id"]))[1]["code"] == "cannot_undo"


async def test_a_sub_agent_paused_since_is_not_undone(mock_db):
    from ai.agents.sub_agents import core as sub_core
    sub = await sub_core.propose_sub_agent(mock_db, U, parent="sales_coach", name="Call QA", purpose="Checks calls",
                                           instructions="Check calls.", tools=["get_call_center_stats"], clients="all")
    await sub_core.approve_sub_agent(mock_db, U, sub["id"])
    await sub_core.pause_sub_agent(mock_db, U, sub["id"])
    prop = {"id": "sub_x", "agent": "sales_coach", "kind": tp.SUB_AGENT_KIND, "status": "pending",
            "payload": {"sub_agent": {"id": sub["id"]}}}
    await mock_db[tp.LOG].insert_one({"user_id": U, "session_id": "s", "role": "assistant", "proposals": [prop]})
    status, body = await tp.undo(mock_db, None, U, "sub_x")
    assert status == 409 and body["code"] == "cannot_undo" and "paused" in body["detail"]


# ── end to end: approve, the owner agent changes things, undo over HTTP ──────

def _sc_proposes_lesson():
    return TeamProvider({
        "birdy": [call("ask_sales_coach", question="What should the callers remember?", client="agency"),
                  say("See the card.")],
        "sales_coach": [call("propose_action", title="Remember: call within 5 minutes", detail="Fast calls connect",
                             kind="save_lesson", payload={"text": "Call within 5 minutes."}),
                        say("Proposed it.")],
    })


def test_a_lesson_saved_on_approval_is_undone_over_http(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    register_stock_lesson_tools(registry)
    p = _sc_proposes_lesson()
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    pid = client.post("/api/chat", json={"message": "lessons?", "agent": "birdy"}).json()["proposals"][0]["id"]
    p.scripts["sales_coach"] = [call("save_lesson", text="Call within 5 minutes."), say("Saved.")]
    client.post(f"/api/chat/proposals/{pid}/apply")
    listed = client.get("/api/chat/proposals", params={"status": "approved"}).json()["proposals"]
    assert listed[0]["id"] == pid and listed[0]["can_undo"] is True
    assert listed[0]["applied"]["changes"][0]["op"] == tp.LESSON_SAVED
    r = client.post(f"/api/chat/proposals/{pid}/undo")
    assert r.status_code == 200 and r.json()["status"] == "undone"
    lesson = _sync(mock_db[MEMORY].find_one({"agent_id": team_memory.sales_coach_id(U)}))
    assert lesson["active"] is False
    r = client.post(f"/api/chat/proposals/{pid}/undo")
    assert r.status_code == 409 and r.json()["code"] == "cannot_undo"


def test_the_owners_yes_in_a_later_turn_is_kept_for_undo(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    register_stock_lesson_tools(registry)
    p = _sc_proposes_lesson()
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    pid = client.post("/api/chat", json={"message": "lessons?", "agent": "birdy"}).json()["proposals"][0]["id"]
    p.scripts["sales_coach"] = [say("I'll save 'Call within 5 minutes.' Go ahead?")]
    applied = client.post(f"/api/chat/proposals/{pid}/apply").json()
    assert client.get("/api/chat/proposals", params={"status": "approved"}).json()["proposals"][0]["can_undo"] is False
    p.scripts["sales_coach"] = [call("save_lesson", text="Call within 5 minutes."), say("Saved.")]
    client.post("/api/chat", json={"message": "yes", "agent": "sales_coach", "session_id": applied["session_id"]})
    listed = client.get("/api/chat/proposals", params={"status": "approved"}).json()["proposals"]
    assert listed[0]["can_undo"] is True
    assert client.post(f"/api/chat/proposals/{pid}/undo").status_code == 200
    # another chat with Sales Coach is not part of the proposal
    p.scripts["sales_coach"] = [call("save_lesson", text="Leave a voicemail."), say("Saved.")]
    client.post("/api/chat", json={"message": "remember voicemails", "agent": "sales_coach"})
    row = _sync(tp.find(mock_db, U, pid))[2]
    assert [c["op"] for c in row["applied"]["changes"]] == [tp.LESSON_SAVED]
