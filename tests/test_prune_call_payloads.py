"""
tests/test_prune_call_payloads.py
---------------------------------
Dropping verbatim webhook bodies from call_logs past the retention window.

A TTL index cannot do this: MongoDB TTL removes the whole document, and there
is no per-field expiry. A TTL on call_logs would delete the call history
itself, which every Sales Hub and Call Centre figure would notice.

So the filter is doing the safety work, and these pin it:

  - only rows past the cutoff are touched
  - only raw_payload/headers are removed, never the row
  - rows already pruned are excluded, so a nightly re-run is a no-op rather
    than a full-collection write

Context for the window: ingestion runs at roughly 50,000 calls a month and
raw_payload + headers average ~1 KB per row, so this was growing about 50 MB a
month, unbounded.
"""

from datetime import datetime, timedelta

import pytest

from routers.cron import prune_call_payloads


class FakeCallLogs:
    def __init__(self):
        self.calls = []

    async def update_many(self, filt, update):
        self.calls.append((filt, update))

        class R:
            modified_count = 7
        return R()


class FakeDb:
    def __init__(self, coll):
        self._coll = coll

    def __getitem__(self, name):
        assert name == "call_logs", f"pruned the wrong collection: {name}"
        return self._coll


class FakeMongo:
    def __init__(self, coll):
        self._db = FakeDb(coll)

    def __getitem__(self, _n):
        return self._db

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def patched(monkeypatch):
    coll = FakeCallLogs()
    monkeypatch.setattr("routers.cron.get_mongo_client", lambda: FakeMongo(coll))
    monkeypatch.setattr("routers.cron._verify_cron_auth", lambda _a: None)
    return coll


@pytest.mark.asyncio
async def test_it_reports_what_it_pruned(patched):
    out = await prune_call_payloads(authorization="Bearer x")

    assert out["ok"] is True
    assert out["pruned"] == 7
    assert out["retain_days"] == 90


@pytest.mark.asyncio
async def test_only_rows_past_the_cutoff_are_touched(patched):
    await prune_call_payloads(authorization="Bearer x")
    filt, _ = patched.calls[0]

    cutoff = filt["received_at"]["$lt"]
    expected = datetime.utcnow() - timedelta(days=90)
    assert abs((cutoff - expected).total_seconds()) < 60


@pytest.mark.asyncio
async def test_it_unsets_the_payload_and_nothing_else(patched):
    """The row itself must survive — the normalized fields are what every
    call figure is built from."""
    await prune_call_payloads(authorization="Bearer x")
    _, update = patched.calls[0]

    assert set(update) == {"$unset"}
    assert set(update["$unset"]) == {"raw_payload", "headers"}


@pytest.mark.asyncio
async def test_already_pruned_rows_are_excluded(patched):
    """Without this the nightly run rewrites every old row forever, which is a
    full-collection write for no change."""
    await prune_call_payloads(authorization="Bearer x")
    filt, _ = patched.calls[0]

    assert filt["$or"] == [
        {"raw_payload": {"$exists": True}},
        {"headers": {"$exists": True}},
    ]


@pytest.mark.asyncio
async def test_a_failure_still_returns_200(patched, monkeypatch):
    """Vercel retries failing crons aggressively; the other handlers all
    swallow and log for the same reason."""
    async def boom(*a, **k):
        raise RuntimeError("mongo down")
    monkeypatch.setattr(patched, "update_many", boom)

    out = await prune_call_payloads(authorization="Bearer x")

    assert out["ok"] is True
    assert out["pruned"] == 0
