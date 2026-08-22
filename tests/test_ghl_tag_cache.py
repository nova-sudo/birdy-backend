"""
tests/test_ghl_tag_cache.py
---------------------------
Windowed tag counts, precomputed.

The Clients page renders one column per GHL tag over the selected window. That
was computed live by a `$unwind` inside a `$facet`, which forced a FETCH of
every contact in the window: 180,106 documents examined — the whole collection —
and 1,530 ms, on every page load.

The trap this module exists to avoid: `gohighlevel_cache.metrics.tag_breakdown`
looks like the same number and is not. It is a lifetime counter accumulated
across syncs. Verified on production, one group: lifetime 96 tags / 28,726,
windowed 47 tags / 2,654.
"""

import pytest

from services.ghl_tag_cache import _rollup


BY_DAY = {
    "2026-08-01": {"lead": 5, "zombie": 2},
    "2026-08-15": {"lead": 3, "vip": 1},
    "2026-08-22": {"lead": 4},
    "2026-06-30": {"lead": 100, "old": 9},
}


class TestRollup:
    def test_a_window_sums_only_the_days_inside_it(self):
        assert _rollup(BY_DAY, "2026-08-01", "2026-08-22") == {"lead": 12, "zombie": 2, "vip": 1}

    def test_bounds_are_inclusive_on_both_ends(self):
        """Off-by-one here is the same bug class as the $lte boundary fix:
        an exclusive end silently drops the final day."""
        assert _rollup(BY_DAY, "2026-08-15", "2026-08-15") == {"lead": 3, "vip": 1}
        assert _rollup(BY_DAY, "2026-08-01", "2026-08-01") == {"lead": 5, "zombie": 2}

    def test_none_bounds_mean_all_time(self):
        """ghl_date_bounds returns (None, None) for the `maximum` preset."""
        assert _rollup(BY_DAY, None, None) == {"lead": 112, "old": 9, "zombie": 2, "vip": 1}

    def test_one_open_bound_is_honoured(self):
        assert _rollup(BY_DAY, "2026-08-01", None) == {"lead": 12, "zombie": 2, "vip": 1}
        assert _rollup(BY_DAY, None, "2026-06-30") == {"lead": 100, "old": 9}

    def test_a_window_with_no_days_is_empty_not_missing(self):
        """An empty dict is a real answer — 'no tags in this window'. The read
        path renders it as empty columns."""
        assert _rollup(BY_DAY, "2026-01-01", "2026-01-31") == {}

    def test_results_are_ordered_heaviest_first(self):
        assert list(_rollup(BY_DAY, None, None)) == ["lead", "old", "zombie", "vip"]

    def test_tags_absent_from_a_window_are_omitted_not_zeroed(self):
        """A zero column and a missing column render the same, but carrying
        every historical tag into every window would bloat all 13 buckets."""
        assert "old" not in _rollup(BY_DAY, "2026-08-01", "2026-08-22")

    def test_no_tag_data_at_all(self):
        assert _rollup({}, "2026-08-01", "2026-08-22") == {}
