"""Proposals, finished (ai/team/proposals.py, routers/chat.py): the pending list across chats and
sub-agent runs, card proposals applied through the card's own guarded path, undo, and statuses on
reopened conversations. mongomock, scripted providers, stub tools."""

import asyncio
from datetime import datetime

from fastapi.testclient import TestClient

from ai.agents.stock import apply as stock_apply
from ai.agents.store import AGENTS, CARDS, TYPE_STOCK
from ai.agents.sub_agents import schedule
from ai.tools.stock_card_tools import register_stock_card_tools
from tests.test_agent_team import (G1, TeamProvider, U, _proposing_script, _use, api, call, isolate,  # noqa: F401
                                   registry, say)

CARD = "card-1"


def _sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mb_proposing(payload):
    return TeamProvider({
        "birdy": [call("ask_media_buyer", question="Anything to pause in Aura?", client="agency"), say("See the card.")],
        "media_buyer": [call("propose_action", title="Pause 'Spring' in Aura", detail="CPL 3x",
                             kind="apply_card_lines", payload=payload),
                        say("Proposed it.")],
    })


async def _stock_card(db):
    await db[AGENTS].insert_one({"_id": "stock1", "user_id": U, "type": TYPE_STOCK, "status": "active"})
    await db[CARDS].insert_one({"_id": CARD, "user_id": U, "agent_id": "stock1", "status": "open",
                                "client_group_id": G1, "client_name": "Aura",
                                "lines": [{"id": "l1", "kind": "remove", "ad_name": "Spring", "state": "proposed"},
                                          {"id": "l2", "kind": "remove", "ad_name": "Summer", "state": "proposed"}]})


def _fake_guarded_apply(monkeypatch, calls):
    async def apply_card(db, mongo_client, user_id, card_id, *, by, source, line_ids=None, budget_s=None):
        calls.append({"card_id": card_id, "line_ids": line_ids, "source": source})
        card = await db[CARDS].find_one({"_id": card_id})
        for ln in card["lines"]:
            if line_ids is None or ln["id"] in line_ids:
                ln.update(state="applied", applied_at=datetime.utcnow())
        await db[CARDS].update_one({"_id": card_id}, {"$set": {"lines": card["lines"]}})
        return {"ok": True, "outcome": "applied", "lines": card["lines"]}
    monkeypatch.setattr(stock_apply, "apply_card", apply_card)


def test_pending_list_covers_chats_and_runs(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    _use(monkeypatch, chat_router, _proposing_script(), registry)
    client = TestClient(app)
    first = client.post("/api/chat", json={"message": "calls?", "agent": "birdy"}).json()
    chat_pid = first["proposals"][0]["id"]
    _sync(mock_db[schedule.RUNS].insert_one({
        "_id": "run1", "user_id": U, "sub_agent_id": "s1", "status": "done", "finished_at": datetime(2030, 1, 1),
        "proposals": [{"id": "p_run", "agent": "media_buyer", "title": "Pause", "kind": "other", "status": "pending",
                       "origin": "sub_agent_run", "sub_agent_id": "s1"}]}))
    items = client.get("/api/chat/proposals").json()["proposals"]
    assert [p["id"] for p in items] == ["p_run", chat_pid]            # newest first
    run_p, chat_p = items
    assert run_p["origin"] == "sub_agent_run" and run_p["sub_agent_id"] == "s1" and run_p["run_id"] == "run1"
    assert chat_p["origin"] == "chat" and chat_p["session_id"] == first["session_id"]
    assert run_p["can_undo"] is False and chat_p["can_undo"] is False
    assert [p["id"] for p in client.get("/api/chat/proposals", params={"agent": "sales_coach"}).json()["proposals"]] \
        == [chat_pid]
    assert client.get("/api/chat/proposals", params={"status": "nope"}).status_code == 422
    # a run's proposal is dismissed like a chat's, and leaves the pending list
    assert client.post("/api/chat/proposals/p_run/dismiss").json()["status"] == "dismissed"
    assert [p["id"] for p in client.get("/api/chat/proposals").json()["proposals"]] == [chat_pid]
    assert [p["id"] for p in client.get("/api/chat/proposals", params={"status": "dismissed"}).json()["proposals"]] \
        == ["p_run"]
    # reopened conversation: the dismissal shows
    client.post(f"/api/chat/proposals/{chat_pid}/dismiss")
    msgs = client.get(f"/api/chat/conversations/{first['session_id']}").json()["messages"]
    assert msgs[-1]["proposals"][0]["status"] == "dismissed" and msgs[-1]["proposals"][0]["can_undo"] is False


def test_approving_a_run_proposal_runs_its_owner(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    p = TeamProvider({"media_buyer": [say("Done through my own tools.")]})
    _use(monkeypatch, chat_router, p, registry)
    _sync(mock_db[schedule.RUNS].insert_one({
        "_id": "run1", "user_id": U, "sub_agent_id": "s1", "status": "done",
        "proposals": [{"id": "p_run", "agent": "media_buyer", "title": "Pause", "kind": "other", "status": "pending",
                       "payload": {}, "proposed_by": "sub:s1"}]}))
    out = TestClient(app).post("/api/chat/proposals/p_run/apply").json()
    assert out["reply"] == "Done through my own tools."
    row = _sync(mock_db[schedule.RUNS].find_one({"_id": "run1"}))
    assert row["proposals"][0]["status"] == "approved" and row["proposals"][0]["applied_session_id"] == out["session_id"]


def test_a_card_proposal_is_applied_through_the_card_and_undone(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    register_stock_card_tools(registry)
    _sync(_stock_card(mock_db))
    applied = []
    _fake_guarded_apply(monkeypatch, applied)
    p = _mb_proposing({"card_id": CARD, "line_ids": ["l1"]})
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    first = client.post("/api/chat", json={"message": "aura?", "agent": "birdy"}).json()
    prop = first["proposals"][0]
    assert prop["kind"] == "apply_card_lines" and prop["agent"] == "media_buyer"
    # the delegated Media Buyer could not apply anything: no card tools, no writes
    assert not {"apply_card_lines", "get_card"} & set(p.offered("media_buyer"))

    p.scripts["media_buyer"] = [call("get_card"), call("apply_card_lines", line_ids=["l1"]), say("Paused Spring.")]
    out = client.post(f"/api/chat/proposals/{prop['id']}/apply").json()
    mb_turn = [c for c in p.calls if c[0] == "media_buyer"][-1]
    assert {"get_card", "apply_card_lines"} <= set(mb_turn[1])
    assert "apply_card_lines" in next(m for m in mb_turn[2] if m["role"] == "user")["content"]
    assert applied == [{"card_id": CARD, "line_ids": ["l1"], "source": "chat"}]     # the card's own path
    assert out["reply"] == "Paused Spring."

    listed = client.get("/api/chat/proposals", params={"status": "approved"}).json()["proposals"]
    assert listed[0]["id"] == prop["id"] and listed[0]["can_undo"] is True
    assert listed[0]["applied"] == {"type": "card_lines", "card_id": CARD, "line_ids": ["l1"]}
    msgs = client.get(f"/api/chat/conversations/{first['session_id']}").json()["messages"]
    assert msgs[-1]["proposals"][0]["status"] == "approved" and msgs[-1]["proposals"][0]["can_undo"] is True

    undone = []

    async def undo_line(db, mongo_client, user_id, card_id, line_id, *, by, source, **kw):
        undone.append((card_id, line_id, source))
        await db[CARDS].update_one({"_id": card_id, "lines.id": line_id}, {"$set": {"lines.$.state": "undone"}})
        return {"ok": True, "outcome": "undone", "reason": None}
    monkeypatch.setattr(stock_apply, "undo_line", undo_line)
    r = client.post(f"/api/chat/proposals/{prop['id']}/undo")
    assert r.status_code == 200 and r.json()["status"] == "undone" and undone == [(CARD, "l1", "chat")]
    msgs = client.get(f"/api/chat/conversations/{first['session_id']}").json()["messages"]
    assert msgs[-1]["proposals"][0]["status"] == "undone" and msgs[-1]["proposals"][0]["can_undo"] is False
    r = client.post(f"/api/chat/proposals/{prop['id']}/undo")
    assert r.status_code == 409 and r.json()["code"] == "cannot_undo"


def test_undo_without_an_undo_path(api, monkeypatch, registry):
    chat_router, app = api
    p = _proposing_script()
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    pid = client.post("/api/chat", json={"message": "calls?", "agent": "birdy"}).json()["proposals"][0]["id"]
    r = client.post(f"/api/chat/proposals/{pid}/undo")              # still pending
    assert r.status_code == 409 and r.json() == {"detail": r.json()["detail"], "code": "cannot_undo"}
    p.scripts["sales_coach"] = [say("Preview first. Go ahead?")]
    client.post(f"/api/chat/proposals/{pid}/apply")
    assert client.post(f"/api/chat/proposals/{pid}/undo").json()["code"] == "cannot_undo"   # no undo path
    assert client.post("/api/chat/proposals/p_nope/undo").status_code == 404


def test_a_card_the_owner_no_longer_has_is_refused_and_stays_pending(api, monkeypatch, registry, mock_db):
    chat_router, app = api
    _use(monkeypatch, chat_router, _mb_proposing({"card_id": "card-gone", "line_ids": ["l1"]}), registry)
    client = TestClient(app)
    pid = client.post("/api/chat", json={"message": "aura?", "agent": "birdy"}).json()["proposals"][0]["id"]
    r = client.post(f"/api/chat/proposals/{pid}/apply")
    assert r.status_code == 409 and r.json()["detail"] == "card_not_found"
    assert [p["id"] for p in client.get("/api/chat/proposals").json()["proposals"]] == [pid]


def test_sub_agent_proposal_status_follows_the_record(api, monkeypatch, registry, mock_db):
    """A new-sub-agent proposal is decided on its own card; lists and reopened chats show that."""

    from ai.agents.sub_agents import core as sub_core
    chat_router, app = api
    from tests.test_agent_pinning import tool_call
    p = TeamProvider({"media_buyer": [tool_call("propose_sub_agent", {
        "name": "Fatigue", "purpose": "Spots tired ads", "instructions": "Check frequency.",
        "tools": ["get_ad_insights"], "clients": "all"}), say("Proposed.")]})
    _use(monkeypatch, chat_router, p, registry)
    client = TestClient(app)
    first = client.post("/api/chat", json={"message": "make a watcher", "agent": "media_buyer"}).json()
    prop = first["proposals"][0]
    assert [x["id"] for x in client.get("/api/chat/proposals").json()["proposals"]] == [prop["id"]]
    sub_id = prop["payload"]["sub_agent"]["id"]
    _sync(sub_core.approve_sub_agent(mock_db, U, sub_id))
    assert client.get("/api/chat/proposals").json()["proposals"] == []
    msgs = client.get(f"/api/chat/conversations/{first['session_id']}").json()["messages"]
    assert msgs[-1]["proposals"][0]["status"] == "approved"
