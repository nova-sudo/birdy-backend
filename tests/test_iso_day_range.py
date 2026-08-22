"""
tests/test_iso_day_range.py
---------------------------
Date bounds for string-ISO timestamp fields.

Two bugs met here. A bare YYYY-MM-DD used as `$lte` against a full timestamp
excluded the whole final day — 63 leads, 4.1%, on a 7-day window. And malformed
input was handled three different ways across the same routers: logged and
ignored (which silently WIDENED the window and returned more rows than were
asked for), passed straight through (which mis-filtered), or rejected.

These pin one behaviour: widen the upper bound to end-of-day, and refuse
anything that is not a date rather than guessing.
"""

import pytest

from core.utils import iso_day_end, iso_day_range, iso_day_start


class TestBounds:
    def test_the_upper_bound_covers_the_whole_final_day(self):
        """The original bug: "...T19:38:41+0000" <= "2026-08-22" is false."""
        assert iso_day_end("2026-08-22") == "2026-08-22T23:59:59.999Z"
        assert "2026-08-22T19:38:41+0000" <= iso_day_end("2026-08-22")

    def test_the_lower_bound_starts_at_midnight(self):
        assert iso_day_start("2026-08-22") == "2026-08-22T00:00:00.000Z"
        assert "2026-08-22T00:00:01+0000" >= iso_day_start("2026-08-22")

    @pytest.mark.parametrize("bad", [
        "2026-08-22T10:14:00+0000",   # already a timestamp — would be corrupted
        "2026-8-2",                   # unpadded
        "22-08-2026",                 # wrong order
        "yesterday",
        "2026-08-22 ",                # stray whitespace
    ])
    def test_malformed_input_raises(self, bad):
        with pytest.raises(ValueError):
            iso_day_end(bad)
        with pytest.raises(ValueError):
            iso_day_start(bad)


class TestRange:
    def test_both_bounds(self):
        assert iso_day_range("2026-08-01", "2026-08-22") == {
            "$gte": "2026-08-01T00:00:00.000Z",
            "$lte": "2026-08-22T23:59:59.999Z",
        }

    def test_either_bound_alone(self):
        assert iso_day_range("2026-08-01", None) == {"$gte": "2026-08-01T00:00:00.000Z"}
        assert iso_day_range(None, "2026-08-22") == {"$lte": "2026-08-22T23:59:59.999Z"}

    @pytest.mark.parametrize("empty", [None, "", "null", "undefined", "None"])
    def test_absent_bounds_are_dropped_not_rejected(self, empty):
        """The frontend sends stringified nulls for an unset date picker. That
        is absence, not malformed input — it must not 400."""
        assert iso_day_range(empty, empty) == {}

    def test_no_bounds_is_an_empty_filter(self):
        """Callers branch on truthiness to decide whether to filter at all."""
        assert iso_day_range(None, None) == {}

    def test_a_malformed_bound_is_reported_by_name(self):
        """The old code logged and dropped it, widening the window silently."""
        with pytest.raises(ValueError, match="start_date"):
            iso_day_range("nonsense", "2026-08-22")
        with pytest.raises(ValueError, match="end_date"):
            iso_day_range("2026-08-01", "nonsense")

    def test_one_good_bound_does_not_rescue_a_bad_one(self):
        """Partial application would be the log-and-ignore bug again."""
        with pytest.raises(ValueError):
            iso_day_range("2026-08-01", "2026-13-99")
