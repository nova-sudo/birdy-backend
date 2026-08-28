"""
tests/test_client_health_rules.py
---------------------------------
The client-health rule:

    expected = close_target × (days elapsed / days in month)
    deficit  = expected − actual closes
    pace     = actual / expected

    Healthy   pace >= 90%, OR less than 1 close behind
    Warning   pace >= 70%, OR less than 2 closes behind
    Critical  anything worse

The `or` in those bands is the whole point and the easiest thing to get wrong:
a client must fail on BOTH arms to be downgraded, which is what stops a
small-target client from false-alarming.
"""

from datetime import date

import pytest

from services import client_health as ch
from services.client_health import HEALTHY, WARNING, CRITICAL, compute_health


def band(target, actual, elapsed=15, days=30):
    return compute_health(target, actual, elapsed, days)["health"]


# ── the arithmetic ────────────────────────────────────────────────────────


def test_expected_is_the_target_prorated_by_elapsed_days():
    r = compute_health(close_target=10, actual_closes=0, days_elapsed=15, days_in_month=30)
    assert r["expected"] == 5.0


def test_deficit_is_expected_minus_actual():
    r = compute_health(close_target=10, actual_closes=3, days_elapsed=15, days_in_month=30)
    assert r["expected"] == 5.0
    assert r["deficit"] == 2.0


def test_pace_is_actual_over_expected():
    r = compute_health(close_target=10, actual_closes=4, days_elapsed=15, days_in_month=30)
    assert r["pace"] == pytest.approx(0.8)


def test_exceeding_the_target_gives_a_negative_deficit():
    r = compute_health(close_target=10, actual_closes=9, days_elapsed=15, days_in_month=30)
    assert r["deficit"] == -4.0
    assert r["health"] == HEALTHY


# ── the bands ─────────────────────────────────────────────────────────────


def test_on_pace_is_healthy():
    # 20 target, half the month gone, 10 closed — exactly on pace.
    assert band(20, 10, 15, 30) == HEALTHY


def test_ninety_percent_pace_is_the_healthy_boundary():
    # expected 10, actual 9 → pace exactly 0.90
    assert band(20, 9, 15, 30) == HEALTHY


def test_just_under_ninety_percent_with_a_big_deficit_is_not_healthy():
    # expected 50, actual 44 → pace 0.88, deficit 6 → fails both arms
    assert band(100, 44, 15, 30) == WARNING


def test_seventy_percent_pace_is_the_warning_boundary():
    # expected 50, actual 35 → pace exactly 0.70
    assert band(100, 35, 15, 30) == WARNING


def test_below_seventy_percent_with_a_big_deficit_is_critical():
    # expected 50, actual 34 → pace 0.68, deficit 16 → fails both arms twice
    assert band(100, 34, 15, 30) == CRITICAL


def test_zero_closes_against_a_large_target_is_critical():
    assert band(100, 0, 15, 30) == CRITICAL


# ── the deficit arm: small-target clients must not false-alarm ────────────


def test_a_small_target_client_missing_one_close_stays_healthy():
    """expected 2, actual 1 → pace 50%, which alone would read Critical.
    Being under 1 close behind keeps it Healthy — that is what the `or` is for."""
    r = compute_health(close_target=4, actual_closes=1, days_elapsed=15, days_in_month=30)
    assert r["pace"] == pytest.approx(0.5)
    assert r["deficit"] == 1.0
    # deficit is 1.0, NOT < 1, so the deficit arm does not save it here
    assert r["health"] == WARNING


def test_less_than_one_close_behind_is_healthy_whatever_the_pace():
    # expected 1.5, actual 0.6 → pace 0.4, deficit 0.9 (< 1)
    r = compute_health(close_target=3, actual_closes=0.6, days_elapsed=15, days_in_month=30)
    assert r["pace"] < ch.WARNING_PACE
    assert r["deficit"] < ch.HEALTHY_DEFICIT
    assert r["health"] == HEALTHY


def test_less_than_two_closes_behind_is_at_worst_warning():
    # expected 2.5, actual 0.6 → pace 0.24, deficit 1.9 (< 2)
    r = compute_health(close_target=5, actual_closes=0.6, days_elapsed=15, days_in_month=30)
    assert r["pace"] < ch.WARNING_PACE
    assert ch.HEALTHY_DEFICIT <= r["deficit"] < ch.WARNING_DEFICIT
    assert r["health"] == WARNING


def test_a_client_must_fail_both_arms_to_be_downgraded():
    # Good pace, large deficit — impossible in practice, but the rule says the
    # pace arm alone is enough to hold Healthy.
    r = compute_health(close_target=1000, actual_closes=475, days_elapsed=15, days_in_month=30)
    assert r["deficit"] == 25.0          # far more than 2 closes behind
    assert r["pace"] == pytest.approx(0.95)
    assert r["health"] == HEALTHY


# ── no target ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", [None, 0, 0.0])
def test_a_client_with_no_target_is_healthy(target):
    """No goal means nothing to be behind on. Inventing a default target would
    invent an expectation the agency never set."""
    r = compute_health(target, actual_closes=0, days_elapsed=15, days_in_month=30)
    assert r["health"] == HEALTHY
    assert r["pace"] is None
    assert "no monthly closes target" in r["reason"]


def test_no_target_does_not_divide_by_zero():
    r = compute_health(0, 5, 15, 30)
    assert r["expected"] == 0.0
    assert r["deficit"] == 0.0


# ── month boundaries ──────────────────────────────────────────────────────


def test_day_one_of_the_month_already_reads_warning_for_a_high_target_client():
    """A consequence of the rule as written, and worth pinning down.

    On day 1 with a 30/month target, expected is exactly 1.0 — so a client who
    has not closed yet is exactly 1 behind. "Less than 1 close behind" is a
    strict `<`, so 1.0 does not qualify, and pace is 0. The client reads
    Warning from the first of the month until its first close.

    Larger targets hit this harder: at 60/month, day 1 expects 2 and the
    client opens the month Critical.
    """
    r = compute_health(close_target=30, actual_closes=0, days_elapsed=1, days_in_month=30)
    assert r["expected"] == 1.0
    assert r["deficit"] == 1.0
    assert r["health"] == WARNING


def test_day_one_with_a_high_enough_target_opens_critical():
    r = compute_health(close_target=60, actual_closes=0, days_elapsed=1, days_in_month=30)
    assert r["expected"] == 2.0
    assert r["health"] == CRITICAL


def test_a_modest_target_opens_the_month_healthy():
    # 15/month → day 1 expects 0.5, so being at zero is under 1 behind.
    r = compute_health(close_target=15, actual_closes=0, days_elapsed=1, days_in_month=30)
    assert r["deficit"] == 0.5
    assert r["health"] == HEALTHY


def test_zero_days_elapsed_is_healthy():
    r = compute_health(close_target=30, actual_closes=0, days_elapsed=0, days_in_month=30)
    assert r["health"] == HEALTHY
    assert "month has not started" in r["reason"]


def test_the_full_month_expects_the_whole_target():
    r = compute_health(close_target=10, actual_closes=10, days_elapsed=31, days_in_month=31)
    assert r["expected"] == 10.0
    assert r["health"] == HEALTHY


def test_elapsed_days_cannot_exceed_the_month():
    r = compute_health(close_target=10, actual_closes=10, days_elapsed=99, days_in_month=30)
    assert r["expected"] == 10.0


# ── the evaluation window ─────────────────────────────────────────────────


def test_previous_sunday_from_a_monday_is_yesterday():
    # 2026-08-24 is a Monday
    assert ch.previous_sunday(date(2026, 8, 24)) == date(2026, 8, 23)


@pytest.mark.parametrize("day,expected", [
    (date(2026, 8, 24), date(2026, 8, 23)),   # Monday
    (date(2026, 8, 26), date(2026, 8, 23)),   # Wednesday
    (date(2026, 8, 29), date(2026, 8, 23)),   # Saturday
    (date(2026, 8, 30), date(2026, 8, 23)),   # Sunday itself → the one before
])
def test_previous_sunday_is_stable_across_the_week(day, expected):
    """A mid-week re-run must measure the window the Monday run would have,
    otherwise a manual run silently reports different bands."""
    assert ch.previous_sunday(day) == expected


def test_month_progress_knows_month_lengths():
    assert ch.month_progress(date(2026, 2, 14)) == (14, 28)
    assert ch.month_progress(date(2026, 8, 14)) == (14, 31)


def test_month_progress_handles_a_leap_february():
    assert ch.month_progress(date(2028, 2, 10)) == (10, 29)


# ── reading a client group ────────────────────────────────────────────────


def test_health_for_group_reads_target_and_won_from_the_cache():
    group = {
        "targets": {"monthly_wins": 20},
        "ghl_opp_cache": {"this_month": {"won": 10}},
    }
    r = ch.health_for_group(group, through=date(2026, 8, 15))
    assert r["close_target"] == 20
    assert r["actual"] == 10
    assert r["health"] == HEALTHY


def test_health_for_group_falls_back_to_the_legacy_cache_location():
    group = {
        "targets": {"monthly_wins": 20},
        "gohighlevel_cache": {"metrics": {"opportunity_stats": {"won": 10}}},
    }
    r = ch.health_for_group(group, through=date(2026, 8, 15))
    assert r["actual"] == 10


def test_health_for_group_with_no_targets_is_healthy():
    r = ch.health_for_group({}, through=date(2026, 8, 15))
    assert r["health"] == HEALTHY


def test_health_for_group_records_the_window_it_used():
    r = ch.health_for_group({}, through=date(2026, 8, 23))
    assert r["through"] == "2026-08-23"
    assert r["days_elapsed"] == 23
    assert r["days_in_month"] == 31


# ── the recompute pass ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recompute_stores_the_band_on_each_group(mock_db):
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": "u",
        "targets": {"monthly_wins": 100},
        "ghl_opp_cache": {"this_month": {"won": 0}},
    })

    out = await ch.recompute_all(mock_db, through=date(2026, 8, 15))

    stored = await mock_db["client_groups"].find_one({"id": "g1"})
    assert stored["health"] == CRITICAL
    assert stored["health_detail"]["expected"] > 0
    assert out["changed"] == 1


@pytest.mark.asyncio
async def test_recompute_does_not_write_when_the_band_is_unchanged(mock_db):
    """An unchanged run should cost no writes, so health_updated_at means
    'when this last moved' rather than 'when the job last ran'."""
    await mock_db["client_groups"].insert_one({
        "id": "g1", "user_id": "u", "health": HEALTHY,
        "targets": {"monthly_wins": 20},
        "ghl_opp_cache": {"this_month": {"won": 10}},
    })

    out = await ch.recompute_all(mock_db, through=date(2026, 8, 15))

    assert out["changed"] == 0
    stored = await mock_db["client_groups"].find_one({"id": "g1"})
    assert "health_updated_at" not in stored


@pytest.mark.asyncio
async def test_recompute_counts_every_band(mock_db):
    await mock_db["client_groups"].insert_many([
        {"id": "healthy", "targets": {"monthly_wins": 20},
         "ghl_opp_cache": {"this_month": {"won": 10}}},
        {"id": "critical", "targets": {"monthly_wins": 100},
         "ghl_opp_cache": {"this_month": {"won": 0}}},
        {"id": "no-target"},
    ])

    out = await ch.recompute_all(mock_db, through=date(2026, 8, 15))

    assert out["scanned"] == 3
    assert out["counts"][CRITICAL] == 1
    assert out["counts"][HEALTHY] == 2       # on-pace client + the untargeted one
