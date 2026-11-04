"""The stock agent's scoreboard (spec section 9): scoring rules, the due check, flagged Adds and catch-up."""

import time
from datetime import datetime, timezone

import pytest

from ai.agents.stock import lessons, runner, scoreboard
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.stock.scoreboard import OUTCOMES, record_add, record_for
from ai.agents.store import AD_DAYS, CARDS

NOW = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
LIVE = datetime(2026, 10, 20, 10)           # 7 days: 20th to 26th
AGENT = {"_id": "ag1", "user_id": "u", "scope": {"schedule": {"tz": "UTC"}},
         "settings": {"zones": {"currency": "GBP", "green_under": 8, "red_over": 15}}}
SCORING = DEFAULT_SETTINGS["scoring"]


@pytest.mark.parametrize("spend,leads,base,want", [
    (0, 0, 8, "never_went_live"), (40, 5, 8, "won"), (40, 5, 7.99, "no_clear_change"),
    (50, 6, 8, "no_clear_change"), (66, 6, 8, "lost"),
    (48, 0, 8, "lost"), (47, 0, 8, "no_clear_change"),
    (10, 1, 8, "no_clear_change"), (48, 1, 8, "lost"),
])
def test_score_result(spend, leads, base, want):
    assert lessons.score_result(spend, leads, base, 48, SCORING) == want


def test_score_result_follows_the_settings_not_fixed_numbers():
    tight = {**SCORING, "win_min_leads": 3, "lose_cpl_multiple": 1.1, "lose_max_leads": 0}
    assert lessons.score_result(24, 3, 8, 48, tight) == "won"
    assert lessons.score_result(27, 3, 8, 48, tight) == "lost"      # 9 > 1.1 * 8
    assert lessons.score_result(20, 1, 8, 48, tight) == "lost"      # one lead is now enough to judge by CPL: 20 > 8.8
    assert lessons.score_result(20, 1, 8, 48, SCORING) == "no_clear_change"  # at most 1 lead: only the spend limit counts


def test_score_result_without_a_baseline_or_a_spend_limit_judges_only_what_it_can():
    assert lessons.score_result(0, 0, None, 48, SCORING) == "never_went_live"
    assert lessons.score_result(48, 0, None, 48, SCORING) == "lost"            # the spend limit needs no baseline
    assert lessons.score_result(40, 5, None, 48, SCORING) is None              # won or not can't be told
    assert lessons.score_result(66, 6, 8, None, SCORING) == "lost"             # CPL alone
    assert lessons.score_result(60, 0, 8, None, SCORING) is None               # no spend limit, no leads to judge by


async def _row(db, key="k1", gid="dest", ad="ad1", live=LIVE, base=8.0, card="c1", line="l1", tids=("t1",), agent="ag1"):
    await record_add(db, {"_id": card, "agent_id": agent, "client_group_id": gid, "current_cpl": base, "currency": "GBP"},
                     {"id": line, "treatment_ids": list(tids), "creative_key": key, "ad_name": "Ad"}, ad, live)
    return f"{card}:{line}"


async def _day(db, ad, day, spend, leads, gid="dest"):
    await db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": gid, "ad_id": ad, "campaign_id": "c", "date_start": day,
                                  "ad_name": ad, "insight_data": {"spend": str(spend),
                                                                  "actions": [{"action_type": "lead", "value": str(leads)}]}})


async def _week(db, ad, spend_per_day, leads_per_day, gid="dest", first=20, days=7):
    for d in range(first, first + days):
        await _day(db, ad, f"2026-10-{d}", spend_per_day, leads_per_day, gid)


async def test_new_agency_has_no_rows(mock_db):
    assert await mock_db[OUTCOMES].count_documents({}) == 0
    assert await lessons.score_due(mock_db, AGENT, NOW) == []
    assert await record_for(mock_db, "ag1", "k1") == {"won": 0, "lost": 0, "moves": 0}


async def test_scored_after_min_days_and_drives_the_record(mock_db):
    rid = await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)   # 56 spend, 7 leads: CPL 8, baseline 8
    [done] = await lessons.score_due(mock_db, AGENT, NOW)
    assert done["_id"] == rid and done["result"] == "won"
    row = await mock_db[OUTCOMES].find_one({"_id": rid})
    assert (row["spend"], row["leads"], row["cpl"], row["result"]) == (56.0, 7, 8.0, "won") and row["scored_at"]
    assert await record_for(mock_db, "ag1", "k1") == {"won": 1, "lost": 0, "moves": 1}
    assert await lessons.score_due(mock_db, AGENT, NOW) == []     # scored once


async def test_a_lost_row_counts_toward_lost_twice(mock_db):
    for i in (1, 2):
        await _row(mock_db, ad=f"ad{i}", card=f"c{i}")
        await _week(mock_db, f"ad{i}", 12, 1)   # CPL 12 > 10.4
    assert {r["result"] for r in await lessons.score_due(mock_db, AGENT, NOW)} == {"lost"}
    assert (await record_for(mock_db, "ag1", "k1", ["t1"]))["lost"] == 2


async def test_not_scored_before_min_days(mock_db):
    rid = await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 26, 23, tzinfo=timezone.utc)) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": rid}))["result"] is None
    assert len(await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 27, 1, tzinfo=timezone.utc))) == 1


async def test_only_the_first_min_days_count(mock_db):
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1, days=9)   # days after the window are ignored
    [done] = await lessons.score_due(mock_db, AGENT, NOW)
    assert done["spend"] == 56.0


async def test_min_days_comes_from_the_agents_settings(mock_db):
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1, days=3)
    agent = {**AGENT, "settings": {**AGENT["settings"], "scoring": {"min_days": 3}}}
    [done] = await lessons.score_due(mock_db, agent, NOW)
    assert done["spend"] == 24.0


async def test_undone_and_other_agents_rows_are_never_scored(mock_db):
    await _row(mock_db, card="c1")
    await scoreboard.mark_add_undone(mock_db, {"_id": "c1"}, {"id": "l1"}, NOW)
    await _row(mock_db, card="c2", agent="other", ad="ad2")
    await _week(mock_db, "ad1", 8, 1)
    await _week(mock_db, "ad2", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, NOW) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] == "undone"


async def test_never_went_live_is_not_won_or_lost(mock_db):
    rid = await _row(mock_db)
    await _week(mock_db, "other-ad", 5, 1)    # the client has data, this ad has none
    [done] = await lessons.score_due(mock_db, AGENT, NOW)
    assert done["result"] == "never_went_live"
    assert await record_for(mock_db, "ag1", "k1") == {"won": 0, "lost": 0, "moves": 1}
    assert (await mock_db[OUTCOMES].find_one({"_id": rid}))["spend"] == 0


async def test_waits_while_the_clients_ad_data_has_not_arrived_then_gives_up(mock_db):
    await _row(mock_db)
    assert await lessons.score_due(mock_db, AGENT, NOW) == []   # no ad rows at all yet
    late = datetime(2026, 11, 5, tzinfo=timezone.utc)
    [done] = await lessons.score_due(mock_db, AGENT, late)
    assert done["result"] == "never_went_live"


async def test_no_baseline_leaves_the_row_unscored_without_raising(mock_db):
    rid = await _row(mock_db, base=None)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, NOW) == []
    row = await mock_db[OUTCOMES].find_one({"_id": rid})
    assert row["result"] is None and "baseline" in row["unscored_reason"]


async def test_lost_spend_comes_from_the_destination_zones(mock_db):
    await _row(mock_db, base=None)
    await _week(mock_db, "ad1", 7, 0)   # 49 spend, no leads, limit 6 x 8 = 48
    [done] = await lessons.score_due(mock_db, AGENT, NOW)
    assert done["result"] == "lost"


async def test_score_due_stops_at_the_deadline(mock_db):
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, NOW, deadline=time.monotonic() - 1) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None


# ---------------------------------------------------------------------------
# Flagged Adds
# ---------------------------------------------------------------------------

async def _flagged_card(db, cid="fc1", status="flagged", name="BS - Timelapse - 100926 - Win", state="flagged", created=datetime(2026, 10, 19, 9)):
    await db[CARDS].insert_one({
        "_id": cid, "agent_id": "ag1", "user_id": "u", "client_group_id": "dest", "status": status, "current_cpl": 9.0,
        "currency": "GBP", "created_at": created,
        "lines": [{"id": "l1", "kind": "add", "state": state, "ad_name": name, "creative_key": "img:H",
                   "treatment_ids": ["t1"]}]})


async def _meta_ad(db, ad_id, name, created=datetime(2026, 10, 21, 9)):
    await db["meta_ad_state"].insert_one({"client_group_id": "dest", "ad_id": ad_id, "name": name,
                                          "effective_status": "ACTIVE", "created_time": created})


async def test_flagged_add_is_recorded_once_a_person_created_and_ran_the_ad(mock_db):
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "m1", "BS - Timelapse - 102126 - Win - Copy")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    await _day(mock_db, "m1", "2026-10-23", 6, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 1
    row = await mock_db[OUTCOMES].find_one({"_id": "fc1:l1"})
    assert (row["ad_id"], row["creative_key"], row["treatment_ids"], row["baseline_cpl"], row["currency"]) == (
        "m1", "img:H", ["t1"], 9.0, "GBP")
    assert row["live_since"] == datetime(2026, 10, 22) and row["flagged"] is True and row["result"] is None
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0     # once
    assert await mock_db[OUTCOMES].count_documents({}) == 1


async def test_flagged_add_waits_until_the_ad_exists_and_has_spent(mock_db):
    await _flagged_card(mock_db)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0     # no spend yet
    assert await mock_db[OUTCOMES].count_documents({}) == 0


async def test_flagged_add_ignores_older_ads_other_names_and_dismissed_cards(mock_db):
    await _flagged_card(mock_db, "fc1")
    await _flagged_card(mock_db, "fc2", status="dismissed")
    await _meta_ad(mock_db, "old", "BS - Timelapse - Win", created=datetime(2026, 9, 1))
    await _meta_ad(mock_db, "other", "Something else")
    for ad in ("old", "other", "m-dismissed"):
        await _day(mock_db, ad, "2026-10-22", 5, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0


async def test_applied_lines_and_old_cards_get_no_flagged_row(mock_db):
    await _flagged_card(mock_db, "fc1", state="applied")
    await _flagged_card(mock_db, "fc2", created=datetime(2026, 8, 1))
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0


async def test_a_superseded_flagged_card_is_still_matched(mock_db):
    await _flagged_card(mock_db, status="superseded")
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 1


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------

async def test_catch_up_scores_matches_and_scans(mock_db, mock_mongo_client):
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    run = {"_id": "r1", "agent_id": "ag1", "as_of": NOW.replace(tzinfo=None), "tick_deadline": time.monotonic() + 60}
    await runner.catch_up_run(mock_db, mock_mongo_client, run, AGENT)
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] == "won"
    assert await mock_db[OUTCOMES].find_one({"_id": "fc1:l1"}) is not None


async def test_catch_up_one_failure_does_not_stop_the_rest(mock_db, mock_mongo_client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(lessons, "score_due", boom)
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    run = {"_id": "r1", "agent_id": "ag1", "as_of": NOW.replace(tzinfo=None)}
    await runner.catch_up_run(mock_db, mock_mongo_client, run, AGENT)
    assert await mock_db[OUTCOMES].find_one({"_id": "fc1:l1"}) is not None


async def test_catch_up_does_nothing_past_the_deadline(mock_db, mock_mongo_client):
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    run = {"_id": "r1", "agent_id": "ag1", "as_of": NOW.replace(tzinfo=None), "tick_deadline": time.monotonic() - 1}
    await runner.catch_up_run(mock_db, mock_mongo_client, run, AGENT)
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None


async def test_every_outcome_field_the_api_lists_exists_on_a_scored_row(mock_db):
    import routers.agent_stock as api

    rid = await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    await lessons.score_due(mock_db, AGENT, NOW)
    row = await mock_db[OUTCOMES].find_one({"_id": rid})
    only_sometimes = {"flagged", "unscored_reason"}   # flagged Adds, and rows that can't be judged
    assert [k for k in api.OUTCOME_FIELDS if k not in row and k not in only_sometimes] == []
    assert {"ad_id", "baseline_cpl", "live_since"} <= set(api.OUTCOME_FIELDS)
