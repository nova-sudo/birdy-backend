"""
tests/test_meta_daily_spend.py
------------------------------
Account-level daily ad spend.

The chart used to draw a preset total spread across days by lead share, which
inherited every gap in lead capture — at 37% capture it drew £2,554 for a day
that actually cost £718. These pin the properties that keep the replacement
honest: real rows, correct pagination, and never blanking good data on failure.
"""

import httpx
import pytest

from integrations.facebook_utils.meta_daily_spend import (
    cache_account_daily_spend,
    fetch_account_daily_spend,
)


def transport(pages, seen=None):
    """Serve a canned sequence of Meta responses, recording each URL."""
    calls = {"n": 0}

    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return httpx.Response(page.get("status", 200), json=page["body"])

    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    """Point the module's httpx.AsyncClient at a mock transport."""
    def apply(pages, seen=None):
        real = httpx.AsyncClient

        def factory(*_a, **kw):
            return real(transport=transport(pages, seen), **{
                k: v for k, v in kw.items() if k != "timeout"
            })

        monkeypatch.setattr(
            "integrations.facebook_utils.meta_daily_spend.httpx.AsyncClient", factory
        )
    return apply


@pytest.mark.asyncio
async def test_returns_one_row_per_day(patch_client):
    patch_client([{"body": {"data": [
        {"date_start": "2026-08-15", "spend": "310.5"},
        {"date_start": "2026-08-16", "spend": "718.52"},
    ]}}])

    rows = await fetch_account_daily_spend("act_1", "T", "2026-08-15", "2026-08-16")

    assert rows == [
        {"date": "2026-08-15", "spend": 310.5},
        {"date": "2026-08-16", "spend": 718.52},
    ]


@pytest.mark.asyncio
async def test_asks_meta_for_daily_rows_over_the_window(patch_client):
    seen = []
    patch_client([{"body": {"data": []}}], seen)

    await fetch_account_daily_spend("act_1", "T", "2026-01-01", "2026-08-17")

    url = seen[0]
    assert "time_increment=1" in url
    assert "2026-01-01" in url and "2026-08-17" in url
    # No `level` — account totals are what the portfolio chart sums.
    assert "level=" not in url


@pytest.mark.asyncio
async def test_sorts_by_date(patch_client):
    patch_client([{"body": {"data": [
        {"date_start": "2026-08-16", "spend": "2"},
        {"date_start": "2026-08-14", "spend": "1"},
    ]}}])

    rows = await fetch_account_daily_spend("act_1", "T", "2026-08-14", "2026-08-16")

    assert [r["date"] for r in rows] == ["2026-08-14", "2026-08-16"]


@pytest.mark.asyncio
async def test_follows_pagination_without_losing_the_cursor(patch_client):
    seen = []
    patch_client([
        {"body": {"data": [{"date_start": "2026-08-15", "spend": "1"}],
                  "paging": {"next": "https://graph.facebook.com/next?access_token=T&after=CUR"}}},
        {"body": {"data": [{"date_start": "2026-08-16", "spend": "2"}]}},
    ], seen)

    rows = await fetch_account_daily_spend("act_1", "T", "2026-08-15", "2026-08-16")

    assert len(rows) == 2
    # The bug that cost every lead past page 1: an empty params dict replaces
    # the query string, stripping the token and cursor Meta put in `next`.
    assert "after=CUR" in seen[1]
    assert "access_token=T" in seen[1]


@pytest.mark.asyncio
async def test_a_failed_fetch_is_none_not_empty(patch_client):
    """None and [] mean different things: [] is 'spent nothing', None is 'we
    do not know'. Conflating them would let a bad API day wipe real history."""
    patch_client([{"status": 500, "body": {"error": "boom"}}])

    assert await fetch_account_daily_spend("act_1", "T", "2026-08-01", "2026-08-16") is None


@pytest.mark.asyncio
async def test_no_spend_in_the_window_is_an_empty_list(patch_client):
    patch_client([{"body": {"data": []}}])

    assert await fetch_account_daily_spend("act_1", "T", "2026-08-01", "2026-08-16") == []


class FakeGroups:
    def __init__(self):
        self.updates = []

    async def update_one(self, filt, update):
        self.updates.append((filt, update))


class FakeDb:
    def __init__(self, groups):
        self._groups = groups

    def __getitem__(self, _name):
        return self._groups


class FakeMongo:
    def __init__(self, groups):
        self._db = FakeDb(groups)

    def __getitem__(self, _name):
        return self._db


@pytest.mark.asyncio
async def test_a_failed_fetch_leaves_the_cache_alone(patch_client):
    patch_client([{"status": 500, "body": {"error": "boom"}}])
    groups = FakeGroups()

    written = await cache_account_daily_spend("g1", "act_1", "T", FakeMongo(groups))

    assert written == 0
    assert groups.updates == []


@pytest.mark.asyncio
async def test_a_successful_fetch_replaces_the_window(patch_client):
    """Meta restates recent days as attribution settles, so the window is
    rewritten rather than appended to."""
    patch_client([{"body": {"data": [{"date_start": "2026-08-16", "spend": "718.52"}]}}])
    groups = FakeGroups()

    written = await cache_account_daily_spend("g1", "act_1", "T", FakeMongo(groups))

    assert written == 1
    _filt, update = groups.updates[0]
    assert update["$set"]["meta_daily_spend"] == [{"date": "2026-08-16", "spend": 718.52}]
