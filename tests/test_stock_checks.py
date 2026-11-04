from datetime import date, datetime, timezone

import pytest

from agent_history_helpers import seed_history
from ai.agents.snapshot import AdDay, AdState
from ai.agents.stock.checks import client_facts, cpl, decide, follow_ups, zone
from ai.agents.stock.settings import DEFAULT_SETTINGS, Zones
from ai.agents.stock.windows import full_day_window
from ai.agents.store import AD_DAYS

NOW = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
WIN = full_day_window(NOW, "UTC")  # 2026-10-27..11-02, previous 10-20..10-26


@pytest.fixture(autouse=True)
async def _synced(mock_db):
    """g1's ad history is usable and synced after the window's last day."""
    await seed_history(mock_db, "g1", last_topup_at=datetime(2026, 11, 3, 2))


def test_cpl():
    assert cpl(50, 5) == 10 and cpl(50, 0) is None


@pytest.mark.parametrize("spend,leads,z", [(0, 0, "no_data"), (50, 0, "red"), (70, 10, "green"),
                                           (80, 10, "yellow"), (100, 10, "yellow"), (101, 10, "red")])
def test_zone(spend, leads, z):
    assert zone(spend, leads, Zones(8, 10)) == z


def test_decide_table():
    assert decide("red", 10, 7, False, 30) == "hold_downtrend"  # exactly 30% counts
    assert decide("red", 10, 7.5, False, 30) == "propose"
    assert decide("red", None, None, False, 30) == "propose"
    assert decide("yellow", 8, 9, False, 30) == "propose"
    assert decide("yellow", 9, 8.5, False, 30) == "none"
    assert decide("yellow", 9, 9, False, 30) == "none"
    assert decide("green", 1, 9, True, 30) == "spend_stopped"
    assert decide("green", 9, 9, False, 30) == "none"
    assert decide("no_data", None, None, False, 30) == "none"


def _agent(**settings):
    return {"_id": "ag1", "user_id": "u", "settings": {
        "zones": {"currency": "GBP", "green_under": 8, "red_over": 10}, **settings}}


GROUP = {"id": "g1", "user_id": "u", "name": "Acme", "ad_account_currency": "GBP", "meta_campaign_ids": ["c1"]}


async def _day(db, ad_id, day, spend, leads, campaign="c1"):
    await db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": "g1", "ad_id": ad_id,
                                  "campaign_id": campaign, "date_start": day, "ad_name": ad_id,
                                  "insight_data": {"spend": str(spend),
                                                   "actions": [{"action_type": "lead", "value": str(leads)}]}})


async def test_client_facts_numbers(mock_db):
    await _day(mock_db, "a1", "2026-11-01", 60, 6)
    await _day(mock_db, "a2", "2026-11-02", 40, 2)
    await _day(mock_db, "a1", "2026-10-25", 90, 9)
    await _day(mock_db, "x", "2026-11-02", 500, 1, campaign="other")  # outside the client's campaigns
    await mock_db["meta_ad_state"].insert_one({"client_group_id": "g1", "ad_id": "a1", "name": "Ad one",
                                               "campaign_id": "c1", "effective_status": "ACTIVE"})
    f = await client_facts(mock_db, _agent(), GROUP, WIN)
    assert (f["spend"], f["leads"], f["cpl"]) == (100, 8, 12.5)
    assert f["prev_cpl"] == 10 and f["currency"] == "GBP"
    assert f["zone"] == "red" and f["decision"] == "propose"
    assert f["zones"] == {"green_under": 8, "red_over": 10} and f["ads_live"] == 1
    a1 = next(a for a in f["ads"] if a["ad_id"] == "a1")
    assert a1["spend_share"] == 0.6 and a1["cpl"] == 10 and a1["name"] == "Ad one"
    assert not f["spend_stopped"]


async def test_no_spend_is_no_data_not_an_error(mock_db):
    f = await client_facts(mock_db, _agent(), GROUP, WIN)
    assert f["zone"] == "no_data" and f["decision"] == "none" and f["cpl"] is None


async def test_spend_stopped_when_last_two_days_have_no_row(mock_db):
    await _day(mock_db, "a1", "2026-10-30", 50, 5)  # nothing on 11-01 or 11-02
    f = await client_facts(mock_db, _agent(), GROUP, WIN)
    assert f["spend_stopped"] and f["decision"] == "spend_stopped"


async def test_no_usable_zones_does_not_raise(mock_db):
    agent = _agent()
    agent["settings"]["zones"]["currency"] = "USD"  # default does not cover a GBP client
    await _day(mock_db, "a1", "2026-11-02", 50, 5)
    f = await client_facts(mock_db, agent, GROUP, WIN)
    assert f["zones"] is None and f["zone"] == "no_zones" and f["decision"] == "no_zones"
    assert f["cpl"] == 10  # facts still reported


async def test_partial_override_gives_no_zones(mock_db):
    agent = _agent(clients={"g1": {"zones": {"green_under": 5}}})
    f = await client_facts(mock_db, agent, GROUP, WIN)
    assert f["zones"] is None


def _state(ad_id, status):
    return AdState(ad_id, "s", "c1", ad_id, status, None, None)


def _d(ad_id, day, spend):
    return AdDay(ad_id, "s", "c1", ad_id, day, spend, 0)


def _card(**kw):
    return {"created_at": datetime(2026, 10, 30, 8), "additional_changes": "Check the form", "lines": [
        {"id": "l1", "kind": "add", "ad_name": "New ad", "meta_ids": {"ad_id": "n1"}, "state": "applied"},
        {"id": "l2", "kind": "remove", "ad_name": "Old ad", "meta_ids": {"ad_id": "o1"}, "state": "applied"},
        {"id": "l3", "kind": "revive", "ad_name": "Back ad", "meta_ids": ["r1"], "state": "applied"}], **kw}


def test_follow_ups_check_each_line():
    states = {"n1": _state("n1", "ACTIVE"), "o1": _state("o1", "PAUSED"), "r1": _state("r1", "ACTIVE")}
    days = {"n1": [_d("n1", date(2026, 10, 31), 5)], "r1": [_d("r1", date(2026, 10, 29), 5)]}
    out = follow_ups(_card(), states, days)
    assert [(o["line_id"], o["ok"]) for o in out] == [("l1", True), ("l2", True), ("l3", False),
                                                      ("additional_changes", None)]
    assert "not spending" in out[2]["text"]


def test_follow_ups_remove_still_live_and_missing_ad():
    out = follow_ups(_card(), {"o1": _state("o1", "ACTIVE")}, {})
    assert out[0]["ok"] is False and out[1]["ok"] is False and out[1]["text"].endswith("still live")


def test_follow_up_additional_changes_is_check_by_hand():
    out = follow_ups(_card(), {}, {})
    assert out[-1]["ok"] is None and "check by hand" in out[-1]["text"].lower()


def test_no_additional_changes_no_hand_line():
    assert all(o["ok"] is not None for o in follow_ups(_card(additional_changes=""), {}, {}))


def test_decide_zero_or_missing_previous_cpl():
    assert decide("red", 0, 5, False, 30) == "propose"
    assert decide("red", None, 5, False, 30) == "propose"
    assert decide("yellow", 0, 9, False, 30) == "propose"
    assert decide("yellow", None, 9, False, 30) == "none"


def _fcard(*lines, **kw):
    return {"created_at": datetime(2026, 10, 30, 8), "lines": list(lines), **kw}


def test_follow_ups_only_check_applied_or_flagged_lines():
    states = {"n1": _state("n1", "ACTIVE")}
    lines = [{"id": f"l{i}", "kind": "add", "ad_name": "A", "meta_ids": {"ad_id": "n1"}, "state": st}
             for i, st in enumerate(["proposed", "queued", "applying", "failed", "needs_file", "blocked",
                                     "undone"])]
    assert follow_ups(_fcard(*lines), states, {}) == []
    assert follow_ups(_fcard(*lines[:1], status="dismissed"), states, {}) == []
    applied = {"id": "a", "kind": "add", "ad_name": "A", "meta_ids": {"ad_id": "n1"}, "state": "applied"}
    assert [o["line_id"] for o in follow_ups(_fcard(applied), states, {})] == ["a"]
    assert follow_ups(_fcard(applied, status="dismissed"), states, {}) == []


def test_follow_ups_flagged_add_without_id_found_by_name():
    line = {"id": "f", "kind": "add", "ad_name": "Clip - 031126", "state": "flagged"}
    states = {"n9": _state("n9", "ACTIVE")}
    states["n9"] = AdState("n9", "s", "c1", "Clip - 101026 \u2013 Copy", "ACTIVE", None, None)
    days = {"n9": [_d("n9", date(2026, 10, 31), 5)]}
    out = follow_ups(_fcard(line), states, days)
    assert out[0]["ok"] is True


def test_follow_ups_flagged_add_not_there_yet_is_not_a_cross():
    line = {"id": "f", "kind": "add", "ad_name": "Clip", "state": "flagged"}
    out = follow_ups(_fcard(line), {"x": _state("x", "ACTIVE")}, {})
    assert out[0]["ok"] is None and "not done yet" in out[0]["text"]


def test_follow_ups_applied_add_with_missing_ad_is_still_a_cross():
    line = {"id": "f", "kind": "add", "ad_name": "Clip", "meta_ids": {"ad_id": "gone"}, "state": "applied"}
    assert follow_ups(_fcard(line), {}, {})[0]["ok"] is False


@pytest.mark.parametrize("seed,decision", [
    ({"last_topup_at": datetime(2026, 11, 2, 20)}, "data_late"),             # synced before the day ended
    ({"status": "running", "last_topup_at": None}, "no_history"),            # still backfilling
])
async def test_spend_stopped_needs_ready_history(mock_db, seed, decision):
    await seed_history(mock_db, "g1", **seed)
    await _day(mock_db, "a1", "2026-10-28", 60, 6)  # nothing newer: reads as stopped on stale rows
    f = await client_facts(mock_db, _agent(), GROUP, WIN)
    assert f["spend_stopped"] is False and f["decision"] == decision and f["zone"] == "no_data"


async def test_no_zones_wins_over_late_data(mock_db):
    await seed_history(mock_db, "g1", last_topup_at=datetime(2026, 11, 2, 20))
    f = await client_facts(mock_db, _agent(), {**GROUP, "ad_account_currency": "USD"}, WIN)
    assert f["decision"] == "no_zones"


async def test_history_is_judged_in_the_accounts_timezone(mock_db):
    # 23:30 UTC on 2 Nov is already 3 Nov in Dubai, so 2 Nov is complete there.
    await seed_history(mock_db, "g1", last_topup_at=datetime(2026, 11, 2, 23, 30))
    group = {**GROUP, "ad_account_timezone": "Asia/Dubai"}
    assert (await client_facts(mock_db, _agent(), group, WIN))["decision"] != "data_late"
    assert (await client_facts(mock_db, _agent(), GROUP, WIN))["decision"] == "data_late"


def test_follow_ups_skip_superseded_cards():
    states = {"o1": _state("o1", "PAUSED")}
    line = {"id": "l1", "kind": "remove", "state": "flagged", "meta_ids": ["o1"], "ad_name": "o1"}
    assert follow_ups(_fcard(line), states, {}) != []
    assert follow_ups(_fcard(line, status="superseded"), states, {}) == []
