"""The stock agent's scoreboard (spec section 9): scoring rules, the due check, flagged Adds and catch-up."""

import time
from datetime import datetime, timezone

import pytest

from ai.agents.stock import lessons, runner, scoreboard
from ai.agents.stock.settings import DEFAULT_SETTINGS
from ai.agents.stock.scoreboard import OUTCOMES, record_add, record_for
from ai.agents.stock.alerts import HISTORY
from ai.agents.store import AD_DAYS, CARDS

NOW = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
LIVE = datetime(2026, 10, 20, 10)           # 7 days: 20th to 26th
AGENT = {"_id": "ag1", "user_id": "u", "scope": {"schedule": {"tz": "UTC"}},
         "settings": {"zones": {"currency": "GBP", "green_under": 8, "red_over": 15}}}
SCORING = DEFAULT_SETTINGS["scoring"]


async def _dest(db, tz="UTC", campaigns=None, synced=datetime(2026, 10, 28, 6)):
    """The destination client: its account timezone, its campaign scope, and when its ad history last synced."""
    await db["client_groups"].replace_one({"id": "dest"}, {
        "id": "dest", "user_id": "u", "name": "Dest", "ad_account_currency": "GBP", "ad_account_timezone": tz,
        "meta_campaign_ids": campaigns}, upsert=True)
    await db[HISTORY].replace_one({"_id": "dest"}, {"_id": "dest", "status": "ready", "last_topup_at": synced}, upsert=True)


@pytest.fixture(autouse=True)
async def dest_client(mock_db):
    await _dest(mock_db)


@pytest.mark.parametrize("spend,leads,base,want", [
    (0, 0, 8, "never_went_live"), (40, 5, 8, "won"), (40, 5, 7.99, "no_clear_change"),
    (50, 6, 8, "no_clear_change"), (66, 6, 8, "lost"),
    (48, 0, 8, "lost"), (47, 0, 8, "no_clear_change"),
    (10, 1, 8, "no_clear_change"), (48, 1, 8, "lost"),
    (30, 1, 8, "lost"), (20, 1, 8, "lost"),          # one lead: CPL 3.75x and 2.5x the baseline
])
def test_score_result(spend, leads, base, want):
    assert lessons.score_result(spend, leads, base, 48, SCORING) == want


def test_score_result_follows_the_settings_not_fixed_numbers():
    tight = {**SCORING, "win_min_leads": 3, "lose_cpl_multiple": 1.1, "lose_max_leads": 0}
    assert lessons.score_result(24, 3, 8, 48, tight) == "won"
    assert lessons.score_result(27, 3, 8, 48, tight) == "lost"      # 9 > 1.1 * 8
    assert lessons.score_result(20, 1, 8, 48, tight) == "lost"      # 20 > 8.8
    assert lessons.score_result(9, 1, 8, 48, SCORING) == "no_clear_change"   # 9 is under 1.3 x 8 and under the limit


def test_score_result_without_a_baseline_or_a_spend_limit_judges_only_what_it_can():
    assert lessons.score_result(0, 0, None, 48, SCORING) == "never_went_live"
    assert lessons.score_result(48, 0, None, 48, SCORING) == "lost"            # the spend limit needs no baseline
    assert lessons.score_result(40, 5, None, 48, SCORING) is None              # won or not can't be told
    assert lessons.score_result(66, 6, 8, None, SCORING) == "lost"             # CPL alone
    assert lessons.score_result(60, 0, 8, None, SCORING) is None               # no spend limit, no leads to judge by
    assert lessons.score_result(30, 1, 8, None, SCORING) == "lost"             # one lead still has a CPL


async def _row(db, key="k1", gid="dest", ad="ad1", live=LIVE, base=8.0, card="c1", line="l1", tids=("t1",), agent="ag1"):
    await record_add(db, {"_id": card, "agent_id": agent, "client_group_id": gid, "current_cpl": base, "currency": "GBP"},
                     {"id": line, "treatment_ids": list(tids), "creative_key": key, "ad_name": "Ad"}, ad, live)
    return f"{card}:{line}"


async def _day(db, ad, day, spend, leads, gid="dest", campaign="c"):
    await db[AD_DAYS].insert_one({"user_id": "u", "client_group_id": gid, "ad_id": ad, "campaign_id": campaign, "date_start": day,
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


async def test_waits_while_the_clients_history_has_not_synced_past_the_window(mock_db):
    await _dest(mock_db, synced=datetime(2026, 10, 26, 12))      # synced on the window's last day: not complete
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, NOW) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None
    await _dest(mock_db, synced=datetime(2026, 10, 28, 6))
    assert len(await lessons.score_due(mock_db, AGENT, NOW)) == 1


async def test_after_the_wait_with_no_data_the_row_is_unscored_never_never_went_live_or_lost(mock_db):
    await _dest(mock_db, synced=datetime(2026, 10, 26, 12))
    await _row(mock_db)
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 5, tzinfo=timezone.utc)) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 12, tzinfo=timezone.utc)) == []
    row = await mock_db[OUTCOMES].find_one({"_id": "c1:l1"})
    assert row["result"] == "unscored" and "never arrived" in row["unscored_reason"]
    assert await record_for(mock_db, "ag1", "k1") == {"won": 0, "lost": 0, "moves": 0}


async def test_a_zero_spend_week_with_the_history_synced_is_never_went_live(mock_db):
    await _row(mock_db)                     # no ad rows at all: Meta writes none for a day with no spend
    [done] = await lessons.score_due(mock_db, AGENT, NOW)
    assert done["result"] == "never_went_live"


async def test_late_data_that_shows_no_spend_is_unscored_not_never_went_live(mock_db):
    await _dest(mock_db, synced=datetime(2026, 10, 26, 12))
    await _row(mock_db)
    await _week(mock_db, "other-ad", 5, 1)
    await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 12, tzinfo=timezone.utc))
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] == "unscored"


async def test_an_unsynced_row_is_never_scored_on_partial_data(mock_db):
    await _dest(mock_db, synced=datetime(2026, 10, 26, 12))
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)                                # spend is there, the sync isn't
    day = lambda d: datetime(2026, 11, d, tzinfo=timezone.utc)
    assert await lessons.score_due(mock_db, AGENT, day(1)) == []     # more than 3 days past the window
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None
    await _dest(mock_db, synced=datetime(2026, 11, 1, 6))            # the sync arrives on day 5 after the window
    [done] = await lessons.score_due(mock_db, AGENT, day(1))
    assert done["result"] == "won"


async def test_an_unsynced_row_with_spend_ends_unscored_at_14_days(mock_db):
    await _dest(mock_db, synced=datetime(2026, 10, 26, 12))
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 12, tzinfo=timezone.utc)) == []
    row = await mock_db[OUTCOMES].find_one({"_id": "c1:l1"})
    assert row["result"] == "unscored" and "never arrived" in row["unscored_reason"]


@pytest.mark.parametrize("doc", [None, {"_id": "dest", "status": "error", "error": "x", "last_topup_at": None}])
async def test_no_history_doc_or_an_errored_one_without_a_sync_never_scores(mock_db, doc):
    await mock_db[HISTORY].delete_many({"_id": "dest"})
    if doc:
        await mock_db[HISTORY].insert_one(doc)
    await _row(mock_db)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 5, tzinfo=timezone.utc)) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] is None
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 12, tzinfo=timezone.utc)) == []
    assert (await mock_db[OUTCOMES].find_one({"_id": "c1:l1"}))["result"] == "unscored"


async def test_no_baseline_waits_then_ends_unscored_after_14_days_and_stops_being_selected(mock_db):
    rid = await _row(mock_db, base=None)
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, NOW) == []
    row = await mock_db[OUTCOMES].find_one({"_id": rid})
    assert row["result"] is None and "baseline" in row["unscored_reason"]
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 11, 12, tzinfo=timezone.utc)) == []
    row = await mock_db[OUTCOMES].find_one({"_id": rid})
    assert row["result"] == "unscored" and "baseline" in row["unscored_reason"] and row["scored_at"]
    assert await record_for(mock_db, "ag1", "k1") == {"won": 0, "lost": 0, "moves": 0}


async def test_rows_that_cant_be_judged_dont_starve_newer_ones_under_the_cap(mock_db, monkeypatch):
    monkeypatch.setattr(lessons, "MAX_SCORE_ROWS", 1)
    await _row(mock_db, base=None, card="stuck", ad="ad0", live=datetime(2026, 10, 19, 10))
    await _week(mock_db, "ad0", 8, 1, first=19)
    for i in (1, 2):
        await _row(mock_db, card=f"c{i}", ad=f"ad{i}")
        await _week(mock_db, f"ad{i}", 8, 1)
    assert len(await lessons.score_due(mock_db, AGENT, NOW)) == 1
    assert len(await lessons.score_due(mock_db, AGENT, NOW)) == 1


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


async def _meta_ad(db, ad_id, name, created=datetime(2026, 10, 21, 9), campaign="c"):
    await db["meta_ad_state"].insert_one({"client_group_id": "dest", "ad_id": ad_id, "name": name, "campaign_id": campaign,
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
    only_sometimes = {"flagged", "unscored_reason", "cleared_by", "cleared_at", "cleared_from"}   # flagged Adds, and rows that can't be judged
    assert [k for k in api.OUTCOME_FIELDS if k not in row and k not in only_sometimes] == []
    assert {"ad_id", "baseline_cpl", "live_since"} <= set(api.OUTCOME_FIELDS)


# ---------------------------------------------------------------------------
# Account timezones, campaign scope, the tick budget
# ---------------------------------------------------------------------------

async def test_a_behind_account_isnt_due_while_its_last_day_is_still_running(mock_db):
    await _dest(mock_db, tz="America/New_York", synced=datetime(2026, 10, 26, 4))
    await _row(mock_db, live=datetime(2026, 10, 20, 3))          # 23:00 on the 19th in New York
    await _week(mock_db, "ad1", 8, 1, first=19)
    await _day(mock_db, "ad1", "2026-10-26", 100, 0)             # the day after the window: never counted
    still_25th = datetime(2026, 10, 26, 3, tzinfo=timezone.utc)  # 23:00 on the 25th there; UTC calls it the 26th
    assert await lessons.score_due(mock_db, AGENT, still_25th) == []
    [done] = await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 26, 5, tzinfo=timezone.utc))
    assert done["spend"] == 56.0 and done["result"] == "won"


async def test_an_ahead_account_is_due_when_its_own_window_has_ended(mock_db):
    await _dest(mock_db, tz="Pacific/Auckland", synced=datetime(2026, 10, 26, 12))
    await _row(mock_db, live=datetime(2026, 10, 20, 3))          # the 20th, 16:00 in Auckland
    await _week(mock_db, "ad1", 8, 1)
    assert await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 26, 10, tzinfo=timezone.utc)) == []   # 23:00 on the 26th there
    [done] = await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 26, 12, tzinfo=timezone.utc))      # 01:00 on the 27th
    assert done["spend"] == 56.0


async def test_flagged_row_live_since_is_the_real_instant_of_the_accounts_first_day(mock_db):
    await _dest(mock_db, tz="America/New_York")
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    await _day(mock_db, "m1", "2026-10-22", 5, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 1
    row = await mock_db[OUTCOMES].find_one({"_id": "fc1:l1"})
    assert row["live_since"] == datetime(2026, 10, 22, 4)        # midnight on the 22nd in New York (EDT)


async def test_a_flagged_row_in_a_behind_account_gets_its_whole_window(mock_db):
    await _dest(mock_db, tz="America/New_York", synced=datetime(2026, 10, 31, 12))
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "m1", "BS - Timelapse - Win")
    for d in range(22, 29):
        await _day(mock_db, "m1", f"2026-10-{d}", 8, 1)
    await scoreboard.record_flagged_adds(mock_db, AGENT, datetime(2026, 10, 30, tzinfo=timezone.utc))
    [done] = await lessons.score_due(mock_db, AGENT, datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert done["spend"] == 56.0


async def test_a_sibling_clients_same_named_ad_on_a_shared_account_isnt_matched(mock_db):
    await _dest(mock_db, campaigns=["c1"])
    await _flagged_card(mock_db)
    await _meta_ad(mock_db, "sib", "BS - Timelapse - Win", campaign="c2")      # another client's campaign
    await _day(mock_db, "sib", "2026-10-22", 5, 1, campaign="c2")
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0
    await _meta_ad(mock_db, "mine", "BS - Timelapse - Win", campaign="c1")
    await _day(mock_db, "mine", "2026-10-23", 5, 1, campaign="c1")
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 1
    assert (await mock_db[OUTCOMES].find_one({"_id": "fc1:l1"}))["ad_id"] == "mine"


async def test_an_ad_made_just_before_the_card_isnt_matched(mock_db):
    await _flagged_card(mock_db, created=datetime(2026, 10, 19, 9))
    await _meta_ad(mock_db, "early", "BS - Timelapse - Win", created=datetime(2026, 10, 19, 8))
    await _day(mock_db, "early", "2026-10-20", 5, 1)
    assert await scoreboard.record_flagged_adds(mock_db, AGENT, NOW) == 0


async def test_catch_up_uses_the_smaller_of_the_tick_deadline_and_sixty_seconds(mock_db, mock_mongo_client, monkeypatch):
    seen = []

    async def spy(db, agent, now, *, deadline=None):
        seen.append(deadline)
        return 0

    monkeypatch.setattr(scoreboard, "record_flagged_adds", spy)
    run = {"_id": "r1", "agent_id": "ag1", "as_of": NOW.replace(tzinfo=None), "tick_deadline": time.monotonic() + 170}
    await runner.catch_up_run(mock_db, mock_mongo_client, run, AGENT)
    assert seen[0] <= time.monotonic() + runner.CATCH_UP_MAX_S + 1
    near = time.monotonic() + 5
    await runner.catch_up_run(mock_db, mock_mongo_client, {**run, "tick_deadline": near}, AGENT)
    assert seen[1] == near


async def test_the_tick_deadline_is_neither_saved_nor_returned(mock_db, mock_mongo_client, monkeypatch):
    import routers.agent_stock as api

    seen = []

    async def spy(db, mongo_client, run, agent):
        seen.append(run.get("tick_deadline"))

    monkeypatch.setattr(runner, "catch_up_run", spy)
    run = {"_id": "r9", "agent_id": "ag1", "user_id": "u", "kind": "batch", "phase": "catch_up", "as_of": NOW.replace(tzinfo=None),
           "local_date": "2026-10-28", "clients": {}, "claimed_at": None}
    await mock_db[runner.RUNS].insert_one(dict(run))
    deadline = time.monotonic() + 100
    out = await runner.advance_run(mock_db, mock_mongo_client, dict(run), AGENT, deadline=deadline)
    assert seen == [deadline] and "tick_deadline" not in out
    assert "tick_deadline" not in await mock_db[runner.RUNS].find_one({"_id": "r9"})
    assert "tick_deadline" not in api._run_to_api({**run, "tick_deadline": 1.0})


async def test_clearing_a_row_takes_it_out_of_the_record(mock_db):
    rid = await _row(mock_db)
    await mock_db[OUTCOMES].update_one({"_id": rid}, {"$set": {"result": "lost"}})
    assert (await record_for(mock_db, "ag1", "k1"))["lost"] == 1
    done = await scoreboard.clear_row(mock_db, "ag1", rid, by="sam", now=NOW)
    assert done["result"] == "cleared" and done["cleared_by"] == "sam" and done["cleared_from"] == "lost" and done["cleared_at"]
    assert await record_for(mock_db, "ag1", "k1") == {"won": 0, "lost": 0, "moves": 0}
    assert await scoreboard.clear_row(mock_db, "ag1", rid, by="sam", now=NOW) is None      # once
    assert await scoreboard.clear_row(mock_db, "other", rid, by="sam", now=NOW) is None


async def test_only_won_or_lost_rows_can_be_cleared(mock_db):
    for i, result in enumerate((None, "undone", "unscored", "never_went_live", "no_clear_change")):
        rid = await _row(mock_db, card=f"x{i}", line="l")
        await mock_db[OUTCOMES].update_one({"_id": rid}, {"$set": {"result": result}})
        assert await scoreboard.clear_row(mock_db, "ag1", rid, by="sam", now=NOW) is None
        assert (await mock_db[OUTCOMES].find_one({"_id": rid}))["result"] == result
    rid = await _row(mock_db, card="w")
    await mock_db[OUTCOMES].update_one({"_id": rid}, {"$set": {"result": "won"}})
    assert (await scoreboard.clear_row(mock_db, "ag1", rid, by="sam", now=NOW))["cleared_from"] == "won"
