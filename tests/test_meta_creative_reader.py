"""Meta tracking reads: ad-id batching, paging, error mapping, status."""

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from integrations.facebook_utils.meta_creative_reader import (
    TRACK_FIELDS,
    MetaReadError,
    fetch_account_daily,
    fetch_ad_status,
    fetch_tracked_ad_days,
)

D1, D2 = date(2026, 10, 1), date(2026, 10, 30)


@pytest.fixture
def patch_client(monkeypatch):
    """Serve canned Meta responses; record each request URL."""
    def apply(pages, seen):
        calls = {"n": 0}

        def handler(request):
            seen.append(str(request.url))
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return httpx.Response(page.get("status", 200), json=page["body"])

        real = httpx.AsyncClient

        def factory(*_a, **kw):
            return real(transport=httpx.MockTransport(handler),
                        **{k: v for k, v in kw.items() if k != "timeout"})

        monkeypatch.setattr(
            "integrations.facebook_utils.meta_creative_reader.httpx.AsyncClient", factory)
    return apply


@pytest.mark.asyncio
async def test_batches_ad_ids_by_50(patch_client):
    seen = []
    patch_client([{"body": {"data": []}}], seen)
    ids = [f"ad{i}" for i in range(120)]

    await fetch_tracked_ad_days("act_1", "T", ids, D1, D2)

    assert len(seen) == 3
    qs = [parse_qs(urlparse(u).query) for u in seen]
    assert [len(json.loads(q["filtering"][0])[0]["value"]) for q in qs] == [50, 50, 20]
    assert all(q["fields"] == [TRACK_FIELDS] for q in qs)
    assert qs[0]["level"] == ["ad"] and qs[0]["time_increment"] == ["1"]
    assert json.loads(qs[0]["filtering"][0])[0]["field"] == "ad.id"
    assert urlparse(seen[0]).path == "/v25.0/act_1/insights"


@pytest.mark.asyncio
async def test_follows_paging(patch_client):
    seen = []
    nxt = "https://graph.facebook.com/v25.0/act_1/insights?after=C&access_token=T"
    patch_client([
        {"body": {"data": [{"ad_id": "a", "date_start": "2026-10-01"}], "paging": {"next": nxt}}},
        {"body": {"data": [{"ad_id": "a", "date_start": "2026-10-02"}]}},
    ], seen)

    rows = await fetch_tracked_ad_days("act_1", "T", ["a"], D1, D2)

    assert [r["date_start"] for r in rows] == ["2026-10-01", "2026-10-02"]
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_rate_limit_error_is_retryable(patch_client):
    patch_client([{"status": 400, "body": {"error": {"code": 17, "message": "limit"}}}], [])
    with pytest.raises(MetaReadError) as ei:
        await fetch_tracked_ad_days("act_1", "T", ["a"], D1, D2)
    assert ei.value.code == 17 and ei.value.retryable is True


@pytest.mark.asyncio
async def test_other_error_not_retryable(patch_client):
    patch_client([{"status": 400, "body": {"error": {"code": 190}}}], [])
    with pytest.raises(MetaReadError) as ei:
        await fetch_ad_status("1", "T")
    assert ei.value.retryable is False


@pytest.mark.asyncio
async def test_status_none_when_deleted(patch_client):
    patch_client([{"status": 400, "body": {"error": {"code": 100}}}], [])
    assert await fetch_ad_status("1", "T") is None


@pytest.mark.asyncio
async def test_status_returns_effective_status(patch_client):
    seen = []
    patch_client([{"body": {"effective_status": "ACTIVE", "id": "1"}}], seen)
    assert await fetch_ad_status("1", "T") == "ACTIVE"
    assert parse_qs(urlparse(seen[0]).query)["fields"] == ["effective_status"]


@pytest.mark.asyncio
async def test_account_daily_is_account_level(patch_client):
    seen = []
    patch_client([{"body": {"data": [{"date_start": "2026-10-01", "spend": "9"}]}}], seen)
    rows = await fetch_account_daily("act_1", "T", D1, D2)
    q = parse_qs(urlparse(seen[0]).query)
    assert q["level"] == ["account"] and "filtering" not in q
    assert rows[0]["spend"] == "9"
