"""
tests/test_ghl_daily_leads.py
------------------------------
Account-level daily new-lead counts (see services/ghl_daily_leads.py, the GHL
sibling of meta_daily_spend). Pins the bucketing and the same
don't-blank-a-good-cache guard the Meta version uses.
"""

import pytest

from services.ghl_daily_leads import cache_ghl_daily_leads, compute_daily_leads


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


class FakeContacts:
    def __init__(self, docs):
        self._docs = docs

    def find(self, _filt, _proj=None):
        return FakeCursor(self._docs)


class FakeGroups:
    def __init__(self, existing=None):
        self.updates = []
        self._existing = existing

    async def find_one(self, _filt, _proj=None):
        return self._existing

    async def update_one(self, filt, update):
        self.updates.append((filt, update))


class FakeDb:
    def __init__(self, contacts, groups):
        self._collections = {"ghl_contacts": contacts, "client_groups": groups}

    def __getitem__(self, name):
        return self._collections[name]


class FakeMongo:
    def __init__(self, contacts, groups):
        self._db = FakeDb(contacts, groups)

    def __getitem__(self, _name):
        return self._db


EMPTY_ROW = {"new_leads": 0, "new_contacts": 0, "open": 0, "won": 0, "lost": 0, "abandoned": 0}


@pytest.mark.asyncio
async def test_buckets_contacts_by_day():
    contacts = FakeContacts([
        {"contact_data": {"dateAdded": "2026-08-16T09:00:00.000Z"}},
        {"contact_data": {"dateAdded": "2026-08-16T18:30:00.000Z"}},
        {"contact_data": {"dateAdded": "2026-08-14T12:00:00.000Z"}},
    ])

    rows = await compute_daily_leads("u1", "loc1", FakeMongo(contacts, FakeGroups()))

    assert rows == [
        {"date": "2026-08-14", "leads": 1, **EMPTY_ROW, "new_contacts": 1},
        {"date": "2026-08-16", "leads": 2, **EMPTY_ROW, "new_contacts": 2},
    ]


@pytest.mark.asyncio
async def test_contacts_without_a_date_are_skipped():
    contacts = FakeContacts([
        {"contact_data": {}},
        {"contact_data": {"dateAdded": "2026-08-16T09:00:00.000Z"}},
    ])

    rows = await compute_daily_leads("u1", "loc1", FakeMongo(contacts, FakeGroups()))

    assert rows == [{"date": "2026-08-16", "leads": 1, **EMPTY_ROW, "new_contacts": 1}]


@pytest.mark.asyncio
async def test_splits_leads_from_contacts_and_snapshots_opportunity_status():
    contacts = FakeContacts([
        # A lead with an open opportunity.
        {
            "lead_type": "lead",
            "contact_data": {
                "dateAdded": "2026-08-16T09:00:00.000Z",
                "opportunities": [{"status": "open"}],
            },
        },
        # A lead with two opportunities — each counts on its own status,
        # unwound just like get_unified_leads' opp_pipeline.
        {
            "lead_type": "lead",
            "contact_data": {
                "dateAdded": "2026-08-16T10:00:00.000Z",
                "opportunities": [{"status": "Won"}, {"status": "lost"}],
            },
        },
        # A lead with no opportunity yet — counted as a lead, no status bucket.
        {
            "lead_type": "lead",
            "contact_data": {"dateAdded": "2026-08-16T11:00:00.000Z"},
        },
        # A plain contact — never counted toward open/won/lost/abandoned.
        {
            "lead_type": "contact",
            "contact_data": {"dateAdded": "2026-08-16T12:00:00.000Z"},
        },
    ])

    rows = await compute_daily_leads("u1", "loc1", FakeMongo(contacts, FakeGroups()))

    assert rows == [{
        "date": "2026-08-16",
        "leads": 4,
        "new_leads": 3,
        "new_contacts": 1,
        "open": 1,
        "won": 1,
        "lost": 1,
        "abandoned": 0,
    }]


@pytest.mark.asyncio
async def test_a_successful_count_replaces_the_cache():
    contacts = FakeContacts([{"contact_data": {"dateAdded": "2026-08-16T09:00:00.000Z"}}])
    groups = FakeGroups()

    written = await cache_ghl_daily_leads("g1", "u1", "loc1", FakeMongo(contacts, groups))

    assert written == 1
    _filt, update = groups.updates[0]
    assert update["$set"]["ghl_daily_leads"] == [
        {"date": "2026-08-16", "leads": 1, **EMPTY_ROW, "new_contacts": 1}
    ]


@pytest.mark.asyncio
async def test_an_empty_count_does_not_blank_an_existing_cache():
    """Mirrors cache_ghl_opp_stats_all_presets: an empty read while a
    non-empty cache exists reads as a transient problem, not a real zero."""
    contacts = FakeContacts([])
    groups = FakeGroups(existing={"ghl_daily_leads": [{"date": "2026-08-16", "leads": 3}]})

    written = await cache_ghl_daily_leads("g1", "u1", "loc1", FakeMongo(contacts, groups))

    assert written == 0
    assert groups.updates == []


@pytest.mark.asyncio
async def test_a_genuine_zero_is_written_when_there_is_no_existing_cache():
    contacts = FakeContacts([])
    groups = FakeGroups(existing=None)

    written = await cache_ghl_daily_leads("g1", "u1", "loc1", FakeMongo(contacts, groups))

    assert written == 0
    _filt, update = groups.updates[0]
    assert update["$set"]["ghl_daily_leads"] == []
