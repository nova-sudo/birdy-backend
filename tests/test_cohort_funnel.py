"""
tests/test_cohort_funnel.py
---------------------------
compute_cohort_funnel — the dashboard's close-rate funnel.

The point of these is the invariant the old funnel could not hold: every stage
describes the same cohort, so closes/leads is a rate you can act on. The stages
are counted from one pass over ghl_contacts, so the fixtures here are contact
documents in the shape services/ghl_service.py projects them.
"""

import pytest

from integrations.gohighlevel import compute_cohort_funnel


def contact(added, opps=None, keys=None):
    """A ghl_contacts doc as the funnel cache projects it."""
    return {
        "contact_data": {"dateAdded": added, "opportunities": opps or []},
        "match_keys": keys or [],
    }


WINDOW = ("2026-07-01", "2026-07-31")


def test_counts_only_contacts_created_in_the_window():
    contacts = [
        contact("2026-07-10T09:00:00Z"),
        contact("2026-07-31T23:00:00Z"),
        contact("2026-06-30T23:00:00Z"),  # day before
        contact("2026-08-01T00:00:00Z"),  # day after
    ]

    stats = compute_cohort_funnel(contacts, set(), *WINDOW)

    assert stats["leads"] == 2


def test_closes_are_a_subset_of_in_crm_which_is_a_subset_of_leads():
    contacts = [
        contact("2026-07-02T09:00:00Z", [{"status": "won"}]),
        contact("2026-07-03T09:00:00Z", [{"status": "open"}]),
        contact("2026-07-04T09:00:00Z"),  # never made it to an opportunity
    ]

    stats = compute_cohort_funnel(contacts, set(), *WINDOW)

    assert stats["leads"] == 3
    assert stats["in_crm"] == 2
    assert stats["closes"] == 1
    assert stats["closes"] <= stats["in_crm"] <= stats["leads"]


def test_a_win_recorded_after_the_window_still_counts_for_its_cohort():
    """
    The whole reason for cohort semantics. This lead arrived in July and was
    won in September; the July funnel must claim the close, because the
    question is what July's leads went on to do.
    """
    contacts = [contact("2026-07-05T09:00:00Z", [{"status": "won"}])]

    stats = compute_cohort_funnel(contacts, set(), *WINDOW)

    assert stats["closes"] == 1


def test_a_contact_is_counted_once_however_many_opportunities_it_has():
    contacts = [
        contact("2026-07-05T09:00:00Z", [{"status": "won"}, {"status": "won"}, {"status": "lost"}])
    ]

    stats = compute_cohort_funnel(contacts, set(), *WINDOW)

    assert stats["leads"] == 1
    assert stats["in_crm"] == 1
    assert stats["closes"] == 1


def test_called_comes_from_the_hotprospector_match_keys():
    contacts = [
        contact("2026-07-05T09:00:00Z", keys=["p:447700900001"]),
        contact("2026-07-06T09:00:00Z", keys=["p:447700900002"]),
        contact("2026-07-07T09:00:00Z", keys=[]),
    ]

    stats = compute_cohort_funnel(contacts, {"p:447700900001"}, *WINDOW)

    assert stats["called"] == 1


def test_called_ignores_dialler_activity_outside_the_cohort():
    """
    The bug this replaced: leads_with_calls counted everyone in the dialler,
    so Called could exceed the window's entire lead count. A called key that
    belongs to no in-window contact must not move the stage.
    """
    contacts = [contact("2026-06-01T09:00:00Z", keys=["p:447700900009"])]

    stats = compute_cohort_funnel(contacts, {"p:447700900009"}, *WINDOW)

    assert stats["leads"] == 0
    assert stats["called"] == 0


def test_won_revenue_sums_only_the_cohort_wins():
    contacts = [
        contact("2026-07-05T09:00:00Z", [{"status": "won", "monetaryValue": 1200}]),
        contact("2026-06-05T09:00:00Z", [{"status": "won", "monetaryValue": 9999}]),
    ]

    stats = compute_cohort_funnel(contacts, set(), *WINDOW)

    assert stats["won_revenue"] == 1200.0


def test_maximum_preset_counts_every_contact():
    contacts = [
        contact("2020-01-01T09:00:00Z", [{"status": "won"}]),
        contact("2026-07-05T09:00:00Z"),
    ]

    stats = compute_cohort_funnel(contacts, set(), None, None)

    assert stats["leads"] == 2
    assert stats["closes"] == 1


def test_a_contact_with_no_date_is_not_counted():
    """A contact with no dateAdded belongs to no cohort — it must not inflate
    the lifetime stage, where every real date passes the window test."""
    stats = compute_cohort_funnel([contact("")], set(), None, None)

    assert stats["leads"] == 0


@pytest.mark.parametrize("status", ["Won", "WON", "won"])
def test_won_status_matching_is_case_insensitive(status):
    contacts = [contact("2026-07-05T09:00:00Z", [{"status": status}])]

    assert compute_cohort_funnel(contacts, set(), *WINDOW)["closes"] == 1
