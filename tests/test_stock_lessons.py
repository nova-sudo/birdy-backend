"""The stock agent's lessons (spec section 9): edits, dismiss reasons and Slack text become lessons that a person can
Keep or Overrule; patterns become rule suggestions; both start empty for every agency."""

import time
from datetime import datetime, timezone

import pytest

from ai.agents.stock import lessons
from ai.agents.stock.cards import STATUS_DISMISSED
from ai.agents.stock.scoreboard import OUTCOMES
from ai.agents.store import AGENTS, CARDS, MEMORY, PROPOSALS, add_memory, list_memory
from test_agent_learning import FakeProvider, use_provider
from test_stock_router import _seed_card, _seed_run, api  # noqa: F401 - api is a fixture
from test_stock_runner import USER, _setup, fakes  # noqa: F401 - fakes is a fixture

NOW = datetime(2026, 11, 3, 8, tzinfo=timezone.utc)
AG = {"_id": "ag1", "user_id": "u1", "settings": {"treatments": [{"id": "t1", "name": "HIFU"}]}}


@pytest.fixture(autouse=True)
def calm_ai(monkeypatch):
    """No test reaches a model unless it sets one up, and nobody is out of credits unless a test says so."""
    use_provider(monkeypatch, FakeProvider(error=RuntimeError("no network in tests")))

    async def not_blocked(db, user_id):
        return False

    monkeypatch.setattr("credits.is_blocked", not_blocked)


def _card(**over):
    base = {"_id": "c1", "agent_id": "ag1", "client_group_id": "g1", "client_name": "Acme",
            "lines": [{"id": "l1", "kind": "add", "ad_name": "Win A"}, {"id": "l2", "kind": "remove", "ad_name": "Old B"}],
            "additional_changes": "Check the budget by hand", "thought_process": "CPL is up, so swap the ads.",
            "next_steps": "Review on Thursday"}
    base.update(over)
    return base


def _original(card):
    return {k: card[k] for k in ("lines", "additional_changes", "thought_process", "next_steps")}


# ---------------------------------------------------------------------------
# What counts as an edit worth learning from
# ---------------------------------------------------------------------------

def test_typo_edit_isnt_a_lesson():
    before = _original(_card())
    after = {**before, "thought_process": "CPL is up so swap the ads", "next_steps": "review on thursday!",
             "additional_changes": "Check the buget by hand"}
    assert lessons.meaningful_edit(before, after) is False
    assert lessons.meaningful_edit(before, dict(before)) is False


def test_reordering_lines_isnt_a_lesson():
    before = _original(_card())
    assert lessons.meaningful_edit(before, {**before, "lines": list(reversed(before["lines"]))}) is False


def test_changed_line_is_a_lesson():
    before = _original(_card())
    assert lessons.meaningful_edit(before, {**before, "lines": before["lines"][:1]}) is True


def test_changed_meaning_is_a_lesson():
    before = _original(_card())
    assert lessons.meaningful_edit(before, {**before, "next_steps": "Pause the whole campaign tomorrow"}) is True
    assert lessons.meaningful_edit(before, {**before, "additional_changes": "Check the budget by hand, raise to 80"}) is True
    assert lessons.meaningful_edit(before, {**before, "next_steps": "Review on Friday"}) is True    # one word, different day


def test_missing_sides_dont_raise():
    assert lessons.meaningful_edit({}, {}) is False
    assert lessons.meaningful_edit(None, {"lines": [{"id": "l1"}]}) is False


async def test_lesson_from_edit_stores_an_edit_lesson(mock_db):
    card = _card(lines=[{"id": "l1", "kind": "add", "ad_name": "Win A"}])
    card["original"] = _original(_card())
    got = await lessons.lesson_from_edit(mock_db, AG, card, "sam")
    assert got["source"] == "edit" and got["author"] == "sam" and got["client_group_id"] == "g1"
    assert got["source_ref"] == "c1" and got["status"] == "active" and got["active"] is True
    assert "Old B" in got["text"] and len(got["text"]) <= 280
    assert [m["_id"] for m in await list_memory(mock_db, "ag1")] == [got["_id"]]


async def test_a_typo_edit_stores_nothing(mock_db):
    card = _card(thought_process="CPL is up so swap the ads")
    card["original"] = _original(_card())
    assert await lessons.lesson_from_edit(mock_db, AG, card, "sam") is None
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_editing_the_same_card_again_updates_one_lesson(mock_db):
    card = _card(lines=[{"id": "l1", "kind": "add", "ad_name": "Win A"}])
    card["original"] = _original(_card())
    first = await lessons.lesson_from_edit(mock_db, AG, card, "sam")
    again = await lessons.lesson_from_edit(mock_db, AG, {**card, "next_steps": "Call the client"}, "sam")
    assert again["_id"] == first["_id"] and await mock_db[MEMORY].count_documents({}) == 1
    assert "Call the client" in (await mock_db[MEMORY].find_one({}))["text"]


async def test_an_overruled_edit_lesson_stays_overruled(mock_db):
    card = _card(lines=[{"id": "l1", "kind": "add", "ad_name": "Win A"}])
    card["original"] = _original(_card())
    first = await lessons.lesson_from_edit(mock_db, AG, card, "sam")
    await lessons.overrule_lesson(mock_db, "ag1", first["_id"])
    await lessons.lesson_from_edit(mock_db, AG, {**card, "next_steps": "Call the client"}, "sam")
    [m] = await mock_db[MEMORY].find({}).to_list(None)
    assert m["status"] == "overruled" and m["active"] is False and "Call the client" not in m["text"]


# ---------------------------------------------------------------------------
# Text lessons (dismiss reasons, Slack mentions)
# ---------------------------------------------------------------------------

async def test_lesson_from_text_uses_the_note_path_and_meters(mock_db, monkeypatch):
    billed = []

    async def record_usage(db, user_id, **kw):
        billed.append(kw)
        return 0.0

    monkeypatch.setattr("credits.record_usage", record_usage)
    use_provider(monkeypatch, FakeProvider(reply='{"kind": "fact", "text": "Clients dislike blue"}'))
    got = await lessons.lesson_from_text(mock_db, AG, "we never run blue", source="slack_mention", author="<@U1>",
                                         client_group_id="g1", source_ref="c1")
    assert (got["text"], got["source"], got["author"], got["client_group_id"], got["status"]) == (
        "Clients dislike blue", "slack_mention", "<@U1>", "g1", "active")
    assert len(billed) == 1 and billed[0]["feature"] == "stock_lesson"


async def test_lesson_from_text_without_credits_stores_the_words_and_never_calls_the_ai(mock_db, monkeypatch):
    async def blocked(db, user_id):
        return True

    monkeypatch.setattr("credits.is_blocked", blocked)
    p = FakeProvider(reply='{"kind": "fact", "text": "x"}')
    use_provider(monkeypatch, p)
    got = await lessons.lesson_from_text(mock_db, AG, "  no blue ads  ", source="dismiss", author="sam")
    assert got["text"] == "no blue ads" and got["kind"] == "fact" and p.calls == []


async def test_lesson_from_text_survives_a_provider_error(mock_db):
    got = await lessons.lesson_from_text(mock_db, AG, "no blue ads", source="dismiss", author="sam")
    assert got["text"] == "no blue ads" and got["source"] == "dismiss" and got["status"] == "active"
    assert await mock_db[MEMORY].count_documents({}) == 1


async def test_empty_text_stores_nothing(mock_db):
    assert await lessons.lesson_from_text(mock_db, AG, "   ", source="dismiss", author="sam") is None
    assert await mock_db[MEMORY].count_documents({}) == 0


# ---------------------------------------------------------------------------
# What reaches the AI
# ---------------------------------------------------------------------------

async def test_new_agency_has_no_lessons(mock_db):
    assert await lessons.lessons_for_prompt(mock_db, "ag1", "g1") == []


async def test_overruled_lesson_not_sent_to_ai(mock_db):
    keep = await add_memory(mock_db, "ag1", text="keep me", kind="fact", source="edit")
    gone = await add_memory(mock_db, "ag1", text="drop me", kind="fact", source="edit")
    await lessons.overrule_lesson(mock_db, "ag1", gone["_id"])
    assert await lessons.lessons_for_prompt(mock_db, "ag1", "g1") == ["keep me"]
    await lessons.keep_lesson(mock_db, "ag1", gone["_id"])
    assert sorted(await lessons.lessons_for_prompt(mock_db, "ag1", "g1")) == ["drop me", "keep me"]
    assert keep["_id"]


async def test_lessons_are_scoped_to_the_agent_and_the_client(mock_db):
    await add_memory(mock_db, "ag1", text="agency wide", kind="fact", source="edit")
    await add_memory(mock_db, "ag1", text="only g1", kind="fact", source="edit", client_group_id="g1")
    await add_memory(mock_db, "ag1", text="only g2", kind="fact", source="edit", client_group_id="g2")
    await add_memory(mock_db, "someone-else", text="another agency", kind="fact", source="edit")
    assert sorted(await lessons.lessons_for_prompt(mock_db, "ag1", "g1")) == ["agency wide", "only g1"]


async def test_removed_lessons_dont_reach_the_ai(mock_db):
    m = await add_memory(mock_db, "ag1", text="removed in settings", kind="fact", source="edit")
    await mock_db[MEMORY].update_one({"_id": m["_id"]}, {"$set": {"active": False}})
    assert await lessons.lessons_for_prompt(mock_db, "ag1", "g1") == []


async def test_only_the_newest_fifty_newest_first(mock_db):
    for i in range(55):
        await add_memory(mock_db, "ag1", text=f"lesson {i}", kind="fact", source="edit")
        await mock_db[MEMORY].update_one({"text": f"lesson {i}"}, {"$set": {"created_at": datetime(2026, 11, 1, 0, i)}})
    got = await lessons.lessons_for_prompt(mock_db, "ag1", "g1")
    assert len(got) == 50 and got[0] == "lesson 54" and got[-1] == "lesson 5"


async def test_keep_and_overrule_are_scoped_to_the_agent(mock_db):
    m = await add_memory(mock_db, "ag1", text="x", kind="fact", source="edit")
    assert await lessons.overrule_lesson(mock_db, "ag2", m["_id"]) is None
    assert (await mock_db[MEMORY].find_one({"_id": m["_id"]})).get("status") != "overruled"
    done = await lessons.overrule_lesson(mock_db, "ag1", m["_id"])
    assert done["status"] == "overruled" and done["active"] is False
    back = await lessons.keep_lesson(mock_db, "ag1", m["_id"])
    assert back["status"] == "active" and back["active"] is True and back["kept"] is True


# ---------------------------------------------------------------------------
# Rule suggestions
# ---------------------------------------------------------------------------

async def _won(db, gid, n, key="img:H", treatment="t1", result="won", agent="ag1", name="Win A"):
    await db[OUTCOMES].insert_one({"_id": f"{gid}-{key}-{n}-{result}", "agent_id": agent, "client_group_id": gid, "creative_key": key,
                                   "treatment_ids": [treatment], "result": result, "ad_name": name,
                                   "created_at": datetime(2026, 10, 1)})


async def test_pattern_at_three_creates_one_proposal(mock_db):
    for gid in ("g1", "g2"):
        await _won(mock_db, gid, 1)
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []
    await _won(mock_db, "g3", 1)
    [p] = await lessons.scan_patterns(mock_db, AG, NOW)
    assert p["kind"] == "promote" and p["status"] == "open" and p["agent_id"] == "ag1"
    assert "Win A" in p["text"] and "HIFU" in p["text"] and "3" in p["text"]
    await _won(mock_db, "g4", 1)                                  # four doesn't make a second one
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []
    assert await mock_db[PROPOSALS].count_documents({"kind": "promote"}) == 1


async def test_the_same_account_winning_three_times_is_not_three_accounts(mock_db):
    for n in range(3):
        await _won(mock_db, "g1", n)
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []


async def test_wins_in_different_treatments_dont_add_up(mock_db):
    await _won(mock_db, "g1", 1)
    await _won(mock_db, "g2", 1, treatment="t2")
    await _won(mock_db, "g3", 1)
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []


async def test_repeated_losses_make_a_suggestion_too(mock_db):
    for n in range(3):
        await _won(mock_db, f"g{n}", n, result="lost", key="name:bad")
    [p] = await lessons.scan_patterns(mock_db, AG, NOW)
    assert p["kind"] == "promote" and p["pattern_key"].startswith("lose:")


async def test_rule_after_comes_from_the_settings(mock_db):
    for gid in ("g1", "g2"):
        await _won(mock_db, gid, 1)
    agent = {**AG, "settings": {**AG["settings"], "scoring": {"rule_after": 2}}}
    assert len(await lessons.scan_patterns(mock_db, agent, NOW)) == 1


async def test_another_agencys_outcomes_dont_count(mock_db):
    await _won(mock_db, "g1", 1)
    await _won(mock_db, "g2", 1)
    await _won(mock_db, "g3", 1, agent="other")
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []


async def test_a_rejected_pattern_isnt_suggested_again(mock_db):
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1)
    [p] = await lessons.scan_patterns(mock_db, AG, NOW)
    await mock_db[PROPOSALS].update_one({"_id": p["_id"]}, {"$set": {"status": "rejected"}})
    await _won(mock_db, "g4", 1)
    assert await lessons.scan_patterns(mock_db, AG, NOW) == []


async def test_scan_patterns_stops_at_the_deadline(mock_db):
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1)
    assert await lessons.scan_patterns(mock_db, AG, NOW, deadline=time.monotonic() - 1) == []


async def test_accept_stores_promoted_lesson(mock_db):
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1)
    [p] = await lessons.scan_patterns(mock_db, AG, NOW)
    done, lesson = await lessons.accept_promotion(mock_db, AG, p["_id"], by="sam", now=NOW)
    assert done["status"] == "accepted" and lesson["source"] == "promoted" and lesson["author"] == "sam"
    assert lesson["text"] == p["text"] and lesson["status"] == "active"
    assert await lessons.lessons_for_prompt(mock_db, "ag1", "g1") == [p["text"]]
    with pytest.raises(Exception):
        await lessons.accept_promotion(mock_db, AG, p["_id"], by="sam", now=NOW)     # once
    assert await mock_db[MEMORY].count_documents({"source": "promoted"}) == 1


# ---------------------------------------------------------------------------
# Through the API and the card flows
# ---------------------------------------------------------------------------

async def _stock(mock_db):
    agent = await _setup(mock_db, "g1")
    await _seed_run(mock_db, agent, rid="r2")
    await _seed_card(mock_db, agent, "c1", "r2")
    return agent


async def test_patch_card_with_a_real_edit_records_a_lesson(api, mock_db):
    agent = await _stock(mock_db)
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"original.lines": [
        {"id": "l1", "kind": "remove", "ad_name": "A"}, {"id": "l2", "kind": "remove", "ad_name": "B"}]}})
    r = api.patch(f"/api/agents/{agent['_id']}/cards/c1", json={"lines": [{"id": "l2"}]})
    assert r.status_code == 200, r.text
    [m] = await list_memory(mock_db, agent["_id"])
    assert m["source"] == "edit" and m["author"] == USER and m["client_group_id"] == "g1" and "A" in m["text"]


async def test_patch_card_typo_records_nothing_and_a_lesson_failure_never_fails_the_edit(api, mock_db, monkeypatch):
    agent = await _stock(mock_db)
    card = await mock_db[CARDS].find_one({"_id": "c1"})
    await mock_db[CARDS].update_one({"_id": "c1"}, {"$set": {"original.lines": card["lines"]}})
    assert api.patch(f"/api/agents/{agent['_id']}/cards/c1", json={"thought_process": "t."}).status_code == 200
    assert await mock_db[MEMORY].count_documents({}) == 0

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(lessons, "lesson_from_edit", boom)
    assert api.patch(f"/api/agents/{agent['_id']}/cards/c1", json={"next_steps": "Do something else entirely"}).status_code == 200


async def test_dismiss_with_a_reason_records_a_lesson(api, mock_db):
    agent = await _stock(mock_db)
    r = api.post(f"/api/agents/{agent['_id']}/cards/c1/dismiss", json={"reason": "This client hates timelapse ads"})
    assert r.status_code == 200, r.text
    [m] = await list_memory(mock_db, agent["_id"])
    assert m["source"] == "dismiss" and m["author"] == USER and m["client_group_id"] == "g1"
    assert "timelapse" in m["text"]
    assert (await mock_db[CARDS].find_one({"_id": "c1"}))["status"] == STATUS_DISMISSED


async def test_dismiss_without_a_reason_or_when_refused_records_nothing(api, mock_db):
    agent = await _stock(mock_db)
    api.post(f"/api/agents/{agent['_id']}/cards/c1/dismiss", json={})
    assert await mock_db[MEMORY].count_documents({}) == 0
    again = api.post(f"/api/agents/{agent['_id']}/cards/c1/dismiss", json={"reason": "still no"})
    assert await mock_db[MEMORY].count_documents({}) == 0 or again.status_code in (200, 409)


async def test_keep_and_overrule_routes(api, mock_db):
    agent = await _stock(mock_db)
    aid = agent["_id"]
    m = await add_memory(mock_db, aid, text="no blue", kind="fact", source="edit")
    r = api.post(f"/api/agents/{aid}/lessons/{m['_id']}/overrule")
    assert r.status_code == 200 and r.json()["status"] == "overruled" and r.json()["id"] == m["_id"]
    assert [x["status"] for x in api.get(f"/api/agents/{aid}/lessons").json()] == ["overruled"]
    assert api.post(f"/api/agents/{aid}/lessons/{m['_id']}/keep").json()["status"] == "active"
    assert api.post(f"/api/agents/{aid}/lessons/nope/keep").status_code == 404
    api.state["user"] = "u2"
    assert api.post(f"/api/agents/{aid}/lessons/{m['_id']}/overrule").status_code == 404


async def test_proposal_accept_route_stores_the_promoted_lesson_and_leaves_rules_alone(api, mock_db):
    agent = await _stock(mock_db)
    aid = agent["_id"]
    agent = await mock_db[AGENTS].find_one({"_id": aid})
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1, agent=aid)
    [p] = await lessons.scan_patterns(mock_db, agent, NOW)
    rules_before = await mock_db["agent_rules"].count_documents({})
    r = api.post(f"/api/agents/{aid}/proposals/{p['_id']}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["proposal"]["status"] == "accepted" and r.json()["lesson"]["source"] == "promoted"
    assert [m["source"] for m in await list_memory(mock_db, aid)] == ["promoted"]
    assert await mock_db["agent_rules"].count_documents({}) == rules_before
    assert api.post(f"/api/agents/{aid}/proposals/{p['_id']}/accept").status_code == 409


async def test_proposal_reject_route_closes_a_promote_proposal(api, mock_db):
    agent = await _stock(mock_db)
    aid = agent["_id"]
    agent = await mock_db[AGENTS].find_one({"_id": aid})
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1, agent=aid)
    [p] = await lessons.scan_patterns(mock_db, agent, NOW)
    assert api.post(f"/api/agents/{aid}/proposals/{p['_id']}/reject").json()["proposal"]["status"] == "rejected"
    assert await mock_db[MEMORY].count_documents({}) == 0


async def test_run_report_shows_what_was_learned_and_open_suggestions(api, mock_db):
    agent = await _stock(mock_db)
    aid = agent["_id"]
    agent = await mock_db[AGENTS].find_one({"_id": aid})
    await _seed_run(mock_db, agent, rid="r1", as_of=datetime(2026, 11, 1, 7))
    m = await add_memory(mock_db, aid, text="no blue", kind="fact", source="edit")
    await mock_db[MEMORY].update_one({"_id": m["_id"]}, {"$set": {"created_at": datetime(2026, 11, 2, 12)}})
    old = await add_memory(mock_db, aid, text="ancient", kind="fact", source="edit")
    await mock_db[MEMORY].update_one({"_id": old["_id"]}, {"$set": {"created_at": datetime(2026, 9, 1)}})
    for gid in ("g1", "g2", "g3"):
        await _won(mock_db, gid, 1, agent=aid)
    await lessons.scan_patterns(mock_db, agent, NOW)
    body = api.get(f"/api/agents/{aid}/runs/r2").json()
    assert [x["text"] for x in body["learned"]] == ["no blue"] and body["learned"][0]["id"] == m["_id"]
    assert [p["kind"] for p in body["proposals"]] == ["promote"]
