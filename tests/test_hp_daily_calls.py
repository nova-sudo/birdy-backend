"""
tests/test_hp_daily_calls.py
------------------------------
Daily call series for the Sales-Hub trend chart (see hp_service.py's
_compute_daily_call_series). Pins the bucketing and the lifetime-cohort
tradeoff "called" makes — each lead counted once, on their first-ever call.
"""

import pytest

from services.hp_service import cache_hp_daily_calls_from_stored, _compute_daily_call_series


def call(iso, status="outbound", duration=60, lead_id="l1"):
    return {"call_time_iso": iso, "call_status": status, "duration": duration, "lead_id": lead_id}


def test_buckets_calls_by_day():
    calls = [
        call("2026-07-01T09:00:00", lead_id="a"),
        call("2026-07-02T09:00:00", lead_id="a"),
        call("2026-07-02T15:00:00", lead_id="b", status="inbound"),
    ]

    rows = _compute_daily_call_series(calls)

    assert rows == [
        {"date": "2026-07-01", "calls": 1, "inbound": 0, "talk_min": 1.0, "called": 1},
        {"date": "2026-07-02", "calls": 2, "inbound": 1, "talk_min": 2.0, "called": 1},
    ]


def test_a_lead_is_counted_as_called_once_on_their_first_call_only():
    """Lead 'a' is called three times across two days; 'called' must count
    them once, on the earlier day — otherwise it's a copy of Total calls."""
    calls = [
        call("2026-07-02T09:00:00", lead_id="a"),
        call("2026-07-01T09:00:00", lead_id="a"),
        call("2026-07-02T15:00:00", lead_id="a"),
    ]

    rows = _compute_daily_call_series(calls)

    assert [r["called"] for r in rows] == [1, 0]


def test_calls_without_a_matched_lead_still_count_toward_calls_and_talk():
    calls = [call("2026-07-01T09:00:00", lead_id=None)]

    rows = _compute_daily_call_series(calls)

    assert rows == [{"date": "2026-07-01", "calls": 1, "inbound": 0, "talk_min": 1.0, "called": 0}]


def test_calls_without_a_timestamp_are_skipped():
    calls = [{"call_status": "outbound", "duration": 60}, call("2026-07-01T09:00:00")]

    rows = _compute_daily_call_series(calls)

    assert len(rows) == 1


def test_prefers_matched_lead_id_over_raw_lead_id():
    calls = [
        {**call("2026-07-01T09:00:00", lead_id="raw"), "matched_lead_id": "matched"},
        {**call("2026-07-02T09:00:00", lead_id="matched"), "matched_lead_id": None},
    ]

    rows = _compute_daily_call_series(calls)

    # Both calls resolve to the same lead ("matched"), so only the earlier
    # day counts a "called" — the raw leadId on day two must not be treated
    # as a second, different lead.
    assert [r["called"] for r in rows] == [1, 0]


class FakeCollection:
    def __init__(self, docs):
        self._docs = docs
        self.updates = []

    def find(self, _filt, _proj=None):
        return FakeCursor(self._docs)

    async def update_many(self, filt, update):
        self.updates.append((filt, update))


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for d in self._docs:
            yield d


class FakeDb:
    def __init__(self, collections):
        self._collections = collections

    def __getitem__(self, name):
        return self._collections[name]


class FakeMongo:
    def __init__(self, collections):
        self._db = FakeDb(collections)

    def __getitem__(self, _name):
        return self._db


@pytest.mark.asyncio
async def test_cache_from_stored_writes_the_series_with_no_api_call():
    leads_col = FakeCollection([
        {"lead_data": {"call_logs": [call("2026-07-01T09:00:00", lead_id="a")]}},
        {"lead_data": {"call_logs": [call("2026-07-02T09:00:00", lead_id="b")]}},
    ])
    groups_col = FakeCollection([])
    mongo = FakeMongo({"hotprospector_leads": leads_col, "client_groups": groups_col})

    written = await cache_hp_daily_calls_from_stored("g1", "u1", "loc1", mongo)

    assert written == 2
    _filt, update = groups_col.updates[0]
    assert len(update["$set"]["hp_daily_calls"]) == 2
