"""
tests/test_ghl_refresh_plan.py
------------------------------
The hourly ghl-tick used to FULL LOAD every location on every run — the
only way an edited contact reached Birdy — and that re-upserted every contact
in the cluster each hour. The plan below replaces it: incremental passes
bounded by the previous run, with a FULL LOAD once a day for the prune.
"""

from datetime import datetime, timedelta

from routers.cron import (
    GHL_FULL_RELOAD_HOURS,
    GHL_INCREMENTAL_OVERLAP_MINUTES,
    ghl_refresh_plan,
)

NOW = datetime(2026, 10, 12, 18, 0, 0)


def test_never_refreshed_group_gets_a_full_load():
    assert ghl_refresh_plan({}, NOW) == (True, None)


def test_group_without_a_recorded_full_load_gets_one():
    # Groups from before the stamp existed: they have last_ghl_refresh only.
    group = {"last_ghl_refresh": NOW - timedelta(hours=1)}
    assert ghl_refresh_plan(group, NOW) == (True, None)


def test_recent_full_load_means_incremental_from_last_run_minus_overlap():
    last = NOW - timedelta(hours=1)
    group = {"last_ghl_refresh": last, "last_ghl_full_refresh": NOW - timedelta(hours=5)}
    full, since = ghl_refresh_plan(group, NOW)
    assert full is False
    assert since == last - timedelta(minutes=GHL_INCREMENTAL_OVERLAP_MINUTES)


def test_full_load_is_due_again_after_the_reload_interval():
    group = {
        "last_ghl_refresh": NOW - timedelta(hours=1),
        "last_ghl_full_refresh": NOW - timedelta(hours=GHL_FULL_RELOAD_HOURS),
    }
    assert ghl_refresh_plan(group, NOW) == (True, None)


def test_full_load_not_due_one_minute_before_the_interval():
    group = {
        "last_ghl_refresh": NOW - timedelta(hours=1),
        "last_ghl_full_refresh": NOW - timedelta(hours=GHL_FULL_RELOAD_HOURS) + timedelta(minutes=1),
    }
    full, since = ghl_refresh_plan(group, NOW)
    assert full is False and since is not None
