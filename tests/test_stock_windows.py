from datetime import date, datetime, timezone

from ai.agents.stock.windows import full_day_window, last_full_day


def test_window_is_7_full_days_ending_yesterday_in_account_tz():
    w = full_day_window(datetime(2026, 11, 3, 7, tzinfo=timezone.utc), "Europe/London")
    assert (w.start, w.end, w.prev_start, w.prev_end) == (
        date(2026, 10, 27), date(2026, 11, 2), date(2026, 10, 20), date(2026, 10, 26))


def test_window_length_follows_days():
    w = full_day_window(datetime(2026, 11, 3, 7, tzinfo=timezone.utc), "Europe/London", days=3)
    assert (w.start, w.end, w.prev_start, w.prev_end) == (
        date(2026, 10, 31), date(2026, 11, 2), date(2026, 10, 28), date(2026, 10, 30))


def test_account_behind_agency_excludes_its_day_in_progress():
    now = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)  # still Nov 2 in Los Angeles
    assert last_full_day(now, "America/Los_Angeles") == date(2026, 11, 1)


def test_account_ahead_of_agency_has_a_later_last_full_day():
    now = datetime(2026, 11, 3, 20, tzinfo=timezone.utc)  # already Nov 4 in Sydney
    assert last_full_day(now, "Australia/Sydney") == date(2026, 11, 3)


def test_unknown_timezone_falls_back_to_utc():
    assert last_full_day(datetime(2026, 11, 3, 7, tzinfo=timezone.utc), "Nope/Nowhere") == date(2026, 11, 2)
